"""transport/phone_path.py (BUILD_PLAN M9-T01/T03): trunk legs, bridge, sign agreement."""
from __future__ import annotations

import pytest

from duplexbench import _loop
from duplexbench.clock import VirtualClock
from duplexbench.transport.phone_path import (LoopbackTrunk, TrunkClient, TwilioTrunk, bridge,
                                              caller_leg, disagreements, media_codec,
                                              sign_agreement, values_from_aggregate,
                                              with_baseline)

MS = 1_000_000


def _vrun(fn):
    async def main():
        clock = VirtualClock()
        return await clock.run(fn(clock))
    return _loop.run(main())


def test_loopback_trunk_is_a_trunk_client():
    async def go(clock):
        return isinstance(LoopbackTrunk(clock, 10), TrunkClient)
    assert _vrun(go)


def test_twilio_trunk_is_blocked_on_q5():
    with pytest.raises(NotImplementedError, match="Q5: trunk vendor not chosen"):
        TwilioTrunk()


def test_media_codec_lazy_import():
    mod = media_codec()
    assert mod is None or hasattr(mod, "encode_media")


def test_caller_leg_ear_and_bridge_marks_and_clear():
    """Caller frames reach the adapter; adapter audio reaches the ear one way later; marks
    echo back; a cancel clears undelivered audio and returns its pending marks."""
    got: list[bytes] = []

    async def go(clock):
        trunk = LoopbackTrunk(clock, delay_ms=30)

        async def to_adapter(p):
            got.append(p)

        async def agent_audio():
            await clock.sleep_until_ns(100 * MS)
            yield ("audio", b"\x01" * 160)
            await clock.sleep_until_ns(300 * MS)
            for _ in range(3):
                yield ("audio", b"\x02" * 160)
            yield ("cancel",)                                  # all three still in flight

        async def agent():
            return await bridge(clock, trunk, to_adapter=to_adapter, from_adapter=agent_audio())

        bt = clock.task(agent())
        sends, ear = await caller_leg(clock, trunk, [b"\xff" * 160] * 10, 20, tail_ms=400)
        stats = await bt
        await trunk.close()
        return sends, ear, stats

    sends, ear, stats = _vrun(go)
    assert len(sends) == 10 and len(got) == 10
    assert [e.t_ns for e in ear] == [130 * MS]                 # 100 ms + 30 ms one way
    assert ear[0].payload == b"\x01" * 160
    assert stats["audio"] == 4 and stats["clears"] == 1
    assert stats["marks_cleared"] == 3 and stats["marks_played"] == 1


def test_sign_agreement_table():
    live = {"yield_rate@1000": {"A": 0.9, "B": 0.6, "C": 0.5},
            "t_turn_gap_ear": {"A": 700.0, "B": 900.0}}
    phone = {"yield_rate@1000": {"A": 0.8, "B": 0.4, "C": 0.45},      # B vs C flips
             "t_turn_gap_ear": {"A": 950.0, "B": 1100.0}}
    t = sign_agreement(live, phone)
    y = t["yield_rate@1000"]
    assert y["n"] == 3 and y["agree"] == 2 and y["disagree"]
    bc = next(p for p in y["pairs"] if (p["a"], p["b"]) == ("B", "C"))
    assert not bc["agree"] and abs(bc["live_diff"] - 0.1) < 1e-12
    assert t["t_turn_gap_ear"] == {"pairs": t["t_turn_gap_ear"]["pairs"], "n": 1, "agree": 1,
                                   "share": 1.0, "disagree": False}
    assert disagreements(t) == ["yield_rate@1000"]


def test_values_from_aggregate_and_baseline():
    agg = {"metrics": {"pass_at_1": {"A": {"C0": {"value": 0.5}, "C1": {"value": 0.4}},
                                     "B": {"C0": {"value": 0.3}}}}}
    assert values_from_aggregate(agg) == {"pass_at_1": {"A": 0.5, "B": 0.3}}
    b = with_baseline(900.0, {"rtt_ms_median": 80.0})
    assert b["raw_ms"] == 900.0 and b["baseline_subtracted_ms"] == 820.0
    assert b["labels"] == ("raw", "baseline-subtracted")
