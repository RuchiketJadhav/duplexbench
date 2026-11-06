"""OpenAI Realtime adapter event mapping against an in-memory socket (M3-T03). No network, no key."""
from __future__ import annotations

import asyncio
import base64
import json

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosedError

from duplexbench.adapters.openai_realtime import DEFAULT_MODEL, OpenAIRealtimeAdapter
from duplexbench.channel import mulaw
from duplexbench.clock import RealClock
from duplexbench.context import CallContext
from duplexbench.systems import load_system

_END = object()


class FakeWs:
    """WsLike: `push` provider events; `sent` holds every client message (decoded JSON)."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.close_code = None

    def push(self, obj) -> None:
        self.q.put_nowait(obj if isinstance(obj, (str, bytes, BaseException)) or obj is _END
                          else json.dumps(obj))

    async def send(self, m) -> None:
        if self.closed:
            raise ConnectionClosedError(None, None)
        self.sent.append(json.loads(m))
        if self.sent[-1]["type"] == "session.update":
            self.push({"type": "session.updated", "session": self.sent[-1]["session"]})

    async def close(self) -> None:
        self.closed = True
        self.close_code = 1000
        self.q.put_nowait(_END)

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.q.get()
        if m is _END:
            raise StopAsyncIteration
        if isinstance(m, BaseException):
            raise m
        return m

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]


class Cond:
    name, rate, codec = "C0", 8000, True

    def __str__(self) -> str:
        return self.name


def _ctx(clock) -> CallContext:
    return CallContext(run_id="r", call_id="c", scenario=None, trial=0, condition=Cond(),
                       system_id="openai_realtime", is_control=False, clock=clock, store=None,
                       tool_token="tok")


async def _open(tool_schemas=None, cond=None):
    clock = RealClock()
    ws = FakeWs()

    async def connector(url, headers):
        connector.url, connector.headers = url, headers
        return ws

    a = OpenAIRealtimeAdapter(clock, load_system("openai_realtime"), api_key="test-key",
                              connector=connector, tool_schemas=tool_schemas)
    ctx = _ctx(clock)
    if cond is not None:
        ctx.condition = cond
    await a.open(ctx)
    return a, ws, ctx, connector


async def _collect(a, ctx, n: int, timeout: float = 2.0) -> list:
    out = []
    agen = a.safe_events(ctx)
    try:
        while len(out) < n:
            out.append(await asyncio.wait_for(agen.__anext__(), timeout))
    except (StopAsyncIteration, asyncio.TimeoutError):
        pass
    return out


def _delta(item: str, pcm: np.ndarray, rid: str = "resp_1") -> dict:
    return {"type": "response.output_audio.delta", "response_id": rid, "item_id": item,
            "content_index": 0, "delta": base64.b64encode(mulaw.encode(pcm)).decode()}


async def test_open_sends_session_update_pcmu_and_turn_detection():
    a, ws, ctx, conn = await _open()
    assert DEFAULT_MODEL == "gpt-realtime-2.1"
    assert conn.url.endswith("?model=gpt-realtime-2.1")
    assert conn.headers["Authorization"] == "Bearer test-key"
    su = ws.sent[0]
    assert su["type"] == "session.update"
    audio = su["session"]["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcmu"}
    assert audio["output"]["format"] == {"type": "audio/pcmu"}
    assert audio["input"]["turn_detection"]["type"] == "server_vad"
    assert audio["input"]["turn_detection"]["interrupt_response"] is True
    assert "reasoning" not in su["session"]          # unconfirmed field never sent by default
    evs = await _collect(a, ctx, 1)
    assert evs[0].kind == "session_open" and evs[0].provider_event == "session.updated"
    await a.close()


async def test_frames_are_appended_as_native_mulaw():
    a, ws, ctx, _ = await _open()
    frame = mulaw.encode(np.full(160, 1000, np.int16))
    await a.send_frame(frame, 0)
    app = ws.sent[-1]
    assert app["type"] == "input_audio_buffer.append"
    assert base64.b64decode(app["audio"]) == frame     # passed through, no transcoding
    await a.close()


async def test_audio_delta_decodes_base64_mulaw_to_int16():
    a, ws, ctx, _ = await _open()
    pcm = (np.sin(np.arange(400) / 5) * 8000).astype(np.int16)
    ws.push({"type": "response.created", "response": {"id": "resp_1"}})
    ws.push(_delta("item_1", pcm))
    evs = await _collect(a, ctx, 2)
    audio = [e for e in evs if e.kind == "audio"]
    assert len(audio) == 1
    got = audio[0].payload["pcm"]
    assert got.dtype == np.int16 and audio[0].payload["rate"] == 8000
    np.testing.assert_array_equal(got, mulaw.decode(mulaw.encode(pcm)))
    await a.close()


async def test_speech_started_cancels_flushes_locally_and_truncates():
    a, ws, ctx, _ = await _open()
    ws.push({"type": "response.created", "response": {"id": "resp_1"}})
    ws.push(_delta("item_1", np.full(800, 3000, np.int16)))           # 100 ms received
    ws.push({"type": "input_audio_buffer.speech_started", "audio_start_ms": 900,
             "item_id": "in_2"})
    ws.push(_delta("item_1", np.full(800, 3000, np.int16)))           # in flight: dropped
    ws.push({"type": "response.done", "response": {"id": "resp_1", "status": "cancelled",
                                                   "status_details": {"reason": "turn_detected"},
                                                   "usage": {"input_tokens": 0}}})
    evs = await _collect(a, ctx, 5, timeout=0.5)
    kinds = [e.kind for e in evs]
    assert kinds.count("audio") == 1                       # second delta flushed locally
    assert kinds.count("cancel_or_truncate") == 1          # response.done{cancelled} not doubled
    cancel = next(e for e in evs if e.kind == "cancel_or_truncate")
    assert cancel.provider_event == "input_audio_buffer.speech_started"
    assert a.dropped_deltas == 1
    tr = [m for m in ws.sent if m["type"] == "conversation.item.truncate"]
    assert len(tr) == 1
    assert tr[0]["item_id"] == "item_1" and tr[0]["content_index"] == 0
    assert 0 <= tr[0]["audio_end_ms"] <= 100              # never past the audio received
    assert "usage" in kinds
    await a.close()


async def test_speech_started_while_agent_silent_is_not_a_cancel():
    a, ws, ctx, _ = await _open()
    ws.push({"type": "input_audio_buffer.speech_started", "audio_start_ms": 0, "item_id": "i"})
    evs = await _collect(a, ctx, 2, timeout=0.3)
    assert [e.kind for e in evs] == ["session_open"]
    assert "conversation.item.truncate" not in ws.types()
    await a.close()


async def test_tool_call_routes_through_tool_client_to_function_call_output():
    from duplexbench.tools.mock_backends.account import AccountBackend
    from duplexbench.tools.server import ToolServer

    server = ToolServer()
    clock = RealClock()
    server.register_call("tok", AccountBackend(), [], None, clock)
    client = server.client("tok")
    a, ws, ctx, _ = await _open(tool_schemas=client.tools())
    tools = ws.sent[0]["session"]["tools"]
    assert {t["name"] for t in tools} >= {"get_account"} and tools[0]["type"] == "function"
    ws.push({"type": "response.function_call_arguments.done", "call_id": "call_9",
             "name": "get_account", "arguments": json.dumps({"customer_id": "C-1"}),
             "item_id": "fc_1", "response_id": "resp_1"})
    evs = await _collect(a, ctx, 2)
    tc = next(e for e in evs if e.kind == "tool_call")
    assert tc.payload["id"] == "call_9" and tc.payload["arguments"] == {"customer_id": "C-1"}
    result = await client.call(tc.payload["name"], tc.payload["arguments"],
                               tool_call_id=tc.payload["id"])
    await a.tool_result(tc.payload["id"], result)
    assert ws.types()[-2:] == ["conversation.item.create", "response.create"]
    item = ws.sent[-2]["item"]
    assert item["type"] == "function_call_output" and item["call_id"] == "call_9"
    assert json.loads(item["output"]) == result
    await a.close()


async def test_transcript_usage_and_errors():
    a, ws, ctx, _ = await _open()
    ws.push({"type": "response.output_audio_transcript.delta", "delta": "hel", "item_id": "i"})
    ws.push({"type": "response.output_audio_transcript.done", "transcript": "hello", "item_id": "i"})
    usage = {"input_tokens": 10, "output_tokens": 5,
             "input_token_details": {"audio_tokens": 10}, "output_token_details": {"audio_tokens": 5}}
    ws.push({"type": "response.done", "response": {"id": "r1", "status": "completed",
                                                   "usage": usage}})
    a._truncate_ids.add("db_trunc_x")
    ws.push({"type": "error", "error": {"type": "invalid_request_error", "event_id": "db_trunc_x",
                                        "message": "already complete"}})
    ws.push({"type": "error", "error": {"type": "server_error", "message": "boom"}})
    evs = await _collect(a, ctx, 6, timeout=0.5)
    tr = [e for e in evs if e.kind == "output_transcript"]
    assert [(e.payload["text"], e.payload["final"]) for e in tr] == [("hel", False), ("hello", True)]
    u = next(e for e in evs if e.kind == "usage")
    assert u.payload["usage"] == usage and a.usage_log == [usage]
    errs = [e for e in evs if e.kind == "error"]
    assert len(errs) == 1 and errs[0].payload["message"] == "boom"   # truncate race -> warning
    assert a.warnings and a.warnings[0]["event_id"] == "db_trunc_x"
    await a.close()


async def test_socket_drop_yields_error_then_closed_never_raises():
    a, ws, ctx, _ = await _open()
    ws.push(ConnectionClosedError(None, None))
    evs = await _collect(a, ctx, 5)
    assert [e.kind for e in evs] == ["session_open", "error", "closed"]
    assert evs[1].payload["type"] == "transport"
    await a.send_frame(mulaw.encode(np.zeros(160, np.int16)), 0)    # dropped, not raised
    assert a.stream.dropped_sends == 1
    await a.close()


async def test_open_fails_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = OpenAIRealtimeAdapter(RealClock(), load_system("openai_realtime"))
    with pytest.raises(RuntimeError, match="no API key"):
        await a.open(_ctx(RealClock()))


async def test_wideband_condition_uses_pcm24k():
    class W(Cond):
        name, rate, codec = "W", 16000, False

    a, ws, ctx, _ = await _open(cond=W())
    assert ws.sent[0]["session"]["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    for _ in range(5):
        await a.send_frame(np.zeros(320, np.int16).tobytes(), 0)
    assert any(m["type"] == "input_audio_buffer.append" for m in ws.sent)
    await a.close()
