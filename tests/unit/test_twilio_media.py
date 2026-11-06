"""M6-T02 (C3): Twilio Media Streams codec on the documented message shapes (providers.md §B)."""
from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from duplexbench.channel import mulaw
from duplexbench.transport import twilio_media as tm

SID = "MZ" + "1" * 32


class FakeWs:
    def __init__(self, inbound=()):
        self.sent: list[str] = []
        self.inbound = list(inbound)

    async def send(self, m):
        self.sent.append(m)

    async def recv(self):
        return self.inbound.pop(0)

    async def close(self):
        pass


def test_connected_shape():
    assert json.loads(tm.encode_connected()) == {"event": "connected", "protocol": "Call",
                                                 "version": "1.0.0"}


def test_start_shape_matches_twilio_docs():
    m = json.loads(tm.encode_start(SID, "tok-1", custom_parameters={"call_token": "tok-1"}))
    assert m["event"] == "start" and m["sequenceNumber"] == "1" and m["streamSid"] == SID
    st = m["start"]
    assert st["streamSid"] == SID and st["callSid"] == "tok-1"
    assert st["mediaFormat"] == {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
    assert st["tracks"] == ["inbound"] and st["customParameters"] == {"call_token": "tok-1"}
    assert st["accountSid"].startswith("AC")
    d = tm.decode(json.dumps(m))
    assert (d.call_sid, d.stream_sid, d.custom_parameters) == ("tok-1", SID, {"call_token": "tok-1"})


def test_media_carrier_and_app_forms():
    payload = bytes(range(160))
    c = json.loads(tm.encode_media(SID, payload, seq=4, chunk=2, timestamp_ms=20, track="inbound"))
    assert c == {"event": "media", "sequenceNumber": "4", "streamSid": SID,
                 "media": {"track": "inbound", "chunk": "2", "timestamp": "20",
                           "payload": base64.b64encode(payload).decode()}}
    a = json.loads(tm.encode_media(SID, payload))
    assert a == {"event": "media", "streamSid": SID,
                 "media": {"payload": base64.b64encode(payload).decode()}}
    d = tm.decode(json.dumps(c))
    assert d.payload == payload and (d.seq, d.chunk, d.timestamp_ms, d.track) == (4, 2, 20, "inbound")


def test_mark_clear_stop_dtmf_shapes():
    assert json.loads(tm.encode_mark(SID, "lbl", seq=4)) == {
        "event": "mark", "sequenceNumber": "4", "streamSid": SID, "mark": {"name": "lbl"}}
    assert json.loads(tm.encode_clear(SID)) == {"event": "clear", "streamSid": SID}
    s = json.loads(tm.encode_stop(SID, "CA1", seq=5))
    assert s["stop"]["callSid"] == "CA1" and s["sequenceNumber"] == "5"
    d = json.loads(tm.encode_dtmf(SID, "1", seq=5))
    assert d["dtmf"] == {"track": "inbound", "digit": "1"}
    assert tm.decode(tm.encode_dtmf(SID, "7")).digit == "7"
    assert tm.decode(tm.encode_mark(SID, "x")).mark_name == "x"
    assert tm.decode(tm.encode_stop(SID, "CA1")).call_sid == "CA1"


def test_clear_maps_to_cancel_or_truncate():
    assert tm.decode(tm.encode_clear(SID)).harness_kind == "cancel_or_truncate"
    assert tm.decode(tm.encode_media(SID, b"\xff")).harness_kind == "audio"
    assert tm.decode(tm.encode_mark(SID, "m")).harness_kind is None


def test_decode_rejects_garbage():
    with pytest.raises(tm.TwilioProtocolError):
        tm.decode("not json")
    with pytest.raises(tm.TwilioProtocolError):
        tm.decode('{"no_event": 1}')


async def test_round_trip_of_20ms_frames_through_client():
    t = np.arange(8000 // 5) / 8000                      # 200 ms of 440 Hz
    pcm = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    audio = mulaw.encode(pcm)
    frames = list(tm.split_frames(audio, 20))
    assert len(frames) == 10 and all(len(f) == 160 for f in frames)
    ws = FakeWs()
    cli = tm.MediaStreamClient(ws, "tok-9", stream_sid=SID)
    with pytest.raises(tm.TwilioProtocolError):
        await cli.send_audio(frames[0])
    await cli.handshake()
    for f in frames:
        await cli.send_audio(f)
    await cli.stop()
    msgs = [tm.decode(m) for m in ws.sent]
    assert [m.event for m in msgs[:2]] == ["connected", "start"]
    media = [m for m in msgs if m.event == "media"]
    assert [m.chunk for m in media] == list(range(1, 11))
    assert [m.timestamp_ms for m in media] == [20 * i for i in range(10)]
    assert [m.seq for m in msgs[1:]] == list(range(1, 13))       # start .. stop
    assert b"".join(m.payload for m in media) == audio
    assert msgs[-1].event == "stop"
    assert np.array_equal(mulaw.decode(b"".join(m.payload for m in media)), mulaw.decode(audio))


async def test_client_records_received_marks():
    ws = FakeWs([tm.encode_mark(SID, "a"), tm.encode_clear(SID)])
    cli = tm.MediaStreamClient(ws, "c", stream_sid=SID)
    assert (await cli.recv()).event == "mark"
    assert (await cli.recv()).harness_kind == "cancel_or_truncate"
    assert cli.marks_received == ["a"]
