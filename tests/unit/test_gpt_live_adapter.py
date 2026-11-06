"""M6-T04/T05: GPT-Live-1 adapter against a fake Live server (providers.md §1). No cancel event."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import numpy as np
import yaml

from _ws_fake import collect, ctx, fake_server, kinds
from duplexbench.adapters.gpt_live import GptLive
from duplexbench.channel import mulaw

FRAME = mulaw.encode(np.full(160, 3000, np.int16))
B64 = base64.b64encode(FRAME).decode()
ROOT = Path(__file__).resolve().parents[2]


def _cfg(url):
    return {"model": "gpt-live-1", "cancel_event": False, "params": {"endpoint": url}}


def _server(seen, drop=False):
    async def handler(ws, path):
        seen.append(("path", path))
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "session.started"}))
        if drop:
            await ws.recv()
            raise RuntimeError("boom")
        n = 0
        async for raw in ws:
            m = json.loads(raw)
            seen.append(m)
            if m["type"] == "session.input_audio.append":
                n += 1
                if n == 2:
                    for _ in range(2):
                        await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": B64}))
                    await ws.send(json.dumps({"type": "session.output_transcript.delta", "delta": "hi"}))
                    await ws.send(json.dumps({"type": "session.delegation.created",
                                              "delegation": {"id": "d1", "name": "get_account",
                                                             "arguments": {"customer_id": "C-1"},
                                                             "offset_ms": 40}}))
                    await ws.send(json.dumps({"type": "session.usage.updated", "usage": {"seconds": 1.5}}))
            elif m["type"] == "session.thinking.append":
                await ws.send(json.dumps({"type": "session.closed", "usage": {"seconds": 2.0}}))
                await ws.close()
                return
    return handler


async def test_event_mapping_no_cancel():
    seen = []
    async with fake_server(_server(seen)) as url:
        call = ctx()
        a = GptLive(call.clock, _cfg(url), api_key="dummy")
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
        await a.tool_result("d1", {"ok": True})
        await asyncio.wait_for(t, 10)
    assert kinds(evs) == ["session_open", "first_audio_byte", "audio", "audio", "output_transcript",
                          "tool_call", "usage", "usage", "closed"]
    assert "cancel_or_truncate" not in kinds(evs)
    assert a.caps.cancel_event is False and a.caps.native_mulaw is True
    assert evs[5].payload == {"id": "d1", "name": "get_account", "arguments": {"customer_id": "C-1"}}
    assert evs[7].payload["seconds"] == 2.0 and a.usage_seconds == 2.0
    assert evs[-1].provider_event == "session.closed"
    s = seen[1]
    assert s["type"] == "session.start"
    assert s["session"]["audio"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    app = [m for m in seen if isinstance(m, dict) and m.get("type") == "session.input_audio.append"]
    assert base64.b64decode(app[0]["audio"]) == FRAME


async def test_drop_yields_error_then_closed():
    seen = []
    async with fake_server(_server(seen, drop=True)) as url:
        call = ctx()
        a = GptLive(call.clock, _cfg(url), api_key="")
        await a.open(call)

        async def drive():
            await a.send_frame(FRAME, 0)

        evs = await collect(a, call, drive)
    assert kinds(evs) == ["session_open", "error", "closed"]


def test_yaml_declares_missing_cancel():
    cfg = yaml.safe_load((ROOT / "configs/systems/gpt_live.yaml").read_text(encoding="utf-8"))
    assert cfg["cancel_event"] is False and cfg["model"] == "gpt-live-1"
    assert cfg["params"]["endpoint"] == "wss://api.openai.com/v1/live/sessions"
