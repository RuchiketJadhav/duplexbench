"""OpenAI GPT-Live-1 adapter (BUILD_PLAN M6-T04/T05; docs/providers.md §1, checked 2026-11-04).

Native full-duplex `gpt-live-1` on `wss://api.openai.com/v1/live/sessions` (not the
Realtime API). One audio format for both directions, fixed at start:
`session.audio.format = {"type":"audio/pcmu","rate":8000}` [Fact].

**No cancel / truncate / interruption event is documented** [Fact]: barge-in happens
inside the model, so `caps.cancel_event=False`, nothing is faked (CLAUDE.md §6 item 9),
and the report takes the missing-cancel caveat path (system YAML `cancel_event: false`).
There are no turn-detection parameters to configure.

    harness event        <- GPT-Live server event                               status
    session_open         <- `session.started`                                   confirmed name; payload UNCONFIRMED
    audio                <- `session.output_audio.delta` (`delta`, b64 μ-law)   confirmed (no timing fields)
    first_audio_byte     <- base class (timestamped on receipt)
    cancel_or_truncate   <- none                                                 documented absence
    output_transcript    <- `session.output_transcript.delta` (`delta`)        confirmed; deltas, no turn end
    tool_call            <- `session.delegation.created` {delegation: {id, ...}} confirmed name; name/arguments fields UNCONFIRMED
    usage                <- `session.usage.updated` (cumulative `usage.seconds`), final in `session.closed`
    closed               <- `session.closed`
    tool result          -> `session.thinking.append`                            UNCONFIRMED (shape)
"""
from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from duplexbench.adapters._convert import MULAW_RATE, InputConverter, parse_json
from duplexbench.transport.live_stream import Connector
from duplexbench.adapters.base import AdapterCaps, AdapterEvent
from duplexbench.adapters.grok_voice import RealtimeStyle, _args

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

DEFAULT_ENDPOINT = "wss://api.openai.com/v1/live/sessions"


class GptLive(RealtimeStyle):
    name = "gpt_live"
    caps = AdapterCaps(cancel_event=False, native_mulaw=True, output_transcript=True,
                       usage_fields=True)

    def __init__(self, clock: "Clock", config: dict, *, connector: Connector | None = None,
                 tool_schemas: list[dict] | None = None, api_key: str | None = None):
        super().__init__(clock, config, connector, tool_schemas, api_key, "OPENAI_API_KEY")
        self.model = self.model or "gpt-live-1"
        self.usage_seconds: float | None = None
        self.delegations: dict[str, dict] = {}

    def session_start(self) -> dict:
        sess: dict[str, Any] = {
            "model": self.model,      # UNCONFIRMED per docs/providers.md: model in session.start vs URL query
            "audio": {"format": {"type": "audio/pcmu", "rate": MULAW_RATE}},
            "delegation": {"type": self.acfg.get("delegation", "client")},
        }
        for k in ("instructions", "voice"):
            if self.acfg.get(k):
                sess[k] = self.acfg[k]
        if self.tool_schemas:     # UNCONFIRMED per docs/providers.md: tool declaration on a Live session
            sess["tools"] = [{"type": "function", "name": t["name"],
                              "description": t.get("description", ""),
                              "parameters": t.get("parameters") or {"type": "object"}}
                             for t in self.tool_schemas]
        return {"type": "session.start", "session": sess}

    async def open(self, call: "CallContext") -> None:
        self.call = call
        self.conv = InputConverter(call, native_mulaw=True)
        url = f"{self.acfg.get('endpoint') or DEFAULT_ENDPOINT}?model={self.model}"   # UNCONFIRMED per docs/providers.md
        await self._connect(url, {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
        await self._send_json(self.session_start())

    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        if self.conv is None:
            return
        await self._send_json({"type": "session.input_audio.append", "audio": base64.b64encode(
            self.conv.to_mulaw8k(mulaw_bytes)).decode("ascii")})

    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        # UNCONFIRMED per docs/providers.md: client-delegation result shape
        await self._send_json({"type": "session.thinking.append", "delegation_id": tool_call_id,
                               "text": json.dumps(result)})

    def _map(self, msg: str | bytes, t_ns: int) -> Iterable[AdapterEvent]:
        obj = parse_json(msg)
        if obj is None:
            self.raw_log.append((t_ns, "non-json"))
            return
        typ = str(obj.get("type", ""))
        if typ == "session.started" and not self._opened:
            self._opened = True
            yield AdapterEvent("session_open", t_ns, typ, {})
        elif typ == "session.output_audio.delta" and obj.get("delta"):
            yield self._audio(obj["delta"], t_ns, typ)
        elif typ == "session.output_transcript.delta":
            yield AdapterEvent("output_transcript", t_ns, typ,
                               {"text": obj.get("delta", ""), "final": False})
        elif typ == "session.delegation.created":
            d = obj.get("delegation") or {}
            did = str(d.get("id", ""))
            self.delegations[did] = d
            # UNCONFIRMED per docs/providers.md: `name` / `arguments` on a client delegation
            yield AdapterEvent("tool_call", t_ns, typ,
                               {"id": did, "name": d.get("name") or d.get("tool"),
                                "arguments": _args(d.get("arguments", d.get("input")))})
        elif typ in ("session.usage.updated", "session.closed"):
            usage = obj.get("usage") or (obj.get("session") or {}).get("usage")
            if isinstance(usage, dict):
                self.usage_seconds = usage.get("seconds", self.usage_seconds)   # cumulative
                yield AdapterEvent("usage", t_ns, typ, dict(usage, cumulative=True))
            if typ == "session.closed":
                yield AdapterEvent("closed", t_ns, typ, {"by": "provider"})
        elif typ == "error":
            err = obj.get("error") or {}
            yield AdapterEvent("error", t_ns, typ, {"type": str(err.get("type", "provider_error")),
                                                    "message": str(err.get("message", ""))})
        else:
            self.raw_log.append((t_ns, typ))
