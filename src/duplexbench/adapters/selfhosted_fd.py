"""Generic self-hosted full-duplex model adapter (BUILD_PLAN M6-T06; spec §2.1).

For Moshi / PersonaPlex style servers behind a thin shim speaking this harness
protocol (Moshi's own wire protocol is Opus 24 kHz with a type byte per message,
docs/providers.md §2; the shim does the Opus step on the GPU box):

    client -> server   text  {"type":"start","sample_rate":R,"format":"pcm16le","call_id":...}
                       binary PCM16 LE mono frames at R
                       text  {"type":"stop"}
    server -> client   text  {"type":"ready"}                     -> session_open
                       binary PCM16 LE mono at `output_rate`      -> audio
                       text  {"type":"transcript","text":...}     -> output_transcript
                       text  {"type":"cancel"}                    -> cancel_or_truncate (only when the
                                                                     config says the server emits it)
                       text  {"type":"error","message":...}       -> error

Moshi has no cancel event and no tools (providers.md §2): the default config sets
`cancel_event: false`; a `cancel` message is then logged and not mapped, so nothing
is faked. `self_hosted=True`: latency includes the instance, flagged in every table.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import numpy as np

from duplexbench.adapters._convert import InputConverter, parse_json
from duplexbench.adapters.ws_adapter import WsAdapter
from duplexbench.transport.live_stream import Connector
from duplexbench.adapters.base import AdapterCaps, AdapterEvent

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

MODEL_RATE = 24000          # Mimi codec rate (providers.md §2) as the default shim rate


class SelfHostedFD(WsAdapter):
    name = "selfhosted_fd"
    self_hosted = True

    def __init__(self, clock: "Clock", config: dict, *, connector: Connector | None = None):
        super().__init__(clock, config, connector)
        if not self.acfg.get("endpoint"):
            raise ValueError("selfhosted_fd: params.endpoint is required")
        self.in_rate = int(self.acfg.get("input_rate", MODEL_RATE))
        self.out_rate = int(self.acfg.get("output_rate", MODEL_RATE))
        self.caps = AdapterCaps(cancel_event=bool(self.config.get("cancel_event", False)),
                                native_mulaw=False, output_transcript=True, usage_fields=False)
        self.conv: InputConverter | None = None

    async def open(self, call: "CallContext") -> None:
        self.call = call
        self.conv = InputConverter(call, native_mulaw=False)
        await self._connect(str(self.acfg["endpoint"]), self.acfg.get("headers") or {})
        await self._send_json({"type": "start", "sample_rate": self.in_rate,
                               "format": "pcm16le", "call_id": call.call_id})

    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        if self.conv is None:
            return
        pcm = self.conv.to_pcm(mulaw_bytes, self.in_rate)
        if pcm.size:
            await self._send(pcm.astype("<i2").tobytes())

    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        return None         # no tools on this protocol

    def _map(self, msg: str | bytes, t_ns: int) -> Iterable[AdapterEvent]:
        if isinstance(msg, (bytes, bytearray)):
            pcm = np.frombuffer(bytes(msg), dtype="<i2").astype(np.int16)
            if pcm.size:
                yield AdapterEvent("audio", t_ns, "binary", {"pcm": pcm, "rate": self.out_rate})
            return
        obj = parse_json(msg)
        typ = str((obj or {}).get("type", "non-json"))
        if typ == "ready":
            yield AdapterEvent("session_open", t_ns, typ, {})
        elif typ == "transcript":
            yield AdapterEvent("output_transcript", t_ns, typ,
                               {"text": obj.get("text", ""), "final": bool(obj.get("final", False))})
        elif typ == "cancel" and self.caps.cancel_event:
            yield AdapterEvent("cancel_or_truncate", t_ns, typ, {})
        elif typ == "error":
            yield AdapterEvent("error", t_ns, typ, {"type": "provider_error",
                                                    "message": str(obj.get("message", ""))})
        else:
            self.raw_log.append((t_ns, typ))

    async def close(self) -> None:
        if not self._closing:
            try:
                await self._send_json({"type": "stop"})
            except Exception:  # noqa: BLE001
                pass
        await super().close()
