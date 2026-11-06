"""Pipecat cascade baseline over a Twilio-protocol WebSocket (BUILD_PLAN M6-T01/M6-T05; spec §2.2).

The cascade (streaming STT -> end-of-turn -> LLM -> streaming TTS) runs locally as a
Pipecat pipeline behind `TwilioFrameSerializer` (docs/providers.md §6). The harness
is the carrier (D2): it opens the pipeline's WebSocket, sends Twilio `connected` +
`start`, streams `media` frames, and maps what the serializer sends back:

    harness event          <- pipecat / Twilio event                    status
    audio                  <- `media` (b64 μ-law 8 kHz)                  confirmed (serializer source)
    cancel_or_truncate     <- `clear` (sent on InterruptionFrame)        confirmed (serializer source)
    first_audio_byte       <- base class, first `audio` after turn end   harness rule
    (log only)             <- `mark`                                     serializer never sends it
    tool_call              <- none on the wire: the pipeline calls the harness ToolServer over
                              HTTP (`/tools/{call_token}/<tool>`); the adapter only logs those
                              calls through the `tool_events` hook. It never emits `tool_call`
                              (the runner would invoke the tool a second time).
    output_transcript, usage <- not on the Twilio wire (caps say so)

`TwilioAgentAdapter` is the shared Twilio-protocol adapter; `DograhStack`
(adapters/dograh_stack.py) subclasses it. `components_only=False`: the pipeline is
a real end-to-end system, not a component re-assembly.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from websockets.exceptions import ConnectionClosed

from duplexbench.adapters._convert import InputConverter
from duplexbench.adapters.ws_adapter import WsAdapter
from duplexbench.transport.live_stream import Connector
from duplexbench.adapters.base import AdapterCaps, AdapterEvent
from duplexbench.channel import mulaw
from duplexbench.transport import twilio_media as tm

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

REQUIRED_COMPONENTS = ("stt", "llm", "tts")


class ToolEvents(Protocol):
    """What the adapter needs from the harness ToolServer (`ToolServer.subscribe`)."""

    def subscribe(self, token: str, cb: Any) -> None: ...


class TwilioAgentAdapter(WsAdapter):
    """Harness-as-carrier over Twilio Media Streams (shared by pipecat_cascade, dograh_stack)."""

    name = "twilio_agent"
    caps = AdapterCaps(cancel_event=True, native_mulaw=True, output_transcript=False,
                       usage_fields=False)

    def __init__(self, clock: "Clock", config: dict, *, connector: Connector | None = None,
                 tool_events: ToolEvents | None = None):
        super().__init__(clock, config, connector)
        self.tool_events = tool_events
        self.stream: tm.MediaStreamClient | None = None
        self.conv: InputConverter | None = None
        self.marks: list[tuple[int, str | None]] = []
        self.tool_log: list[tuple[str, dict]] = []      # ("start"|"result", info) from ToolServer
        self.inbound: dict[str, int] = {}               # Twilio event -> count received
        self._t_open = 0

    # -- hooks for subclasses --------------------------------------------------------
    async def _ws_url(self, call: "CallContext") -> str:
        return str(self.acfg["endpoint"])

    def _call_sid(self, call: "CallContext") -> str:
        return call.tool_token          # Dograh: callSid -> {{call_id}} in tool URLs

    def _custom_parameters(self, call: "CallContext") -> dict[str, str]:
        return {"call_token": call.tool_token, **{str(k): str(v) for k, v in
                                                  (self.acfg.get("custom_parameters") or {}).items()}}

    # -- Adapter API --------------------------------------------------------------------
    async def open(self, call: "CallContext") -> None:
        self.call = call
        self.conv = InputConverter(call, native_mulaw=True)
        url = await self._ws_url(call)
        ws = await self._connect(url, self.acfg.get("headers") or {})
        self.stream = tm.MediaStreamClient(ws, self._call_sid(call),
                                           custom_parameters=self._custom_parameters(call))
        await self.stream.handshake()
        self._t_open = self.clock.now_ns()
        if self.tool_events is not None:
            self.tool_events.subscribe(call.tool_token, self.on_tool_event)

    def on_tool_event(self, kind: str, info: dict) -> None:
        self.tool_log.append((kind, dict(info)))

    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        if self.stream is None or self._closing or self.conv is None:
            return
        try:
            await self.stream.send_audio(self.conv.to_mulaw8k(mulaw_bytes))
        except ConnectionClosed:
            pass    # the receive loop reports the drop (error -> closed) exactly once

    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        return None     # tools are served over HTTP by the ToolServer, not through this socket

    def _initial_events(self) -> Iterable[AdapterEvent]:
        yield AdapterEvent("session_open", self._t_open, "twilio.start",
                           {"stream_sid": self.stream.stream_sid if self.stream else None,
                            "call_sid": self.stream.call_sid if self.stream else None})

    def _map(self, msg: str | bytes, t_ns: int) -> Iterable[AdapterEvent]:
        try:
            m = tm.decode(msg)
        except tm.TwilioProtocolError as e:
            self.raw_log.append((t_ns, f"undecodable: {e}"))
            return
        self.inbound[m.event] = self.inbound.get(m.event, 0) + 1
        if m.event == "media" and m.payload:
            yield AdapterEvent("audio", t_ns, "media",
                               {"pcm": mulaw.decode(m.payload), "rate": tm.SAMPLE_RATE})
        elif m.event == "clear":
            yield AdapterEvent("cancel_or_truncate", t_ns, "clear", {})
        elif m.event == "mark":
            self.marks.append((t_ns, m.mark_name))
        else:
            self.raw_log.append((t_ns, m.event))

    async def close(self) -> None:
        if self.stream is not None and not self._closing:
            try:
                await self.stream.stop()
            except Exception:  # noqa: BLE001 - the socket may already be gone
                pass
        await super().close()


class PipecatCascade(TwilioAgentAdapter):
    """A locally run Pipecat STT -> LLM -> TTS pipeline (spec §2.2 cascade baseline)."""

    name = "pipecat_cascade"

    def __init__(self, clock: "Clock", config: dict, *, connector: Connector | None = None,
                 tool_events: ToolEvents | None = None):
        super().__init__(clock, config, connector=connector, tool_events=tool_events)
        comps = self.acfg.get("components") or {}
        missing = [c for c in REQUIRED_COMPONENTS if not comps.get(c)]
        if missing:
            raise ValueError(f"pipecat_cascade: params.components lacks {missing} "
                             "(spec §2.1: each component and version is recorded)")
        if not self.acfg.get("endpoint"):
            raise ValueError("pipecat_cascade: params.endpoint is required")
        self.components = comps
