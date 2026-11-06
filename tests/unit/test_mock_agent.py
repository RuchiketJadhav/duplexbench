"""M1-T12: mock agent adapter, driven only by the (virtual) clock."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from duplexbench.adapters.mock_agent import SHORT_REPLY_MS, MockAgent, MockProgram, babble_chunk
from duplexbench.channel import mulaw
from duplexbench.channel.chain import load_condition
from duplexbench.clock import VirtualClock
from duplexbench.context import CallContext
from duplexbench.metrics.activity import frame_rms_dbfs
from duplexbench.stimuli.caller_source import tone
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()
TURN_END_MS = 4_000
BURST_MS = 5_000


def _drive(program: MockProgram, burst_at_ms: int | None = BURST_MS, end_ms: int = 9_000,
           tool_result_after_ms: int | None = None):
    """Caller: silence, a turn end at 4.0 s, a 300 ms tone burst at `burst_at_ms`."""
    clock = VirtualClock()
    ag = MockAgent(clock, program, TH)
    ctx = CallContext("r", "c", None, 0, load_condition("C0", TH), "mock", False, clock, None, "t")
    evs = []

    async def consume():
        async for e in ag.safe_events(ctx):
            evs.append(e)
            if e.kind == "tool_call" and tool_result_after_ms is not None:
                async def reply(tid=e.payload["id"]):
                    await clock.sleep_until_ns(clock.now_ns() + tool_result_after_ms * MS)
                    await ag.tool_result(tid, {"ok": True})
                clock.task(reply())

    async def main():
        await ag.open(ctx)
        t = clock.task(consume())
        burst = mulaw.encode(tone(8000, 160, 660, -20))
        for k in range(end_ms // 20):
            now = k * 20 * MS
            await clock.sleep_until_ns(now)
            if k * 20 == TURN_END_MS:
                ctx.turn_ended(now)
            speech = burst_at_ms is not None and burst_at_ms <= k * 20 < burst_at_ms + 300
            await ag.send_frame(burst if speech else mulaw.SILENCE * 160, now)
        await ag.close()
        t.cancel()
        await clock.idle(asyncio.gather(t, return_exceptions=True))

    asyncio.run(clock.run(main()))
    return evs, ag


def _audio_t(evs):
    return [e.t_ns for e in evs if e.kind == "audio"]


def test_stops_cancels_250ms_after_the_burst_and_goes_silent():
    evs, ag = _drive(MockProgram(behaviour="stops"))
    cancels = [e.t_ns for e in evs if e.kind == "cancel_or_truncate"]
    assert cancels == [5_250 * MS]
    a = _audio_t(evs)
    assert a[0] == (TURN_END_MS + 400) * MS and max(a) < 5_250 * MS
    assert ag.heard[0] == BURST_MS * MS


def test_first_audio_byte_from_base_class():
    evs, _ = _drive(MockProgram(behaviour="stops"))
    fab = [e for e in evs if e.kind == "first_audio_byte"]
    assert len(fab) == 1 and fab[0].t_ns == 4_400 * MS and fab[0].payload["turn_end_ns"] == 4_000 * MS


def test_never_stops_keeps_emitting():
    evs, _ = _drive(MockProgram(behaviour="never_stops"))
    assert not [e for e in evs if e.kind == "cancel_or_truncate"]
    a = _audio_t(evs)
    assert len(a) == 3_000 // 20 and a[-1] == (4_400 + 3_000 - 20) * MS


def test_tool_call_at_1200_after_turn_end():
    evs, _ = _drive(MockProgram(behaviour="never_stops"))
    tc = [e for e in evs if e.kind == "tool_call"]
    assert len(tc) == 1 and tc[0].t_ns == (TURN_END_MS + 1_200) * MS
    assert set(tc[0].payload) == {"id", "name", "arguments"}


def test_tool_disabled():
    evs, _ = _drive(MockProgram(tool_at_ms=None))
    assert not [e for e in evs if e.kind == "tool_call"]


def test_short_reply():
    evs, _ = _drive(MockProgram(behaviour="short_reply"), burst_at_ms=None)
    a = _audio_t(evs)
    assert len(a) == SHORT_REPLY_MS // 20 and a[-1] < (4_400 + SHORT_REPLY_MS) * MS


def test_cancel_restart_restarts_20ms_later():
    evs, _ = _drive(MockProgram(behaviour="cancel_restart"))
    assert [e.t_ns for e in evs if e.kind == "cancel_or_truncate"] == [5_250 * MS]
    after = [t for t in _audio_t(evs) if t > 5_250 * MS]
    assert after[0] == 5_270 * MS and len(after) == 3_000 // 20     # the whole reply again


def test_silent_in_tool_waits_for_the_result():
    evs, _ = _drive(MockProgram(behaviour="silent_in_tool"), burst_at_ms=None,
                    tool_result_after_ms=2_000)
    a = _audio_t(evs)
    assert not [t for t in a if 5_200 * MS <= t < 7_200 * MS]
    assert 7_200 * MS in a


def test_filler_then_result_keeps_talking():
    evs, _ = _drive(MockProgram(behaviour="filler_then_result"), burst_at_ms=None,
                    tool_result_after_ms=2_000)
    a = set(_audio_t(evs))
    assert all(t in a for t in range(5_200 * MS, 7_200 * MS, 20 * MS))


def test_tone_is_440hz_at_minus_20_dbfs():
    evs, _ = _drive(MockProgram(behaviour="never_stops", rate=8000), burst_at_ms=None)
    pcm = np.concatenate([e.payload["pcm"] for e in evs if e.kind == "audio"][:50])
    assert abs(frame_rms_dbfs(pcm) - (-20.0)) < 0.1
    spec = np.abs(np.fft.rfft(pcm))
    assert abs(np.argmax(spec) * 8000 / len(pcm) - 440) < 2


def test_babble_level_and_continuity():
    x = babble_chunk(8000, 0, 16_000)
    assert abs(frame_rms_dbfs(x) - (-20.0)) < 0.5
    assert np.array_equal(np.concatenate([babble_chunk(8000, 0, 100), babble_chunk(8000, 100, 60)]),
                          babble_chunk(8000, 0, 160))


def test_noise_floor_does_not_count_as_speech():
    """A steady C1-like noise bed is not an interruption; a louder burst on top of it is."""
    clock = VirtualClock()
    ag = MockAgent(clock, MockProgram(), TH)
    rng = np.random.default_rng(1)
    noise = lambda: np.rint(rng.standard_normal(160) * 32768 * 10 ** (-35 / 20)).astype(np.int16)

    async def main():
        await ag.open(CallContext("r", "c", None, 0, load_condition("C0", TH), "m", False, clock,
                                  None, "t"))
        for k in range(100):
            await ag.send_frame(mulaw.encode(noise()), k * 20 * MS)
        n0 = len(ag.heard)
        await ag.send_frame(mulaw.encode(tone(8000, 160, 660, -20) + noise()), 100 * 20 * MS)
        return n0, len(ag.heard)

    n0, n1 = asyncio.run(clock.run(main()))
    assert n0 <= 1 and n1 == n0 + 1          # (the very first sound sets the floor)


@pytest.mark.parametrize("b", ["perfect", "stale_writer", "claims_success", "stops_on_everything"])
def test_other_behaviours_stop_like_stops(b):
    evs, _ = _drive(MockProgram(behaviour=b))
    assert [e.t_ns for e in evs if e.kind == "cancel_or_truncate"] == [5_250 * MS]
