"""load/runner.py (BUILD_PLAN M9-T03): open-loop levels under the VirtualClock with mock calls."""
from __future__ import annotations

import asyncio
import json
import sqlite3

from duplexbench import _loop
from duplexbench.clock import VirtualClock
from duplexbench.load.runner import run_load
from duplexbench.storage import Store
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()


def _factory(slip_ms: int = 0, outcomes=None):
    """Mock call: 50 frames on the frame grid, each sent `slip_ms` late; latency = 400 + level."""
    async def call(ctx):
        clock, step = ctx.clock, ctx.th.frame_ms * MS
        t = ctx.started_ns
        for _ in range(50):
            t += step
            await clock.sleep_until_ns(t + slip_ms * MS)
            ctx.pace.record(t, clock.now_ns())
        out = (outcomes or {}).get((ctx.level, ctx.index), "completed")
        if out == "raise":
            raise ConnectionError("socket dropped")
        flags = {"http_status": 429} if out == "rate_limited" else {}
        return {"outcome": out, "latency_ms": 400.0 + ctx.level, "flags": flags}
    return call


def _run(coro_fn):
    async def main():
        clock = VirtualClock()
        return await clock.run(coro_fn(clock))
    return _loop.run(main())


def test_c4_level_rates_and_shift(tmp_path):
    outcomes = {(4, 0): "rate_limited", (4, 1): "raise", (4, 2): "disconnect"}

    async def go(clock):
        return await run_load(clock, _factory(0, outcomes), th=TH, levels=(1, 4),
                              calls_per_level=8, mean_session_s=1.0, seed=7,
                              out_dir=tmp_path, run_id="L")
    rep = _run(go)
    assert [s["level"] for s in rep["levels"]] == [1, 4]
    c1, c4 = rep["levels"]
    assert c1["n"] == c4["n"] == 8
    assert c1["error_rate"]["k"] == 0 and c1["latency_shift_ms"] == 0.0
    assert c4["error_rate"]["k"] == 3 and c4["rate_429"]["k"] == 1
    assert c4["disconnect_rate"]["k"] == 1
    assert c4["outcomes"]["transport_error"] == 1
    lo, hi = c4["rate_429"]["ci"]
    assert lo < 1 / 8 < hi
    # five completed + three failing calls at c=4 all report latency 404
    assert c4["latency_p50_ms"] == 404.0 and c4["latency_shift_ms"] == 3.0
    assert c4["pacing_slip_p99_ms"] == 0.0 and c4["loop_lag_p99_ms"] == 0.0
    assert not rep["load_invalid"]
    assert rep["sysmon"] is None                       # virtual clock: no SysMon
    assert json.loads((tmp_path / "load_L.json").read_text())["levels"][1]["n"] == 8


def test_open_loop_overlap_at_c4():
    """Open loop: at c=4 calls overlap in virtual time (arrivals do not wait for completions)."""
    starts: list[tuple[int, int]] = []

    async def call(ctx):
        await ctx.clock.sleep_until_ns(ctx.started_ns + 1000 * MS)  # threshold-ok 1 s call
        starts.append((ctx.started_ns, ctx.clock.now_ns()))
        return {"outcome": "completed"}

    async def go(clock):
        return await run_load(clock, call, th=TH, levels=(4,), calls_per_level=8,
                              mean_session_s=1.0, seed=1)
    rep = _run(go)
    starts.sort()
    overlaps = sum(1 for (a0, a1), (b0, _) in zip(starts, starts[1:]) if b0 < a1)
    assert overlaps >= 1
    assert all(c["admit_slip_ms"] == 0.0 for c in rep["calls"])


def test_slow_factory_is_load_invalid(tmp_path):
    slow = TH.pacing_slip_p99_ms + 5

    async def go(clock):
        store = Store(tmp_path / "events.sqlite")
        try:
            return await run_load(clock, _factory(slow), th=TH, levels=(2,), calls_per_level=3,
                                  mean_session_s=1.0, seed=2, store=store, run_id="R",
                                  system_id="sysA")
        finally:
            store.close()
    rep = _run(go)
    lvl = rep["levels"][0]
    assert lvl["load_invalid"] and lvl["invalid_reasons"] == ["pacing_slip"]
    assert abs(lvl["pacing_slip_p99_ms"] - slow) < 1e-9
    assert rep["load_invalid"] and all(c["load_invalid"] for c in rep["calls"])
    rows = sqlite3.connect(tmp_path / "events.sqlite").execute(
        "select call_id, system_id, flags_json from calls order by call_id").fetchall()
    assert len(rows) == 3 and {r[1] for r in rows} == {"sysA"}
    f = json.loads(rows[0][2])
    assert f["load_invalid"] is True and f["load"]["level"] == 2
    assert (tmp_path / "load_R.json").exists()


def test_existing_call_row_is_merged(tmp_path):
    """A factory that writes its own calls row (as CallRunner does) keeps it; flags are merged."""
    store = Store(tmp_path / "events.sqlite")

    async def call(ctx):
        store.put("calls", {"call_id": ctx.call_id, "run_id": "R", "scenario_id": "s1",
                            "trial": 0, "condition": "C0", "system_id": "sysB", "is_control": 0,
                            "outcome": "completed", "started_ns": 0, "ended_ns": 1,
                            "wav_path": "a.wav", "usd_projected": 0.0, "usd_actual": 0.0,
                            "flags_json": json.dumps({"delivery": "mulaw"})})
        return {"outcome": "completed", "call_id": ctx.call_id}

    async def go(clock):
        return await run_load(clock, call, th=TH, levels=(1,), calls_per_level=2,
                              mean_session_s=1.0, store=store, run_id="R", out_dir=tmp_path)
    _run(go)
    store.close()
    rows = sqlite3.connect(tmp_path / "events.sqlite").execute(
        "select wav_path, flags_json from calls").fetchall()
    assert len(rows) == 2
    for wav, fj in rows:
        f = json.loads(fj)
        assert wav == "a.wav" and f["delivery"] == "mulaw" and f["load_invalid"] is False


def test_cli_load_spend_refusal_and_levels():
    from duplexbench.cli_load import EXIT_BUDGET, EXIT_NOT_STARTED, check_spend, levels_for
    assert levels_for([8]) == (1, 8) and levels_for([1, 2, 4, 8]) == (1, 2, 4, 8)
    assert levels_for([1]) == (1,)
    assert check_spend(0.0, 0.0, yes=False) is None                    # $0 mock runs
    assert check_spend(2.0, 5.0, yes=False)[0] == EXIT_NOT_STARTED     # priced: needs --yes
    assert check_spend(2.0, 5.0, yes=True) is None
    assert check_spend(6.0, 5.0, yes=True)[0] == EXIT_BUDGET


def test_cli_gate_command(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    import _report_fixture as fx
    from typer.testing import CliRunner
    from duplexbench.cli import app
    db = fx.build(tmp_path / "events.sqlite")
    res = CliRunner().invoke(app, ["gate", fx.RUN, "--db", str(db)])
    assert res.exit_code == 1                        # fixture lacks power/coverage pre-registration
    assert "gate r1: FAIL" in res.output and "[FAIL] coverage_n_met" in res.output
