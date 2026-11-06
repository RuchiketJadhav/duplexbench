"""M6-T06: generic self-hosted full-duplex adapter against a fake shim server."""
from __future__ import annotations

import asyncio
import json

import numpy as np

from _ws_fake import collect, ctx, fake_server, kinds
from duplexbench.adapters.selfhosted_fd import SelfHostedFD
from duplexbench.channel import mulaw

FRAME = mulaw.encode(np.full(160, 3000, np.int16))
OUT = np.full(480, 1200, "<i2").tobytes()


def _cfg(url, cancel=False):
    return {"model": "kyutai/moshiko-pytorch-bf16", "self_hosted": True, "cancel_event": cancel,
            "params": {"endpoint": url, "input_rate": 24000, "output_rate": 24000}}


def _server(seen, drop=False):
    async def handler(ws, path):
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "ready"}))
        if drop:
            await ws.recv()
            raise RuntimeError("gpu fell over")
        n = 0
        async for raw in ws:
            if isinstance(raw, str):
                seen.append(json.loads(raw))
                continue
            seen.append(len(raw))
            n += 1
            if n == 2:
                await ws.send(OUT)
                await ws.send(OUT)
                await ws.send(json.dumps({"type": "transcript", "text": "hey", "final": True}))
                await ws.send(json.dumps({"type": "cancel"}))
                await ws.close()
                return
    return handler


async def _run(cancel):
    seen = []
    async with fake_server(_server(seen)) as url:
        call = ctx()
        a = SelfHostedFD(call.clock, _cfg(url, cancel))
        await a.open(call)

        async def drive():
            call.turn_ended(call.clock.now_ns())
            for _ in range(4):
                await a.send_frame(FRAME, 0)
                await asyncio.sleep(0.02)

        evs = await collect(a, call, drive)
    return a, evs, seen


async def test_event_mapping_without_cancel_support():
    a, evs, seen = await _run(cancel=False)
    assert kinds(evs) == ["session_open", "first_audio_byte", "audio", "audio",
                          "output_transcript", "closed"]
    assert a.self_hosted is True and a.caps.cancel_event is False
    assert any(r[1] == "cancel" for r in a.raw_log)            # logged, not mapped (nothing faked)
    assert evs[2].payload["rate"] == 24000 and evs[2].payload["pcm"].size == 480
    assert seen[0] == {"type": "start", "sample_rate": 24000, "format": "pcm16le", "call_id": "call1"}
    assert all(isinstance(x, int) and x % 2 == 0 for x in seen[1:3])   # binary PCM16 frames


async def test_cancel_mapped_when_server_supports_it():
    a, evs, _ = await _run(cancel=True)
    assert "cancel_or_truncate" in kinds(evs) and a.caps.cancel_event is True


async def test_drop_yields_error_then_closed():
    seen = []
    async with fake_server(_server(seen, drop=True)) as url:
        call = ctx()
        a = SelfHostedFD(call.clock, _cfg(url))
        await a.open(call)

        async def drive():
            for _ in range(8):
                await a.send_frame(FRAME, 0)

        evs = await collect(a, call, drive)
    assert kinds(evs) == ["session_open", "error", "closed"]
