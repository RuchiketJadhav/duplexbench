"""xAI Grok Voice adapter (BUILD_PLAN M6-T04; docs/providers.md §5, checked 2026-11-04).

`grok-voice-3.0` over `wss://api.x.ai/v1/realtime?model=...`; OpenAI-Realtime-shaped
events [Fact: "same event shapes as OpenAI Realtime"]. μ-law native: input and output
`audio/pcmu` 8000 Hz, so the runner delivers μ-law bytes under a codec condition.

    harness event        <- Grok server event                                  status
    session_open         <- `session.updated`                                  UNCONFIRMED (which lifecycle event)
    audio                <- `response.output_audio.delta` / `response.audio.delta` (b64 μ-law)   confirmed
    first_audio_byte     <- base class
    cancel_or_truncate   <- `input_audio_buffer.speech_started` while a response is producing audio
                                                                               confirmed event; "while producing" is a harness rule
    output_transcript    <- `response.output_audio_transcript.done`            confirmed
    tool_call            <- `response.function_call_arguments.done` {call_id, name, arguments}  confirmed
    usage                <- `response.done.response.usage`                     UNCONFIRMED (no usage fields documented)
    error                <- `error`
    tool result          -> `conversation.item.create{function_call_output}` + `response.create`

No client `conversation.item.truncate` is sent: the harness does not know how much
of the reply was played at the far end when the event arrives (its ear owns that).
"""
from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from duplexbench.adapters._convert import MULAW_RATE, InputConverter, parse_json
from duplexbench.adapters.ws_adapter import WsAdapter
from duplexbench.transport.live_stream import Connector
from duplexbench.adapters.base import AdapterCaps, AdapterEvent
from duplexbench.channel import mulaw

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

DEFAULT_ENDPOINT = "wss://api.x.ai/v1/realtime"
AUDIO_DELTA = ("response.output_audio.delta", "response.audio.delta")


def realtime_tools(tool_schemas: list[dict]) -> list[dict]:
    return [{"type": "function", "name": t["name"], "description": t.get("description", ""),
             "parameters": t.get("parameters") or {"type": "object", "properties": {}}}
            for t in tool_schemas]


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"_raw": raw}
    return v if isinstance(v, dict) else {"_raw": v}


class RealtimeStyle(WsAdapter):
    """Shared receive mapping for OpenAI-Realtime-shaped μ-law sessions."""

    def __init__(self, clock: "Clock", config: dict, connector: Connector | None,
                 tool_schemas: list[dict] | None, api_key: str | None, key_env: str):
        super().__init__(clock, config, connector)
        self.model = str(self.config.get("model") or "")
        self.tool_schemas = list(tool_schemas or [])
        env = self.acfg.get("api_key_env", key_env)
        self.api_key = api_key if api_key is not None else os.environ.get(env, "")
        self.conv: InputConverter | None = None
        self._producing = False          # audio received since the last response.done
        self._opened = False

    def _audio(self, b64: str, t_ns: int, ev: str) -> AdapterEvent:
        self._producing = True
        return AdapterEvent("audio", t_ns, ev, {"pcm": mulaw.decode(base64.b64decode(b64)),
                                                "rate": MULAW_RATE})


class GrokVoice(RealtimeStyle):
    name = "grok_voice"
    caps = AdapterCaps(cancel_event=True, native_mulaw=True, output_transcript=True,
                       usage_fields=False)

    def __init__(self, clock: "Clock", config: dict, *, connector: Connector | None = None,
                 tool_schemas: list[dict] | None = None, api_key: str | None = None):
        super().__init__(clock, config, connector, tool_schemas, api_key, "XAI_API_KEY")
        self.model = self.model or "grok-voice-3.0"

    def session_update(self) -> dict:
        fmt = {"type": "audio/pcmu", "rate": MULAW_RATE}
        sess: dict[str, Any] = {
            "audio": {"input": {"format": dict(fmt)}, "output": {"format": dict(fmt)}},
            # UNCONFIRMED per docs/providers.md: turn_detection placement (session.turn_detection)
            "turn_detection": self.config.get("turn_detection") or {"type": "server_vad"},
        }
        if self.config.get("reasoning_effort"):
            sess["reasoning"] = {"effort": self.config["reasoning_effort"]}
        for k in ("instructions", "voice"):
            if self.acfg.get(k):
                sess[k] = self.acfg[k]
        if self.tool_schemas:
            sess["tools"] = realtime_tools(self.tool_schemas)
        return {"type": "session.update", "session": sess}

    async def open(self, call: "CallContext") -> None:
        self.call = call
        self.conv = InputConverter(call, native_mulaw=True)
        url = f"{self.acfg.get('endpoint') or DEFAULT_ENDPOINT}?model={self.model}"
        await self._connect(url, {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
        await self._send_json(self.session_update())

    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        if self.conv is None:
            return
        await self._send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(
            self.conv.to_mulaw8k(mulaw_bytes)).decode("ascii")})

    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        await self._send_json({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": tool_call_id,
            "output": json.dumps(result)}})
        await self._send_json({"type": "response.create"})

    def _map(self, msg: str | bytes, t_ns: int) -> Iterable[AdapterEvent]:
        obj = parse_json(msg)
        if obj is None:
            self.raw_log.append((t_ns, "non-json"))
            return
        typ = str(obj.get("type", ""))
        if typ == "session.updated" and not self._opened:   # UNCONFIRMED per docs/providers.md
            self._opened = True
            yield AdapterEvent("session_open", t_ns, typ, {})
        elif typ in AUDIO_DELTA and obj.get("delta"):
            yield self._audio(obj["delta"], t_ns, typ)
        elif typ == "input_audio_buffer.speech_started":
            if self._producing:
                self._producing = False
                yield AdapterEvent("cancel_or_truncate", t_ns, typ,
                                   {"item_id": obj.get("item_id")})
            else:
                self.raw_log.append((t_ns, typ))
        elif typ == "response.output_audio_transcript.done":
            yield AdapterEvent("output_transcript", t_ns, typ,
                               {"text": obj.get("transcript", ""), "final": True})
        elif typ == "response.function_call_arguments.done":
            yield AdapterEvent("tool_call", t_ns, typ, {"id": str(obj.get("call_id", "")),
                                                        "name": obj.get("name"),
                                                        "arguments": _args(obj.get("arguments"))})
        elif typ == "response.done":
            self._producing = False
            usage = (obj.get("response") or {}).get("usage")   # UNCONFIRMED per docs/providers.md
            if isinstance(usage, dict):
                yield AdapterEvent("usage", t_ns, typ, dict(usage))
        elif typ == "error":
            err = obj.get("error") or {}
            yield AdapterEvent("error", t_ns, typ, {"type": str(err.get("type", "provider_error")),
                                                    "message": str(err.get("message", ""))})
        else:
            self.raw_log.append((t_ns, typ))
