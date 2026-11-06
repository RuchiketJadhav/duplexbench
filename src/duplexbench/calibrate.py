# Pattern (fixed-behaviour mock, |error| P95 gate, loop-lag P99, JSON in results/) follows
# C:\Users\Lenovo\research\asr-bench\src\asr_bench\report\calibrate.py (asr-bench, same author).
"""Harness calibration against a mock agent with known behaviour (spec §1.4; BUILD_PLAN M1-T14).

Runs `calls` scripted mock calls (configs/runs/calibrate.yaml) through the real
loop (runner, channel, playout, recorder) and checks that what the harness
measures equals what the mock was programmed to do:

    event-anchored  T_ttfa_recv - first_audio_ms, T_stop_model - stop_after_interrupt_ms,
                    T_tool_offset - tool_at_ms                     |error| p95 <= calib_event_tol_ms
    audio-anchored  T_turn_gap_ear - (first_audio_ms + return-resampler delay),
                    T_stop_ear - stop_after_interrupt_ms           |error| p95 <= calib_audio_tol_ms
                                                                   + jitter_buffer_ms
    harness health  event-loop lag p99 < loop_lag_p99_ms, pacing slip p99 < pacing_slip_p99_ms
    cross-check     RMS vs Silero segment boundary disagreement < silero_disagree_ms
    reference ASR   `refasr_stage` (M4-T01, spec §1.4 D4): partial-latency p90 <= refasr_partial_p90_ms
                    and RTF <= refasr_rtf_max on a 60 s clip; "skipped" when no ASR is configured

`refasr_stage` streams the clip in 20 ms frames through `asr.stream()` on a
virtual real-time feed: frame k is due at (k+1)*20 ms of audio, starts at
max(due, previous frame done), and takes the processing time the clock measured
for its `push`. A word's partial latency = done time of the push that released
it - the word's end_ms, so a slow engine accumulates backlog exactly as it would
live, without the stage sleeping 60 s. RTF = summed processing time / clip duration.

The audio-anchored error includes the jitter-buffer fill by construction: a
real-time source's third frame arrives two frame periods after its first, so on
an exact clock T_turn_gap_ear error = jitter_buffer_ms - frame_ms (+ one frame of
resampler hold-back quantization); that is why the gate allows the buffer.

`simulate_call` builds one call (condition, channel, ToneCaller, MockAgent,
playout, recorder) and is shared by the tests and the M1 golden replay.
`MemStore` keeps rows in memory (optionally teeing to a real `Store`) and
`record_from_rows` turns them into a `CallRecord` until M2-T12's `CallRecord.load`.
"""
from __future__ import annotations

import asyncio
import json
import socket
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
import yaml

from duplexbench.adapters.mock_agent import MockAgent, MockProgram
from duplexbench.channel.chain import ChannelChain, load_condition
from duplexbench.clock import Clock, RealClock, VirtualClock, clock_info
from duplexbench.context import CallContext
from duplexbench.metrics import latency as L
from duplexbench.metrics.activity import segment
from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, Event, OverlayRow, ToolRow
from duplexbench.playout import Playout
from duplexbench.recorder import Recorder
from duplexbench.runner import CallResult, run_call
from duplexbench.stimuli.caller_source import ToneCaller, tone
from duplexbench.thresholds import Thresholds

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs" / "runs" / "calibrate.yaml"
_NS_PER_MS = 1_000_000
_LAG_TICK_NS = 10 * _NS_PER_MS        # event-loop lag probe period
_P95, _P99, _P50 = 95, 99, 50
_P90 = 90
REFASR_FRAME_MS = 20
REFASR_RATE = 8_000
_MS_PER_S = 1_000
DEFAULT_REFASR_CLIP = ROOT / "tests" / "fixtures" / "refasr_calib_60s.wav"


# -- config ------------------------------------------------------------------------
@dataclass(frozen=True)
class CalibConfig:
    calls: int
    condition: str
    seed_fwd: int
    seed_back: int
    call_ms: int
    turn_ms: int
    interrupt_at_ms: int
    interrupt_ms: int
    caller_level_dbfs: float
    mock: MockProgram

    @classmethod
    def load(cls, path: Path | str | None = None, **override: Any) -> "CalibConfig":
        raw = yaml.safe_load(Path(path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
        raw.update(override)
        mock = raw.pop("mock")
        if not isinstance(mock, MockProgram):
            mock = MockProgram(**mock)
        return cls(mock=mock, **raw)


# -- in-memory store and record ---------------------------------------------------------
class MemStore:
    """`Store`-compatible sink keeping rows in memory; tees to `inner` when given."""

    def __init__(self, inner: Any = None):
        self.rows: dict[str, list[dict]] = defaultdict(list)
        self.inner = inner

    def put(self, table: str, row: dict) -> None:
        self.rows[table].append(dict(row))
        if self.inner is not None:
            self.inner.put(table, row)

    async def flush(self) -> None:
        if self.inner is not None:
            await self.inner.flush()


def record_from_rows(rows: dict[str, list[dict]], call_id: str, *, scenario: Any = None,
                     wav_path: Path | None = None, condition: str = "", system_id: str = "",
                     run_id: str = "", trial: int = 0) -> CallRecord:
    """CallRecord from `MemStore.rows` for one call (stand-in for M2-T12 `CallRecord.load`)."""
    ev = [Event(r["t_ns"], r["seq"], r["kind"], r["src"], json.loads(r["payload_json"]))
          for r in rows.get("events", []) if r["call_id"] == call_id]
    ev.sort(key=lambda e: (e.t_ns, e.seq))
    pr = [r for r in rows.get("playout", []) if r["call_id"] == call_id]
    po = np.zeros(len(pr), dtype=PLAYOUT_DTYPE)
    for i, r in enumerate(pr):
        po[i] = (r["t_ns"], r["frame_idx"], -np.inf if r["rms_dbfs"] is None else r["rms_dbfs"],
                 bool(r["active"]), r["queue_depth"], r["backlog_ms"], bool(r["flushed"]))
    ov = [OverlayRow(r["overlay_id"], r["cls"], r["scheduled_ns"], r["actual_ns"], r["onset_ns"],
                     r["end_ns"], tuple(json.loads(r["tags_json"] or "[]")))
          for r in rows.get("overlays", []) if r["call_id"] == call_id]
    tc = [ToolRow(r["tool_call_id"], r["name"], json.loads(r["args_json"]), r["start_ns"],
                  r["result_ns"], r["injected_latency_ms"], r["injected_failure"],
                  json.loads(r["result_json"]) if r["result_json"] else None)
          for r in rows.get("tool_calls", []) if r["call_id"] == call_id]
    return CallRecord(events=ev, playout=po, overlays=ov, tool_calls=tc, sim_turns=[],
                      wav_path=Path(wav_path) if wav_path else Path(), scenario=scenario,
                      twin=None, call_id=call_id, run_id=run_id, trial=trial,
                      condition=condition, system_id=system_id)


# -- one call ----------------------------------------------------------------------------
@dataclass
class SimulatedCall:
    result: CallResult
    record: CallRecord
    wav_path: Path
    chain: ChannelChain
    agent: MockAgent
    store: MemStore = field(repr=False)


async def simulate_call(clock: Clock, cfg: CalibConfig, th: Thresholds, out_dir: Path,
                        call_id: str, *, run_id: str = "calibrate",
                        store: MemStore | None = None,
                        adapter: MockAgent | None = None) -> SimulatedCall:
    """One scripted mock call: caller turn 0..turn_ms, one interruption burst at
    interrupt_at_ms, call length call_ms. Must run inside an accounted task."""
    cond = load_condition(cfg.condition, th)
    store = store or MemStore()
    ctx = CallContext(run_id=run_id, call_id=call_id, scenario=None, trial=0, condition=cond,
                      system_id="mock_agent", is_control=False, clock=clock, store=store,
                      tool_token="calibrate")
    t0 = clock.now_ns()
    caller = ToneCaller(clock, [("s1", 0, cfg.turn_ms)], rate=cond.rate, total_ms=cfg.call_ms,
                        level_dbfs=cfg.caller_level_dbfs)
    caller.inject(tone(cond.rate, cfg.interrupt_ms * cond.rate // 1_000, caller.hz,
                       cfg.caller_level_dbfs), 0.0, t0 + cfg.interrupt_at_ms * _NS_PER_MS)
    wav_path = Path(out_dir) / f"{call_id}.wav"
    rec = Recorder(wav_path, cond.rate, origin_ns=t0)
    playout = Playout(clock, store, rec, th, run_id=run_id, call_id=call_id, rate=cond.rate)
    chain = ChannelChain(cond, cfg.seed_fwd, cfg.seed_back)
    agent = adapter or MockAgent(clock, cfg.mock, th)
    chain.back_delay_ms(agent.program.rate)   # build the return resampler before the call starts
    res = await run_call(ctx, agent, caller, None, playout, chain, None, None, th=th)
    record = record_from_rows(store.rows, call_id, wav_path=wav_path, condition=cond.name,
                              system_id="mock_agent", run_id=run_id)
    return SimulatedCall(res, record, wav_path, chain, agent, store)


# -- measurement -----------------------------------------------------------------------
def _vals(obs) -> list[float]:
    return [o.value for o in obs if o.valid and o.value is not None]


def call_errors(sim: SimulatedCall, cfg: CalibConfig, th: Thresholds) -> dict[str, list[float]]:
    """Measured minus programmed, per calibrated timing (ms)."""
    rec, p = sim.record, cfg.mock
    delay = sim.chain.back_delay_ms(p.rate)
    return {
        "t_ttfa_recv": [v - p.first_audio_ms for v in _vals(L.t_ttfa_recv(rec, th))],
        "t_stop_model": [v - p.stop_after_interrupt_ms for v in _vals(L.t_stop_model(rec, th))],
        "t_tool_offset": [v - p.tool_at_ms for v in _vals(L.t_tool_offset(rec, th))],
        "t_turn_gap_ear": [v - (p.first_audio_ms + delay) for v in _vals(L.t_turn_gap_ear(rec, th))],
        "t_stop_ear": [v - p.stop_after_interrupt_ms for v in _vals(L.t_stop_ear(rec, th))],
    }


def silero_disagreement(sim: SimulatedCall, th: Thresholds) -> float:
    """RMS (played-frame) segments vs Silero boundaries on the ear channel (spec §1.4)."""
    from duplexbench.metrics import silero_check   # offline only; never in the live loop
    data, rate = sf.read(str(sim.wav_path), dtype="int16", always_2d=True)
    po = sim.record.playout
    t_ms = (po["t_ns"] - po["t_ns"][0]) / _NS_PER_MS if len(po) else np.zeros(0)
    rms = []
    for a, b in segment(po["active"], th):
        end = t_ms[b] if b < len(t_ms) else t_ms[-1] + th.frame_ms
        rms.append((float(t_ms[a]), float(end)))
    return silero_check.disagreement_ms(rms, silero_check.boundaries(data[:, 1], rate))


EVENT_METRICS = ("t_ttfa_recv", "t_stop_model", "t_tool_offset")
AUDIO_METRICS = ("t_turn_gap_ear", "t_stop_ear")


def _pct(xs: list[float], q: float) -> float | None:
    return float(np.percentile(xs, q)) if xs else None


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


async def _lag_probe(clock: Clock, lags: list[float], stop: asyncio.Event) -> None:
    t = clock.now_ns()
    while not stop.is_set():
        t += _LAG_TICK_NS
        await clock.sleep_until_ns(t)
        lags.append((clock.now_ns() - t) / _NS_PER_MS)


def refasr_stage(asr: Any, clip_pcm8k: np.ndarray, th: Thresholds, clock: Clock) -> dict:
    """Reference-ASR calibration stage (BUILD_PLAN M4-T01; spec §1.4 D4).

    Returns {partial_p90_ms, rtf, passed, reason, n_words, version}. `clock` measures
    the processing time of each `push`/`finish` (RealClock live; a fake clock in tests)."""
    x = np.asarray(clip_pcm8k, dtype=np.int16).ravel()
    flen = REFASR_RATE * REFASR_FRAME_MS // _MS_PER_S          # 160 samples
    clip_ms = len(x) * _MS_PER_S / REFASR_RATE
    st = asr.stream()
    lat: list[float] = []
    proc_ns_total = 0
    done_ms = 0.0
    n_frames = -(-len(x) // flen)
    for k in range(n_frames + 1):                   # the extra step is finish()
        due_ms = min((k + 1) * REFASR_FRAME_MS, clip_ms)
        t0 = clock.now_ns()
        words = st.push(x[k * flen:(k + 1) * flen]) if k < n_frames else st.finish()
        dt = clock.now_ns() - t0
        proc_ns_total += dt
        done_ms = max(due_ms, done_ms) + dt / _NS_PER_MS
        lat += [done_ms - w.end_ms for w in words]
    p90 = _pct(lat, _P90)
    rtf = (proc_ns_total / _NS_PER_MS) / clip_ms if clip_ms else float("inf")
    reasons = []
    if p90 is None:
        reasons.append("no words emitted")
    elif p90 > th.refasr_partial_p90_ms:
        reasons.append(f"partial p90 {p90:.1f} ms > refasr_partial_p90_ms {th.refasr_partial_p90_ms}")
    if rtf > th.refasr_rtf_max:
        reasons.append(f"RTF {rtf:.3f} > refasr_rtf_max {th.refasr_rtf_max}")
    return {"partial_p90_ms": _r(p90), "rtf": _r(rtf, 4), "passed": not reasons,
            "reason": "; ".join(reasons) or "ok", "n_words": len(lat),
            "version": f"{getattr(asr, 'name', '?')}:{getattr(asr, 'version', '?')}"}


def refasr_result(asr: Any, clip: Path | str | None, th: Thresholds, clock: Clock | None = None) -> dict:
    """The `refasr` block of the calibration JSON: skipped without an ASR, else pass/fail."""
    if asr is None:
        return {"status": "skipped",
                "reason": "no reference ASR configured (M4-T01: pinned model not chosen yet; "
                          "partial-latency p90 <= refasr_partial_p90_ms, RTF <= refasr_rtf_max)"}
    pcm, sr = sf.read(str(clip or DEFAULT_REFASR_CLIP), dtype="int16")
    if sr != REFASR_RATE:
        raise ValueError(f"refasr calibration clip must be {REFASR_RATE} Hz, got {sr}")
    res = refasr_stage(asr, pcm, th, clock or RealClock())
    return {"status": "pass" if res["passed"] else "fail", **res}


async def calibrate(config: Path | str | CalibConfig | None = None, *, virtual: bool = False,
                    th: Thresholds | None = None, out_dir: Path | None = None,
                    store: Any = None, asr: Any = None,
                    refasr_clip: Path | str | None = None) -> dict:
    """Run the calibration; returns the result dict and writes calibration-<ts>.json.

    `asr`: a `RefASR`; when None the reference-ASR stage is reported as skipped."""
    th = th or Thresholds.load()
    cfg = config if isinstance(config, CalibConfig) else CalibConfig.load(config)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(out_dir or ROOT / "results")
    wav_dir = out_dir / f"calibration-{ts}"
    errs: dict[str, list[float]] = {k: [] for k in EVENT_METRICS + AUDIO_METRICS}
    disagreements, slips, lags, outcomes = [], [], [], []
    # One VirtualClock serves every call: two VirtualClocks in one event loop livelock
    # (each finds the other's _maybe_advance in loop._ready and reschedules forever).
    vclock = VirtualClock() if virtual else None
    if not virtual:     # discarded warm-up call: first-use costs (imports, caches) stall the loop
        warm = RealClock()
        await warm.run(simulate_call(warm, cfg, th, wav_dir / "warmup", "warmup"))
    for i in range(cfg.calls):
        clock: Clock = vclock if vclock is not None else RealClock()
        stop = asyncio.Event()

        async def one(clock=clock, stop=stop, i=i):
            probe = clock.task(_lag_probe(clock, lags, stop)) if not virtual else None
            try:
                return await simulate_call(clock, cfg, th, wav_dir, f"cal{i:03d}",
                                           store=MemStore(store))
            finally:
                stop.set()
                if probe is not None:
                    probe.cancel()
                    await clock.idle(asyncio.gather(probe, return_exceptions=True))

        sim = await clock.run(one())
        outcomes.append(sim.result.outcome)
        for k, v in call_errors(sim, cfg, th).items():
            errs[k] += v
        if "pacing_slip_p99_ms" in sim.result.flags:
            slips.append(sim.result.flags["pacing_slip_p99_ms"])
        disagreements.append(silero_disagreement(sim, th))
    ev_tol = th.calib_event_tol_ms
    au_tol = th.calib_audio_tol_ms + th.jitter_buffer_ms
    metrics, ok = {}, True
    for k in EVENT_METRICS + AUDIO_METRICS:
        e = errs[k]
        tol = ev_tol if k in EVENT_METRICS else au_tol
        abs95 = _pct([abs(x) for x in e], _P95)
        passed = bool(abs95 is not None and abs95 <= tol and len(e) == cfg.calls)
        ok &= passed
        metrics[k] = {"kind": "event" if k in EVENT_METRICS else "audio", "n": len(e),
                      "error_ms_p50": _r(_pct(e, _P50)), "abs_error_ms_p95": _r(abs95),
                      "abs_error_ms_max": _r(max((abs(x) for x in e), default=None)),
                      "tolerance_ms": tol, "pass": passed}
    lag99 = _pct(lags, _P99) if lags else 0.0
    slip99 = max(slips) if slips else 0.0
    dis = max(disagreements) if disagreements else None
    health = {
        "loop_lag_p99_ms": {"value": _r(lag99), "limit": th.loop_lag_p99_ms,
                            "pass": bool(lag99 < th.loop_lag_p99_ms)},
        "pacing_slip_p99_ms": {"value": _r(slip99), "limit": th.pacing_slip_p99_ms,
                               "pass": bool(slip99 < th.pacing_slip_p99_ms)},
        "silero_disagreement_ms": {"value": _r(dis), "limit": th.silero_disagree_ms,
                                   "pass": bool(dis is not None and dis < th.silero_disagree_ms)},
    }
    for h in health.values():
        ok &= h["pass"]
    bad_calls = sum(o != "completed" for o in outcomes)
    ok &= bad_calls == 0
    refasr = refasr_result(asr, refasr_clip, th)
    ok &= refasr["status"] != "fail"
    out = {
        "utc": datetime.now(timezone.utc).isoformat(), "host": socket.gethostname(),
        "clock": "virtual" if virtual else clock_info(), "condition": cfg.condition,
        "calls": cfg.calls, "failed_calls": bad_calls, "outcomes": outcomes,
        "program": {"first_audio_ms": cfg.mock.first_audio_ms,
                    "stop_after_interrupt_ms": cfg.mock.stop_after_interrupt_ms,
                    "tool_at_ms": cfg.mock.tool_at_ms, "reply_len_ms": cfg.mock.reply_len_ms,
                    "behaviour": cfg.mock.behaviour, "rate": cfg.mock.rate,
                    "voice": cfg.mock.voice, "interrupt_at_ms": cfg.interrupt_at_ms},
        "thresholds_hash": th.hash(),
        "metrics": metrics, "health": health,
        "refasr": refasr,
        "pass": bool(ok),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"calibration-{ts}.json"
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    out["path"] = str(dest)
    return out


def format_table(res: dict) -> str:
    """Plain-text table of a calibration result."""
    lines = [f"calibration  condition={res['condition']}  calls={res['calls']}  "
             f"clock={res['clock'] if isinstance(res['clock'], str) else res['clock']['implementation']}",
             f"{'timing':<16}{'kind':<7}{'n':>4}{'err p50':>10}{'|err| p95':>11}{'|err| max':>11}"
             f"{'tol':>7}  ok"]
    for k, m in res["metrics"].items():
        lines.append(f"{k:<16}{m['kind']:<7}{m['n']:>4}{_f(m['error_ms_p50']):>10}"
                     f"{_f(m['abs_error_ms_p95']):>11}{_f(m['abs_error_ms_max']):>11}"
                     f"{m['tolerance_ms']:>7}  {'PASS' if m['pass'] else 'FAIL'}")
    for k, h in res["health"].items():
        lines.append(f"{k:<38}{_f(h['value']):>10}  < {h['limit']}  {'PASS' if h['pass'] else 'FAIL'}")
    ra = res["refasr"]
    if ra["status"] == "skipped":
        lines.append(f"{'refasr':<38}{'skipped':>10}  ({ra['reason'][:40]}...)")
    else:
        lines.append(f"{'refasr partial p90 ms / RTF':<38}{_f(ra['partial_p90_ms']):>10}  "
                     f"/ {ra['rtf']}  {'PASS' if ra['passed'] else 'FAIL'}  {ra['reason']}")
    lines.append(f"failed calls: {res['failed_calls']}   RESULT: {'PASS' if res['pass'] else 'FAIL'}")
    return "\n".join(lines)


def _f(x) -> str:
    return "-" if x is None else f"{x:.2f}"
