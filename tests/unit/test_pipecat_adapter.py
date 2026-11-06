"""M6-T05: Pipecat cascade over the Twilio protocol, against a fake pipecat endpoint."""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from _ws_fake import collect, ctx, fake_server, kinds
from duplexbench.adapters.pipecat_cascade import PipecatCascade
from duplexbench.channel import mulaw
from duplexbench.transport import twilio_media as tm

FRAME = mulaw.encode(np.full(160, 4000, np.int16))
COMPONENTS = {"stt": {"vendor": "fake-stt", "version": "0"},
              "llm": {"vendor": "fake-llm", "version": "0"},
              "tts": {"vendor": "fake-tts", "version": "0"}}


def _cfg(url):
    return {"system_id": "pipecat_cascade", "adapter": "pipecat_cascade",
            "params": {"endpoint": url, "components": COMPONENTS}}


class Hook:
    def __init__(self):
        self.subs = []

    def subscribe(self, token, cb):
        self.subs.append((token, cb))


def _pipecat(seen, drop=False):
    async def handler(ws, path):
        seen.append(tm.decode(await ws.recv()))
        seen.append(tm.decode(await ws.recv()))
        sid = seen[1].stream_sid
        if drop:
            raise RuntimeError("pipeline crashed")
        n = 0
        async for raw in ws:
            m = tm.decode(raw)
            seen.append(m)
            if m.event != "media":
                continue
            n += 1
            if n == 1:
                for _ in range(5):
                    await ws.send(tm.encode_media(sid, FRAME))
            elif n == 3:
                await ws.send(tm.encode_clear(sid))
                await ws.send(tm.encode_mark(sid, "m1"))
            elif n == 5:
                await ws.close()
                return
    return handler


async def test_event_mapping_and_first_audio_byte():
    seen = []
    async with fake_server(_pipecat(seen)) as url:
        hook = Hook()
        call = ctx()
        a = PipecatCascade(call.clock, _cfg(url), tool_events=hook)
        await a.open(call)

        async def drive():
            call.turn_ended(call.clock.now_ns())
            for _ in range(5):
                await a.send_frame(FRAME, call.clock.now_ns())
                await asyncio.sleep(0.02)

        evs = await collect(a, call, drive)
        await a.close()
    assert kinds(evs) == (["session_open", "first_audio_byte"] + ["audio"] * 5
                          + ["cancel_or_truncate", "closed"])
    audio = [e for e in evs if e.kind == "audio"][0]
    assert audio.payload["rate"] == 8000 and audio.payload["pcm"].size == 160
    assert evs[-1].payload["by"] == "provider"
    assert [m.event for m in seen[:2]] == ["connected", "start"]
    assert seen[1].call_sid == "tok-abc" and seen[1].custom_parameters["call_token"] == "tok-abc"
    assert a.marks and a.marks[0][1] == "m1"
    assert hook.subs and hook.subs[0][0] == "tok-abc"
    a.on_tool_event("start", {"name": "get_account"})
    assert a.tool_log == [("start", {"name": "get_account"})]
    assert a.components["stt"]["vendor"] == "fake-stt"


async def test_drop_yields_error_then_closed():
    seen = []
    async with fake_server(_pipecat(seen, drop=True)) as url:
        call = ctx()
        a = PipecatCascade(call.clock, _cfg(url))
        await a.open(call)
        evs = await collect(a, call)
        await a.close()
    assert kinds(evs) == ["session_open", "error", "closed"]
    assert evs[1].payload["type"] == "transport"


async def test_pcm_delivery_is_reencoded_to_mulaw():
    """W condition (no codec): PCM16 @16 kHz in -> μ-law 8 kHz on the wire."""
    class Cond:
        codec, rate = False, 16000
    seen = []

    async def handler(ws, path):
        for _ in range(2):
            await ws.recv()
        for _ in range(3):
            seen.append(tm.decode(await ws.recv()))
        await ws.close()

    async with fake_server(handler) as url:
        call = ctx(condition=Cond())
        a = PipecatCascade(call.clock, _cfg(url))
        await a.open(call)

        async def drive():
            for _ in range(3):
                await a.send_frame(np.full(320, 3000, np.int16).tobytes(), 0)

        await collect(a, call, drive)
    assert all(m.event == "media" for m in seen)
    assert sum(len(m.payload) for m in seen) <= 3 * 160


def test_components_required():
    with pytest.raises(ValueError, match="components"):
        PipecatCascade(None, {"params": {"endpoint": "ws://x", "components": {"stt": {}}}})
    with pytest.raises(ValueError, match="endpoint"):
        PipecatCascade(None, {"params": {"components": COMPONENTS}})


def test_config_yaml_loads():
    import yaml
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "configs" / "systems" / "pipecat_cascade.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg["components_only"] is False and cfg["adapter"] == "pipecat_cascade"
    PipecatCascade(None, cfg)
    json.dumps(cfg)
