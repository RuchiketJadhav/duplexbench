"""M6-T03: Gemini Live adapter against a fake BidiGenerateContent server (shapes per providers.md §4)."""
from __future__ import annotations

import asyncio
import base64
import json

import numpy as np

from _ws_fake import collect, ctx, fake_server, kinds
from duplexbench.adapters.gemini_live import GeminiLive
from duplexbench.channel import mulaw

FRAME = mulaw.encode(np.full(160, 3000, np.int16))
TOOLS = [{"name": "get_account", "kind": "read", "description": "look up",
          "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}}}]
PCM24 = base64.b64encode(np.full(480, 1000, "<i2").tobytes()).decode()


def _cfg(url):
    return {"model": "gemini-3.8-live", "turn_detection": {"activityHandling": "START_OF_ACTIVITY_INTERRUPTS"},
            "params": {"endpoint": url}}


def _audio_msg():
    return {"serverContent": {"modelTurn": {"parts": [
        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": PCM24}}]}}}


def _server(seen, drop=False):
    async def handler(ws, path):
        seen.append(("path", path))
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"setupComplete": {}}))
        if drop:
            first = json.loads(await ws.recv())
            seen.append(first)
            raise RuntimeError("boom")
        n = 0
        async for raw in ws:
            m = json.loads(raw)
            seen.append(m)
            if "realtimeInput" in m:
                n += 1
                if n == 2:
                    await ws.send(json.dumps(_audio_msg()))
                    await ws.send(json.dumps(_audio_msg()).encode())     # binary JSON frame too
                    await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "hi"}}}))
                    await ws.send(json.dumps({"toolCall": {"functionCalls": [
                        {"id": "fc1", "name": "get_account", "args": {"customer_id": "C-1"}}]}}))
                if n == 4:
                    await ws.send(json.dumps({"serverContent": {"interrupted": True}}))
                    await ws.send(json.dumps({"usageMetadata": {"totalTokenCount": 42}}))
            if "toolResponse" in m:
                await ws.close()
                return
    return handler


async def test_event_mapping_resample_tools_usage():
    seen = []
    async with fake_server(_server(seen)) as url:
        call = ctx()
        a = GeminiLive(call.clock, _cfg(url), tool_schemas=TOOLS, api_key="dummy")
        await a.open(call)
        evs = []

        async def drive():
            call.turn_ended(call.clock.now_ns())
            for _ in range(6):
                await a.send_frame(FRAME, 0)
                await asyncio.sleep(0.02)
            while not any(e.kind == "tool_call" for e in evs):
                await asyncio.sleep(0.01)
            await a.tool_result("fc1", {"balance": 3})

        async def recv():
            async for ev in a.safe_events(call):
                evs.append(ev)

        t = asyncio.create_task(recv())
        await drive()
        await asyncio.wait_for(t, 10)
        await a.close()
    k = kinds(evs)
    assert k[0] == "session_open" and k[1] == "first_audio_byte" and k[-1] == "closed"
    assert k.count("audio") == 2 and "error" not in k
    for want in ("output_transcript", "tool_call", "cancel_or_truncate", "usage"):
        assert want in k
    au = [e for e in evs if e.kind == "audio"][0]
    assert au.payload["rate"] == 24000 and au.payload["pcm"].size == 480
    tc = [e for e in evs if e.kind == "tool_call"][0]
    assert tc.payload == {"id": "fc1", "name": "get_account", "arguments": {"customer_id": "C-1"}}
    assert [e for e in evs if e.kind == "usage"][0].payload["totalTokenCount"] == 42
    setup = seen[1]["setup"]
    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["tools"][0]["functionDeclarations"][0]["name"] == "get_account"
    assert setup["realtimeInputConfig"]["activityHandling"] == "START_OF_ACTIVITY_INTERRUPTS"
    assert "key=dummy" in seen[0][1]
    rt = [m for m in seen if isinstance(m, dict) and "realtimeInput" in m]
    assert rt[0]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    total = sum(len(base64.b64decode(m["realtimeInput"]["audio"]["data"])) // 2 for m in rt)
    assert 0 < total <= 6 * 320                       # 8 kHz -> 16 kHz, resampler hold-back
    resp = [m for m in seen if isinstance(m, dict) and "toolResponse" in m][0]
    assert resp["toolResponse"]["functionResponses"][0] == {
        "id": "fc1", "name": "get_account", "response": {"balance": 3}}
    assert not a.caps.native_mulaw


async def test_drop_yields_error_then_closed():
    seen = []
    async with fake_server(_server(seen, drop=True)) as url:
        call = ctx()
        a = GeminiLive(call.clock, _cfg(url), api_key="")

        await a.open(call)

        async def drive():
            await asyncio.sleep(0.05)
            for _ in range(3):
                await a.send_frame(FRAME, 0)

        evs = await collect(a, call, drive)
    assert kinds(evs) == ["session_open", "error", "closed"]
