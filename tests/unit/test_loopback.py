"""transport/loopback_baseline.py (BUILD_PLAN M9-T02) on LoopbackTrunk under the VirtualClock."""
from __future__ import annotations

from duplexbench import _loop
from duplexbench.channel.chain import ChannelChain, load_condition
from duplexbench.clock import VirtualClock
from duplexbench.run_card_fill import fill
from duplexbench.storage import Store
from duplexbench.thresholds import Thresholds
from duplexbench.transport.loopback_baseline import probe_frames, run_loopback
from duplexbench.transport.phone_path import LoopbackTrunk

TH = Thresholds()


def _chain():
    return ChannelChain(load_condition("C0", TH), seed_fwd=1, seed_back=2)


def _run(delay_ms, jitter_ms=0.0, probes=10):
    async def main():
        clock = VirtualClock()

        async def go():
            trunk = LoopbackTrunk(clock, delay_ms, jitter_ms, seed=3)
            return await run_loopback(clock, trunk, _chain(), TH, probes=probes)
        return await clock.run(go())
    return _loop.run(main())


def test_probe_train_is_mulaw_line_frames():
    frames, idx = probe_frames(_chain(), 50, 25)
    assert len(frames) == 50 and idx == [0, 25]
    assert all(len(f) == _chain().frame_len for f in frames)       # 1 byte per sample (μ-law)


def test_fixed_delay_round_trip_is_twice_one_way():
    res = _run(40.0)
    assert res["n_probes"] == 10 and res["lost"] == 0
    assert res["rtt_ms"] == [80.0] * 10
    assert res["rtt_ms_median"] == 80.0 and res["jitter_ms_median"] == 0.0
    assert res["trunk"] == "loopback" and res["condition"] == "C0"


def test_jitter_is_measured():
    res = _run(40.0, jitter_ms=10.0, probes=20)
    assert res["lost"] == 0
    assert 80.0 <= res["rtt_ms_median"] <= 100.0
    assert res["jitter_ms_median"] > 0.0


def test_baseline_stored_in_run_card(tmp_path):
    import json
    import sqlite3
    p = tmp_path / "e.sqlite"
    s = Store(p)
    s.put("runs", {"run_id": "r", "started_ns": 0, "config_json": "{}", "hashes_json": "{}",
                   "calibration_json": None, "run_card_json": "{}"})
    s.close()
    res = _run(25.0, probes=4)
    fill(p, "r", loopback=res)
    card = json.loads(sqlite3.connect(p).execute("select run_card_json from runs").fetchone()[0])
    assert card["loopback_baseline"]["rtt_ms_median"] == 50.0
