"""Load runner: open-loop arrivals per concurrency level (spec §4.4 Track L; BUILD_PLAN M9-T03/T04).

    run_load(clock, call_factory, th=th, levels=(1, 2, 4, 8), calls_per_level=N,
             mean_session_s=S, seed=0, store=None, run_id="load") -> dict

Per level the arrivals come from `load.scheduler.open_loop(N, level, S, seed + level)`
(rate = level / S; phase by scheduled arrival time: ramp / sustain / cooldown).
Levels run one after another; a level ends when its last call returns.

Each call runs as an accounted task (`clock.task`) and gets a `LoadCallCtx` with
two harness-side monitors:

  * `ctx.pace`  PaceMonitor: the factory reports every frame it sends with
                `ctx.pace.record(scheduled_ns, sent_ns)` or `await ctx.pace.send_at(t)`;
                slip = sent - scheduled (spec §6.1 logged pacing slip).
  * loop lag    LoopLagMonitor: a ticker on the frame grid (`th.frame_ms`) while the
                call is alive; lag = wake - target (spec §1.4).

A call is `load_invalid` when its harness-side pacing slip p99 > `th.pacing_slip_p99_ms`
or its loop-lag p99 > `th.loop_lag_p99_ms`; a level is `load_invalid` when the pooled
slip p99 (or pooled loop-lag p99) of its calls exceeds the same thresholds. These are
harness failures, never charged to the system (gates.py counts them as harness-invalid).

The factory returns a runner.CallResult-like object or a dict: `outcome`
(completed / transport_error / rate_limited / disconnect / ...), optional `flags`,
`call_id`, `latency_ms` (the latency whose shift per level is reported). A factory
that raises yields `transport_error`; the attempt is kept.

Per level: n, error / 429 / disconnect rates with Wilson CIs, median latency and its
shift against the first level, admission slip, slip and loop-lag p99.

Store (optional): each call's `calls.flags_json` gains `load` (level, phase,
scheduled_ns, admit_slip_ms, pacing_slip_p99_ms, loop_lag_p99_ms) and
`load_invalid`; when the factory wrote no `calls` row a minimal one is written.
`load_<run_id>.json` (the returned report) and, on a real clock, SysMon samples
`sysmon_<run_id>.json` are written next to the store file (or `out_dir`).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from duplexbench.load.scheduler import Arrival, open_loop
from duplexbench.load.sysmon import JsonSink, SysMon, percentile
from duplexbench.stats.wilson import wilson
from duplexbench.storage import canonical_json
from duplexbench.thresholds import Thresholds

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
LEVELS = (1, 2, 4, 8)       # BUILD_PLAN M9-T03 (spec §4.4: c = 1 and c = 8; 2 and 4 between)
P99 = 99
P50 = 50


class PaceMonitor:
    """Harness-side pacing slip of one call's sender (spec §6.1)."""

    def __init__(self, clock: Any):
        self.clock = clock
        self.slips_ms: list[float] = []

    def record(self, scheduled_ns: int, sent_ns: int) -> None:
        self.slips_ms.append(max(0, sent_ns - scheduled_ns) / NS_PER_MS)

    async def send_at(self, t_ns: int) -> int:
        await self.clock.sleep_until_ns(t_ns)
        now = self.clock.now_ns()
        self.record(t_ns, now)
        return now

    def p99_ms(self) -> float | None:
        return percentile(self.slips_ms, P99)


class LoopLagMonitor:
    """Frame-grid ticker on the call's clock; lag = wake time - target (spec §1.4)."""

    def __init__(self, clock: Any, frame_ms: int):
        self.clock, self.frame_ns = clock, frame_ms * NS_PER_MS
        self.lags_ms: list[float] = []
        self._task: asyncio.Task | None = None

    async def _tick(self) -> None:
        target = self.clock.now_ns()
        while True:
            target += self.frame_ns
            await self.clock.sleep_until_ns(target)
            self.lags_ms.append(max(0, self.clock.now_ns() - target) / NS_PER_MS)

    def start(self) -> None:
        self._task = self.clock.task(self._tick())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await self.clock.idle(asyncio.gather(self._task, return_exceptions=True))

    def p99_ms(self) -> float | None:
        return percentile(self.lags_ms, P99)


@dataclass
class LoadCallCtx:
    index: int
    level: int
    phase: str
    scheduled_ns: int
    started_ns: int
    call_id: str
    clock: Any
    pace: PaceMonitor
    th: Thresholds


@dataclass
class LoadCall:
    call_id: str
    index: int
    level: int
    phase: str
    scheduled_ns: int
    started_ns: int
    ended_ns: int
    outcome: str
    latency_ms: float | None
    admit_slip_ms: float
    pacing_slip_p99_ms: float | None
    loop_lag_p99_ms: float | None
    slips_ms: list[float] = field(default_factory=list, repr=False)
    lags_ms: list[float] = field(default_factory=list, repr=False)
    flags: dict = field(default_factory=dict)
    load_invalid: bool = False
    invalid_reasons: list[str] = field(default_factory=list)


CallFactory = Callable[[LoadCallCtx], Awaitable[Any]]


def _result_fields(res: Any) -> tuple[str, dict, str | None, float | None]:
    if isinstance(res, dict):
        flags = dict(res.get("flags") or {})
        return (str(res.get("outcome", "completed")), flags, res.get("call_id"),
                res.get("latency_ms", flags.get("latency_ms")))
    flags = dict(getattr(res, "flags", None) or {})
    lat = getattr(res, "latency_ms", None)
    return (str(getattr(res, "outcome", "completed")), flags, getattr(res, "call_id", None),
            lat if lat is not None else flags.get("latency_ms"))


def is_rate_limited(c: LoadCall) -> bool:
    return c.outcome == "rate_limited" or c.flags.get("http_status") == 429 \
        or bool(c.flags.get("rate_limited"))


def is_disconnect(c: LoadCall) -> bool:
    return c.outcome == "disconnect" or bool(c.flags.get("disconnect"))


def _rate(k: int, n: int) -> dict:
    if not n:
        return {"k": k, "n": n, "value": None, "ci": None}
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "value": k / n, "ci": [lo, hi]}


async def _run_one(clock: Any, factory: CallFactory, arr: Arrival, level: int, t0: int,
                   th: Thresholds, run_id: str) -> LoadCall:
    sched = t0 + int(round(arr.t_s * NS_PER_S))
    await clock.sleep_until_ns(sched)
    start = clock.now_ns()
    call_id = f"{run_id}-c{level}-{arr.index:04d}"
    pace = PaceMonitor(clock)
    lag = LoopLagMonitor(clock, th.frame_ms)
    lag.start()
    ctx = LoadCallCtx(arr.index, level, arr.phase, sched, start, call_id, clock, pace, th)
    flags: dict = {}
    latency = None
    try:
        res = await factory(ctx)
        outcome, flags, cid, latency = _result_fields(res)
        call_id = cid or call_id
    except asyncio.CancelledError:
        raise
    except Exception as e:          # an attempt is never dropped (CLAUDE.md M8)
        outcome, flags = "transport_error", {"error": repr(e)}
    finally:
        await lag.stop()
    slip = pace.p99_ms()
    if slip is None and flags.get("pacing_slip_p99_ms") is not None:
        slip = float(flags["pacing_slip_p99_ms"])
    c = LoadCall(call_id, arr.index, level, arr.phase, sched, start, clock.now_ns(), outcome,
                 None if latency is None else float(latency), (start - sched) / NS_PER_MS,
                 slip, lag.p99_ms(), list(pace.slips_ms), list(lag.lags_ms), flags)
    if slip is not None and slip > th.pacing_slip_p99_ms:
        c.invalid_reasons.append("pacing_slip")
    if c.loop_lag_p99_ms is not None and c.loop_lag_p99_ms > th.loop_lag_p99_ms:
        c.invalid_reasons.append("loop_lag")
    c.load_invalid = bool(c.invalid_reasons)
    return c


def summarize_level(level: int, calls: Sequence[LoadCall], th: Thresholds,
                    base_latency_p50: float | None) -> dict:
    n = len(calls)
    slips = [s for c in calls for s in c.slips_ms] or \
        [c.pacing_slip_p99_ms for c in calls if c.pacing_slip_p99_ms is not None]
    lags = [x for c in calls for x in c.lags_ms]
    slip_p99, lag_p99 = percentile(slips, P99), percentile(lags, P99)
    lat = [c.latency_ms for c in calls if c.latency_ms is not None]
    lat_p50 = percentile(lat, P50)
    reasons = []
    if slip_p99 is not None and slip_p99 > th.pacing_slip_p99_ms:
        reasons.append("pacing_slip")
    if lag_p99 is not None and lag_p99 > th.loop_lag_p99_ms:
        reasons.append("loop_lag")
    phases: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for c in calls:
        phases[c.phase] = phases.get(c.phase, 0) + 1
        outcomes[c.outcome] = outcomes.get(c.outcome, 0) + 1
    return {
        "level": level, "n": n, "phases": dict(sorted(phases.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "error_rate": _rate(sum(c.outcome != "completed" for c in calls), n),
        "rate_429": _rate(sum(is_rate_limited(c) for c in calls), n),
        "disconnect_rate": _rate(sum(is_disconnect(c) for c in calls), n),
        "latency_p50_ms": lat_p50, "latency_n": len(lat),
        "latency_shift_ms": (None if lat_p50 is None or base_latency_p50 is None
                             else lat_p50 - base_latency_p50),
        "admit_slip_p99_ms": percentile([c.admit_slip_ms for c in calls], P99),
        "pacing_slip_p99_ms": slip_p99, "loop_lag_p99_ms": lag_p99,
        "load_invalid": bool(reasons), "invalid_reasons": reasons,
        "n_calls_load_invalid": sum(c.load_invalid for c in calls),
    }


def _store_path(store: Any) -> Path | None:
    if store is None:
        return None
    return Path(store.path) if hasattr(store, "path") else Path(store)


def _write_calls(store: Any, run_id: str, system_id: str | None,
                 calls: Sequence[LoadCall]) -> None:
    """Merge the load flags into each call's `calls` row (INSERT OR REPLACE of the whole row)."""
    store.flush_sync()
    existing: dict[str, dict] = {}
    if calls:
        conn = sqlite3.connect(str(store.path))
        conn.row_factory = sqlite3.Row
        try:
            q = ",".join("?" * len(calls))
            existing = {r["call_id"]: dict(r) for r in conn.execute(
                f"SELECT * FROM calls WHERE call_id IN ({q})", [c.call_id for c in calls])}
        except sqlite3.OperationalError:
            existing = {}
        finally:
            conn.close()
    for c in calls:
        load = {"level": c.level, "phase": c.phase, "scheduled_ns": c.scheduled_ns,
                "admit_slip_ms": c.admit_slip_ms, "pacing_slip_p99_ms": c.pacing_slip_p99_ms,
                "loop_lag_p99_ms": c.loop_lag_p99_ms, "invalid_reasons": c.invalid_reasons}
        row = existing.get(c.call_id)
        if row is None:
            row = {"call_id": c.call_id, "run_id": run_id,
                   "scenario_id": c.flags.get("scenario_id"), "trial": c.index,
                   "condition": c.flags.get("condition", "C0"), "system_id": system_id,
                   "is_control": 0, "outcome": c.outcome, "started_ns": c.started_ns,
                   "ended_ns": c.ended_ns, "wav_path": None, "usd_projected": None,
                   "usd_actual": None, "flags_json": canonical_json(c.flags)}
        flags = json.loads(row.get("flags_json") or "{}")
        flags["load"] = load
        flags["load_invalid"] = c.load_invalid
        row["flags_json"] = canonical_json(flags)
        store.put("calls", row)
    store.flush_sync()


async def run_load(clock: Any, call_factory: CallFactory, *, th: Thresholds,
                   levels: Sequence[int] = LEVELS, calls_per_level: int, mean_session_s: float,
                   seed: int = 0, ramp_s: float | None = None, cool_s: float | None = None,
                   store: Any = None, run_id: str = "load", system_id: str | None = None,
                   sysmon: bool | None = None, out_dir: Path | str | None = None) -> dict:
    """Run every level; return the load report (see module docstring).

    `sysmon=None` starts SysMon on a real clock only (it measures wall time).
    """
    use_sysmon = (getattr(clock, "name", "") != "virtual") if sysmon is None else sysmon
    sp = _store_path(store)
    base = Path(out_dir) if out_dir is not None else (sp.parent if sp is not None else None)
    sink = mon = None
    if use_sysmon:
        sink = JsonSink((base or Path(".")) / f"sysmon_{run_id}.json")
        mon = SysMon(sink, run_id, th=th)
        mon.start()
    all_calls: list[LoadCall] = []
    per_level: list[dict] = []
    base_p50: float | None = None
    mon_summary = None
    try:
        for lvl in levels:
            arrivals = open_loop(calls_per_level, lvl, mean_session_s, seed + lvl, ramp_s, cool_s)
            if mon is not None:
                mon.level, mon.phase = lvl, "level"
            t0 = clock.now_ns()
            tasks = [clock.task(_run_one(clock, call_factory, a, lvl, t0, th, run_id))
                     for a in arrivals]
            done = await clock.idle(asyncio.gather(*tasks))
            calls = sorted(done, key=lambda c: c.index)
            s = summarize_level(lvl, calls, th, base_p50)
            if not per_level:
                base_p50 = s["latency_p50_ms"]
                s["latency_shift_ms"] = 0.0 if base_p50 is not None else None
            per_level.append(s)
            all_calls += calls
    finally:
        if mon is not None:
            mon_summary = await mon.stop()
    if store is not None and hasattr(store, "put"):
        _write_calls(store, run_id, system_id, all_calls)
    report: dict[str, Any] = {
        "run_id": run_id, "system_id": system_id, "levels": per_level,
        "mean_session_s": mean_session_s, "calls_per_level": calls_per_level, "seed": seed,
        "thresholds": {"pacing_slip_p99_ms": th.pacing_slip_p99_ms,
                       "loop_lag_p99_ms": th.loop_lag_p99_ms},
        "load_invalid": any(s["load_invalid"] for s in per_level),
        "calls": [{"call_id": c.call_id, "level": c.level, "phase": c.phase,
                   "outcome": c.outcome, "load_invalid": c.load_invalid,
                   "invalid_reasons": c.invalid_reasons, "admit_slip_ms": c.admit_slip_ms,
                   "pacing_slip_p99_ms": c.pacing_slip_p99_ms,
                   "loop_lag_p99_ms": c.loop_lag_p99_ms, "latency_ms": c.latency_ms}
                  for c in all_calls],
        "sysmon": mon_summary,
    }
    if sink is not None:
        report["sysmon_path"] = str(sink.write(mon_summary))
    if base is not None:
        base.mkdir(parents=True, exist_ok=True)
        p = base / f"load_{run_id}.json"
        p.write_text(json.dumps(report, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        report["report_path"] = str(p)
    return report
