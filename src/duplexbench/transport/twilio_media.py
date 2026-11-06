"""Twilio Media Streams codec + carrier-side client (BUILD_PLAN M6-T02, C3).

One codec for every Twilio-protocol path: `adapters/dograh_stack.py`,
`adapters/pipecat_cascade.py` and, later, `transport/phone_path.py`.
Message shapes are the ones on https://www.twilio.com/docs/voice/media-streams/websocket-messages
(summarised in docs/providers.md §B, checked 2026-11-04):

    carrier -> app   connected, start, media, mark, stop, dtmf
    app -> carrier   media, mark, clear

`sequenceNumber`, `chunk` and `timestamp` are strings on the wire. `mediaFormat`
is always audio/x-mulaw, 8000 Hz, 1 channel. The harness acts as the carrier
(D2): `MediaStreamClient` sends `connected` + `start`, then `media` frames, and
receives `media` / `mark` / `clear` from the agent side.

Harness mapping (docs/dograh-ingress.md §2, reference-notes pipecat serializer):
    media  -> "audio"               (base64 μ-law 8 kHz, decoded by the adapter)
    clear  -> "cancel_or_truncate"  (pipecat sends it on InterruptionFrame)
    mark   -> logged only           (Dograh never sends it)
"""
from __future__ import annotations

import base64
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

ENCODING = "audio/x-mulaw"
SAMPLE_RATE = 8000
CHANNELS = 1
BYTES_PER_MS = SAMPLE_RATE // 1_000        # μ-law: one byte per sample
PROTOCOL = "Call"
VERSION = "1.0.0"
PLACEHOLDER_ACCOUNT_SID = "AC" + "0" * 32

Event = Literal["connected", "start", "media", "mark", "clear", "stop", "dtmf"]
EVENTS: tuple[str, ...] = ("connected", "start", "media", "mark", "clear", "stop", "dtmf")

# harness EventKind for each inbound (agent -> carrier) Twilio event; None = log only
HARNESS_KIND: dict[str, str | None] = {"media": "audio", "clear": "cancel_or_truncate",
                                       "mark": None}


class TwilioProtocolError(ValueError):
    """A frame that is not a Twilio Media Streams message."""


def _dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def new_stream_sid() -> str:
    return "MZ" + secrets.token_hex(16)


def media_format() -> dict:
    return {"encoding": ENCODING, "sampleRate": SAMPLE_RATE, "channels": CHANNELS}


# -- encoders ---------------------------------------------------------------------------
def encode_connected(protocol: str = PROTOCOL, version: str = VERSION) -> str:
    return _dumps({"event": "connected", "protocol": protocol, "version": version})


def encode_start(stream_sid: str, call_sid: str, *, account_sid: str = PLACEHOLDER_ACCOUNT_SID,
                 custom_parameters: dict[str, str] | None = None,
                 tracks: tuple[str, ...] = ("inbound",), seq: int = 1) -> str:
    return _dumps({"event": "start", "sequenceNumber": str(seq), "streamSid": stream_sid,
                   "start": {"streamSid": stream_sid, "callSid": call_sid,
                             "accountSid": account_sid, "tracks": list(tracks),
                             "mediaFormat": media_format(),
                             "customParameters": dict(custom_parameters or {})}})


def encode_media(stream_sid: str, payload: bytes, *, seq: int | None = None,
                 chunk: int | None = None, timestamp_ms: int | None = None,
                 track: str | None = None) -> str:
    """Carrier form when `seq`/`chunk`/`timestamp_ms`/`track` are given; app form
    (`{"event","streamSid","media":{"payload"}}`) when they are all None."""
    media: dict[str, Any] = {}
    if track is not None:
        media["track"] = track
    if chunk is not None:
        media["chunk"] = str(chunk)
    if timestamp_ms is not None:
        media["timestamp"] = str(timestamp_ms)
    media["payload"] = base64.b64encode(bytes(payload)).decode("ascii")
    msg: dict[str, Any] = {"event": "media"}
    if seq is not None:
        msg["sequenceNumber"] = str(seq)
    msg["streamSid"] = stream_sid
    msg["media"] = media
    return _dumps(msg)


def encode_mark(stream_sid: str, name: str, *, seq: int | None = None) -> str:
    msg: dict[str, Any] = {"event": "mark"}
    if seq is not None:
        msg["sequenceNumber"] = str(seq)
    msg.update(streamSid=stream_sid, mark={"name": name})
    return _dumps(msg)


def encode_clear(stream_sid: str) -> str:
    return _dumps({"event": "clear", "streamSid": stream_sid})


def encode_stop(stream_sid: str, call_sid: str, *, account_sid: str = PLACEHOLDER_ACCOUNT_SID,
                seq: int = 1) -> str:
    return _dumps({"event": "stop", "sequenceNumber": str(seq), "streamSid": stream_sid,
                   "stop": {"accountSid": account_sid, "callSid": call_sid}})


def encode_dtmf(stream_sid: str, digit: str, *, seq: int = 1, track: str = "inbound") -> str:
    return _dumps({"event": "dtmf", "streamSid": stream_sid, "sequenceNumber": str(seq),
                   "dtmf": {"track": track, "digit": str(digit)}})


# -- decoder ----------------------------------------------------------------------------
@dataclass
class TwilioMessage:
    event: str
    raw: dict
    stream_sid: str | None = None
    seq: int | None = None
    payload: bytes | None = None          # media: decoded μ-law bytes
    track: str | None = None
    chunk: int | None = None
    timestamp_ms: int | None = None
    mark_name: str | None = None
    digit: str | None = None
    call_sid: str | None = None
    account_sid: str | None = None
    custom_parameters: dict = field(default_factory=dict)
    media_format: dict | None = None
    tracks: list = field(default_factory=list)
    protocol: str | None = None
    version: str | None = None

    @property
    def harness_kind(self) -> str | None:
        return HARNESS_KIND.get(self.event)


def _int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def decode(msg: str | bytes | dict) -> TwilioMessage:
    if isinstance(msg, dict):
        obj = msg
    else:
        try:
            obj = json.loads(msg)
        except (ValueError, UnicodeDecodeError) as e:
            raise TwilioProtocolError(f"not JSON: {e}") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("event"), str):
        raise TwilioProtocolError("no 'event' field")
    ev = obj["event"]
    m = TwilioMessage(event=ev, raw=obj, stream_sid=obj.get("streamSid"),
                      seq=_int(obj.get("sequenceNumber")))
    if ev == "connected":
        m.protocol, m.version = obj.get("protocol"), obj.get("version")
    elif ev == "start":
        st = obj.get("start") or {}
        m.stream_sid = st.get("streamSid", m.stream_sid)
        m.call_sid, m.account_sid = st.get("callSid"), st.get("accountSid")
        m.custom_parameters = dict(st.get("customParameters") or {})
        m.media_format = st.get("mediaFormat")
        m.tracks = list(st.get("tracks") or [])
    elif ev == "media":
        md = obj.get("media") or {}
        try:
            m.payload = base64.b64decode(md.get("payload", ""), validate=True)
        except (ValueError, TypeError) as e:
            raise TwilioProtocolError(f"bad media payload: {e}") from None
        m.track, m.chunk = md.get("track"), _int(md.get("chunk"))
        m.timestamp_ms = _int(md.get("timestamp"))
    elif ev == "mark":
        m.mark_name = (obj.get("mark") or {}).get("name")
    elif ev == "stop":
        st = obj.get("stop") or {}
        m.call_sid, m.account_sid = st.get("callSid"), st.get("accountSid")
    elif ev == "dtmf":
        d = obj.get("dtmf") or {}
        m.digit, m.track = d.get("digit"), d.get("track")
    return m


def split_frames(mulaw_bytes: bytes, frame_ms: int) -> Iterator[bytes]:
    """Cut μ-law audio into `frame_ms` frames (the last one may be short)."""
    n = BYTES_PER_MS * frame_ms
    for i in range(0, len(mulaw_bytes), n):
        yield mulaw_bytes[i:i + n]


# -- carrier-side client ----------------------------------------------------------------
class MediaStreamClient:
    """The harness as the Twilio carrier on one Media Streams WebSocket.

    `ws` is any object with async `send(str)` / `recv()` / `close()` (a
    `websockets` client connection in production, a fake in tests).
    """

    def __init__(self, ws: Any, call_sid: str, *, stream_sid: str | None = None,
                 account_sid: str = PLACEHOLDER_ACCOUNT_SID,
                 custom_parameters: dict[str, str] | None = None, track: str = "inbound"):
        self.ws = ws
        self.call_sid = call_sid
        self.stream_sid = stream_sid or new_stream_sid()
        self.account_sid = account_sid
        self.custom_parameters = dict(custom_parameters or {})
        self.track = track
        self.seq = 0                 # last sequenceNumber sent
        self.chunk = 0               # last media chunk sent (starts at 1)
        self.sent_bytes = 0          # μ-law bytes sent = samples at 8 kHz
        self.marks_received: list[str] = []
        self.started = False
        self.stopped = False

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    async def handshake(self) -> None:
        """`connected` then `start` (Dograh closes 4400 on any other order)."""
        await self.ws.send(encode_connected())
        await self.ws.send(encode_start(self.stream_sid, self.call_sid,
                                        account_sid=self.account_sid,
                                        custom_parameters=self.custom_parameters,
                                        tracks=(self.track,), seq=self._next_seq()))
        self.started = True

    async def send_audio(self, mulaw_bytes: bytes) -> None:
        """One `media` message; `timestamp` = ms of audio sent before this chunk."""
        if not self.started:
            raise TwilioProtocolError("send_audio before handshake")
        self.chunk += 1
        ts = self.sent_bytes // BYTES_PER_MS
        self.sent_bytes += len(mulaw_bytes)
        await self.ws.send(encode_media(self.stream_sid, mulaw_bytes, seq=self._next_seq(),
                                        chunk=self.chunk, timestamp_ms=ts, track=self.track))

    async def send_mark(self, name: str) -> None:
        await self.ws.send(encode_mark(self.stream_sid, name, seq=self._next_seq()))

    async def send_dtmf(self, digit: str) -> None:
        await self.ws.send(encode_dtmf(self.stream_sid, digit, seq=self._next_seq(),
                                       track=self.track))

    async def stop(self) -> None:
        if self.stopped or not self.started:
            return
        self.stopped = True
        await self.ws.send(encode_stop(self.stream_sid, self.call_sid,
                                       account_sid=self.account_sid, seq=self._next_seq()))

    async def recv(self) -> TwilioMessage:
        m = decode(await self.ws.recv())
        if m.event == "mark" and m.mark_name is not None:
            self.marks_received.append(m.mark_name)
        return m

    async def messages(self) -> AsyncIterator[TwilioMessage]:
        while True:
            yield await self.recv()
