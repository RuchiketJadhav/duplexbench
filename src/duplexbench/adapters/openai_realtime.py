"""OpenAI Realtime API adapter over a WebSocket (spec §2.1 row 2, §6.1; BUILD_PLAN M3-T03).

Facts used here are from docs/providers.md §3 (checked 2026-11-04):
* model default `gpt-realtime-2.1` (per docs/providers.md 2026-11-04: `gpt-realtime` is
  deprecated and shuts down 2027-01-20);
* GA session fields: `session.audio.input.format` / `session.audio.output.format`
  = `{"type": "audio/pcmu"}` (G.711 μ-law 8 kHz, native: no resampling), or
  `{"type": "audio/pcm", "rate": 24000}` when the condition has no codec (W);
  turn detection in `session.audio.input.turn_detection`, sent verbatim from the system config;
* `output_audio_buffer.clear` is WebRTC/SIP only, so this WebSocket client follows the
  reference-client behaviour: on interruption it flushes locally and sends
  `conversation.item.truncate{item_id, content_index, audio_end_ms}`.

Event mapping (provider -> harness):

    session.updated                              -> session_open (open() waits for it)
    response.output_audio.delta                  -> audio {pcm int16, rate 8000}  (base64 μ-law decoded)
    input_audio_buffer.speech_started            -> cancel_or_truncate (only while agent audio is
                                                    in flight; + local flush + item.truncate)
    response.cancelled / response.done{cancelled} -> cancel_or_truncate (once per response)
    response.output_audio_transcript.delta/.done -> output_transcript {text, final}
    response.function_call_arguments.done        -> tool_call {id: call_id, name, arguments}
    response.done.response.usage                 -> usage {usage, response_id, status}
    error                                        -> error (except errors caused by our own
                                                    truncate, which are recorded in `warnings`)
    transport drop                               -> error, then closed (transport/live_stream.py)

"Local flush": after a truncate, further deltas of the truncated item still in
flight are dropped (`dropped_deltas`), so nothing the provider cancelled reaches
the playout queue. `audio_end_ms` is the played estimate: wall time since the
item's first delta, capped at the audio received for it (the adapter does not
see the playout queue; error <= jitter buffer).

`tool_result` sends `conversation.item.create{function_call_output}` then
`response.create`. Usage payloads accumulate in `usage_log` for `metrics/cost.py`.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from duplexbench.adapters.base import Adapter, AdapterCaps, AdapterEvent
from duplexbench.channel import mulaw
from duplexbench.channel.resample import Resampler
from duplexbench.transport.live_stream import Connector, LiveStream

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

DEFAULT_MODEL = "gpt-realtime-2.1"     # per docs/providers.md 2026-11-04 (gpt-realtime deprecated 2027-01-20)
DEFAULT_URL = "wss://api.openai.com/v1/realtime"
MULAW_RATE = 8000                      # G.711 is 8 kHz by definition
PCM_RATE = 24000                       # "Only a 24kHz sample rate is supported" for audio/pcm
_NS_PER_MS = 1_000_000
_MS_PER_S = 1e3                        # threshold-ok: unit conversion


@dataclass
class _Item:
    item_id: str
    content_index: int
    t_first_ns: int
    received_samples: int = 0
    rate: int = MULAW_RATE
    truncated: bool = False

    def received_ms(self) -> float:
        return self.received_samples * _MS_PER_S / self.rate


class OpenAIRealtimeAdapter(Adapter):
    name = "openai_realtime"
    caps = AdapterCaps(cancel_event=True, native_mulaw=True, output_transcript=True,
                       usage_fields=True)

    def __init__(self, clock: "Clock", system: Any, *, api_key: str | None = None,
                 url: str | None = None, connector: Connector | None = None,
                 tool_schemas: list[dict] | None = None):
        cfg = system.model_dump(mode="json") if hasattr(system, "model_dump") else dict(system)
        self.clock = clock
        self.config = cfg
        params = dict(cfg.get("params") or {})
        self.params = params
        self.model = cfg.get("model") or DEFAULT_MODEL
        self.turn_detection = cfg.get("turn_detection")
        self.reasoning_effort = cfg.get("reasoning_effort")
        base = url or params.get("url") or DEFAULT_URL
        sep = "&" if "?" in base else "?"
        self.url = f"{base}{sep}model={self.model}"
        key_env = params.get("api_key_env") or "OPENAI_API_KEY"
        self._api_key = api_key if api_key is not None else os.environ.get(key_env, "")
        self._key_env = key_env
        self._connector = connector
        self.open_timeout_s = float(params.get("open_timeout_s") or 15)
        self.tool_schemas: list[dict] = list(tool_schemas or [])
        self.stream: LiveStream | None = None
        self.call: "CallContext | None" = None
        self._ids = itertools.count(1)
        self._pending: list[AdapterEvent] = []        # events observed during open()
        self._items: dict[str, _Item] = {}
        self._current: _Item | None = None
        self._response_active = False
        self._cancelled_responses: set[str] = set()
        self._cur_response: str | None = None
        self._truncate_ids: set[str] = set()
        self._codec = True
        self._in_rs: Resampler | None = None
        self._line_rate = MULAW_RATE
        self.usage_log: list[dict] = []
        self.warnings: list[dict] = []
        self.unmapped: dict[str, int] = {}
        self.dropped_deltas = 0
        self.sent_events: list[dict] = []             # client events except audio appends
        self.session: dict | None = None

    # -- session ----------------------------------------------------------------
    def _event_id(self, prefix: str) -> str:
        return f"db_{prefix}_{next(self._ids)}"

    def _formats(self) -> tuple[dict, dict]:
        if self._codec:
            return {"type": "audio/pcmu"}, {"type": "audio/pcmu"}
        return {"type": "audio/pcm", "rate": PCM_RATE}, {"type": "audio/pcm", "rate": PCM_RATE}

    def session_update(self) -> dict:
        fin, fout = self._formats()
        audio_in: dict[str, Any] = {"format": fin, "turn_detection": self.turn_detection}
        audio_out: dict[str, Any] = {"format": fout}
        if self.params.get("voice"):
            audio_out["voice"] = self.params["voice"]
        session: dict[str, Any] = {"type": "realtime", "model": self.model,
                                   "output_modalities": ["audio"],
                                   "audio": {"input": audio_in, "output": audio_out}}
        if self.params.get("instructions"):
            session["instructions"] = self.params["instructions"]
        if self.tool_schemas:
            session["tools"] = [{"type": "function", "name": t["name"],
                                 "description": t.get("description", ""),
                                 "parameters": t.get("parameters") or {"type": "object"}}
                                for t in self.tool_schemas]
            session["tool_choice"] = "auto"
        if self.reasoning_effort:
            # [Assumption] field shape (`reasoning.effort`) is not confirmed for gpt-realtime-2.1
            session["reasoning"] = {"effort": self.reasoning_effort}
        return {"type": "session.update", "event_id": self._event_id("session"),
                "session": session}

    async def _send(self, obj: dict) -> bool:
        if obj.get("type") != "input_audio_buffer.append":
            self.sent_events.append(obj)
        assert self.stream is not None
        return await self.stream.send_json(obj)

    async def open(self, call: "CallContext") -> None:
        self.call = call
        cond = getattr(call, "condition", None)
        self._codec = bool(getattr(cond, "codec", True))
        self._line_rate = int(getattr(cond, "rate", MULAW_RATE))
        if not self._codec:
            self._in_rs = Resampler(self._line_rate, PCM_RATE)
        if not self._api_key:
            raise RuntimeError(f"{self.name}: no API key (${self._key_env} is empty)")
        self.stream = LiveStream(self.clock, self.url,
                                 {"Authorization": f"Bearer {self._api_key}"},
                                 connector=self._connector, open_timeout_s=self.open_timeout_s)
        await self.stream.connect()
        await self._send(self.session_update())
        # Wait for session.updated before any audio is appended (else it is read in the
        # default 24 kHz PCM format). Messages seen meanwhile are replayed by events().
        deadline = asyncio.get_running_loop().time() + self.open_timeout_s
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise TimeoutError(f"{self.name}: no session.updated in {self.open_timeout_s} s")
            m = await asyncio.wait_for(self.stream.recv(), timeout=left)
            evs = list(self._map_message(m))
            self._pending += evs
            for ev in evs:
                if ev.kind in ("error", "closed"):
                    raise RuntimeError(f"{self.name}: session setup failed: {ev.payload}")
            if any(ev.kind == "session_open" for ev in evs):
                return

    # -- caller audio -------------------------------------------------------------
    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        if self.stream is None:
            return
        if self._codec:
            payload = bytes(mulaw_bytes)     # native μ-law 8 kHz: passed through untouched
        else:
            pcm = np.frombuffer(mulaw_bytes, dtype=np.int16)
            assert self._in_rs is not None
            payload = self._in_rs.push(pcm).astype("<i2").tobytes()
            if not payload:
                return
        await self.stream.send_json({"type": "input_audio_buffer.append",
                                     "audio": base64.b64encode(payload).decode("ascii")})

    # -- tools --------------------------------------------------------------------
    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        if self.stream is None:
            return
        await self._send({"type": "conversation.item.create", "event_id": self._event_id("tool"),
                          "item": {"type": "function_call_output", "call_id": tool_call_id,
                                   "output": json.dumps(result, separators=(",", ":"))}})
        await self._send({"type": "response.create", "event_id": self._event_id("resp")})

    async def close(self) -> None:
        if self.stream is not None:
            await self.stream.close()

    # -- events -------------------------------------------------------------------
    def events(self) -> AsyncIterator[AdapterEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[AdapterEvent]:
        if self.stream is None:
            raise RuntimeError(f"{self.name}: events() before open()")
        pending, self._pending = self._pending, []
        for ev in pending:
            yield ev
        while True:
            m = await self.stream.recv()
            evs = list(self._map_message(m))
            truncate = None
            for ev in evs:
                if ev.kind == "cancel_or_truncate" and ev.payload.get("truncate"):
                    truncate = ev.payload["truncate"]
            if truncate is not None:            # reference-client flush: tell the server
                await self._send(truncate)
            for ev in evs:
                yield ev
                if ev.kind == "closed":
                    return

    def _map_message(self, m) -> list[AdapterEvent]:
        t = m.t_ns
        if m.kind == "error":
            return [AdapterEvent("error", t, "ws.close", dict(m.info))]
        if m.kind == "closed":
            return [AdapterEvent("closed", t, "ws.close", dict(m.info))]
        if m.kind == "binary":
            self.unmapped["<binary>"] = self.unmapped.get("<binary>", 0) + 1
            return []
        try:
            obj = json.loads(m.data)
        except (ValueError, TypeError):
            self.unmapped["<non-json>"] = self.unmapped.get("<non-json>", 0) + 1
            return []
        if not isinstance(obj, dict):
            return []
        return self.map_event(obj, t)

    def map_event(self, e: dict, t: int) -> list[AdapterEvent]:
        """One provider event -> harness events (pure apart from adapter state)."""
        typ = str(e.get("type", ""))
        if typ == "session.updated":
            self.session = e.get("session")
            return [AdapterEvent("session_open", t, typ, {"session_id": (e.get("session") or {}).get("id")})]
        if typ == "response.created":
            self._response_active = True
            self._cur_response = (e.get("response") or {}).get("id")
            return []
        if typ == "response.output_audio.delta":
            return self._audio(e, t)
        if typ == "input_audio_buffer.speech_started":
            return self._speech_started(e, t)
        if typ == "response.cancelled":
            rid = e.get("response_id") or (e.get("response") or {}).get("id")
            return self._cancelled(rid, t, typ, "response.cancelled")
        if typ in ("response.output_audio_transcript.delta", "response.output_audio_transcript.done"):
            final = typ.endswith(".done")
            text = e.get("transcript") if final else e.get("delta")
            return [AdapterEvent("output_transcript", t, typ,
                                 {"text": text or "", "final": final, "item_id": e.get("item_id"),
                                  "response_id": e.get("response_id")})]
        if typ == "response.function_call_arguments.done":
            raw = e.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                args = {"_raw": raw}
            return [AdapterEvent("tool_call", t, typ, {"id": e.get("call_id"), "name": e.get("name"),
                                                        "arguments": args,
                                                        "item_id": e.get("item_id")})]
        if typ == "response.done":
            resp = e.get("response") or {}
            rid, status = resp.get("id"), resp.get("status")
            out: list[AdapterEvent] = []
            if status == "cancelled":
                reason = (resp.get("status_details") or {}).get("reason")
                out += self._cancelled(rid, t, typ, reason or "cancelled")
            self._response_active = False
            self._cur_response = None
            usage = resp.get("usage")
            if usage is not None:
                self.usage_log.append(usage)
                out.append(AdapterEvent("usage", t, typ, {"usage": usage, "response_id": rid,
                                                          "status": status}))
            return out
        if typ == "conversation.item.truncated":
            return []
        if typ == "error":
            err = e.get("error") or {}
            if err.get("event_id") in self._truncate_ids:   # our truncate raced the item end
                self.warnings.append(err)
                return []
            return [AdapterEvent("error", t, typ, {"type": err.get("type", "provider_error"),
                                                   "code": err.get("code"),
                                                   "message": err.get("message", ""),
                                                   "event_id": err.get("event_id")})]
        self.unmapped[typ] = self.unmapped.get(typ, 0) + 1
        return []

    def _audio(self, e: dict, t: int) -> list[AdapterEvent]:
        item_id = str(e.get("item_id") or "")
        ci = int(e.get("content_index") or 0)
        it = self._items.get(item_id)
        if it is None:
            rate = MULAW_RATE if self._codec else PCM_RATE
            it = self._items[item_id] = _Item(item_id, ci, t, rate=rate)
        self._current = it
        if it.truncated:                          # local flush: cancelled audio still in flight
            self.dropped_deltas += 1
            return []
        raw = base64.b64decode(e.get("delta") or "")
        if self._codec:
            pcm = mulaw.decode(raw)
        else:
            pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.int16)
        it.received_samples += len(pcm)
        return [AdapterEvent("audio", t, "response.output_audio.delta",
                             {"pcm": pcm, "rate": it.rate, "item_id": item_id})]

    def _played_ms(self, it: _Item, t: int) -> int:
        return int(min(it.received_ms(), max(0.0, (t - it.t_first_ns) / _NS_PER_MS)))

    def _in_flight(self, it: _Item | None, t: int) -> bool:
        if it is None or it.truncated:
            return False
        return self._response_active or self._played_ms(it, t) < int(it.received_ms())

    def _truncate_msg(self, it: _Item, t: int) -> dict:
        eid = self._event_id("trunc")
        self._truncate_ids.add(eid)
        return {"type": "conversation.item.truncate", "event_id": eid, "item_id": it.item_id,
                "content_index": it.content_index, "audio_end_ms": self._played_ms(it, t)}

    def _speech_started(self, e: dict, t: int) -> list[AdapterEvent]:
        it = self._current
        if not self._in_flight(it, t):
            return []                             # agent not speaking: nothing to cancel
        assert it is not None
        msg = self._truncate_msg(it, t)
        it.truncated = True
        if self._cur_response:
            self._cancelled_responses.add(self._cur_response)
        return [AdapterEvent("cancel_or_truncate", t, "input_audio_buffer.speech_started",
                             {"reason": "speech_started", "item_id": it.item_id,
                              "audio_start_ms": e.get("audio_start_ms"),
                              "audio_end_ms": msg["audio_end_ms"],
                              "received_ms": it.received_ms(), "truncate": msg})]

    def _cancelled(self, rid: str | None, t: int, typ: str, reason: str) -> list[AdapterEvent]:
        if rid is not None and rid in self._cancelled_responses:
            return []                             # already flushed on speech_started
        if rid is not None:
            self._cancelled_responses.add(rid)
        it = self._current
        payload: dict[str, Any] = {"reason": reason, "response_id": rid}
        if it is not None and not it.truncated and self._played_ms(it, t) < int(it.received_ms()):
            payload["truncate"] = self._truncate_msg(it, t)
            payload["item_id"] = it.item_id
            payload["audio_end_ms"] = payload["truncate"]["audio_end_ms"]
            it.truncated = True
        return [AdapterEvent("cancel_or_truncate", t, typ, payload)]
