"""M6-T04: Grok Voice adapter against a fake Realtime-shaped server (providers.md §5)."""
from __future__ import annotations

import asyncio
import base64
import json

import numpy as np

from _ws_fake import collect, ctx, fake_server, kinds
from duplexbench.adapters.grok_voice import GrokVoice
from duplexbench.channel import mulaw

FRAME = mulaw.encode(np.full(160, 3000, np.int16))
B64 = base64.b64encode(FRAME).decode()
TOOLS = [{"name": "get_account", "kind": "read", "description": "", "parameters": {"type": "object"}}]


def _cfg(url):
    return {"model": "grok-voice-3.0", "reasoning_effort": "high",
            "turn_detection": {"type": "server_vad"}, "params": {"endpoint": url}}


def _server(seen, drop=False):
    async def handler(ws, path):
        seen.append(("path", path, ws.request.headers.get("Authorization")))
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "session.updated", "session": {}}))
        if drop:
            await ws.recv()
            raise RuntimeError("boom")
        n = 0
        async for raw in ws:
            m = json.loads(raw)
            seen.append(m)
            if m["type"] == "input_audio_buffer.append":
                n += 1
                if n == 1:                       # speech_started with nothing playing: not a cancel
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
                if n == 2:
                    await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": B64}))
                    await ws.send(json.dumps({"type": "response.audio.delta", "delta": B64}))
                    await ws.send(json.dumps({"type": "response.output_audio_transcript.done",
                                              "transcript": "hello"}))
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_started",
                                              "item_id": "it1"}))
                    await ws.send(json.dumps({"type": "response.function_call_arguments.done",
                                              "call_id": "c1", "name": "get_account",
                                              "arguments": "{\"customer_id\": \"C-1\"}"}))
                    await ws.send(json.dumps({"type": "response.done",
                                              "response": {"usage": {"total_tokens": 7}}}))
            elif m["type"] == "response.create":
                await ws.close()
                return
    return handler


async def test_event_mapping():
    seen = []
    async with fake_server(_server(seen)) as url:
        call = ctx()
        a = GrokVoice(call.clock, _cfg(url), tool_schemas=TOOLS, api_key="dummy")
        await a.open(call)
        evs = []

        async def recv():
            async for ev in a.safe_events(call):
                evs.append(ev)

        t = asyncio.create_task(recv())
        call.turn_ended(call.clock.now_ns())
        for _ in range(3):
            await a.send_frame(FRAME, 0)
            await asyncio.sleep(0.02)
        while not any(e.kind == "tool_call" for e in evs):
            await asyncio.sleep(0.01)
        await a.tool_result("c1", {"ok": True})
        await asyncio.wait_for(t, 10)
    assert kinds(evs) == ["session_open", "first_audio_byte", "audio", "audio", "output_transcript",
                          "cancel_or_truncate", "tool_call", "usage", "closed"]
    assert evs[2].payload["rate"] == 8000 and evs[2].payload["pcm"].size == 160
    assert evs[6].payload == {"id": "c1", "name": "get_account", "arguments": {"customer_id": "C-1"}}
    assert seen[0][1].endswith("?model=grok-voice-3.0") and seen[0][2] == "Bearer dummy"
    s = seen[1]["session"]
    assert s["audio"]["input"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    assert s["audio"]["output"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    assert s["reasoning"] == {"effort": "high"} and s["tools"][0]["type"] == "function"
    app = [m for m in seen if isinstance(m, dict) and m.get("type") == "input_audio_buffer.append"]
    assert base64.b64decode(app[0]["audio"]) == FRAME          # μ-law passes through unchanged
    out = [m for m in seen if isinstance(m, dict) and m.get("type") == "conversation.item.create"][0]
    assert out["item"]["type"] == "function_call_output" and json.loads(out["item"]["output"]) == {"ok": True}
    assert any(r[1] == "input_audio_buffer.speech_started" for r in a.raw_log)


async def test_drop_yields_error_then_closed():
    seen = []
    async with fake_server(_server(seen, drop=True)) as url:
        call = ctx()
        a = GrokVoice(call.clock, _cfg(url), api_key="")
        await a.open(call)

        async def drive():
            await a.send_frame(FRAME, 0)

        evs = await collect(a, call, drive)
    assert kinds(evs) == ["session_open", "error", "closed"]
