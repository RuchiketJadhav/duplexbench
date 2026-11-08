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
    reference ASR   `refasr_stage` (M4-T01, spec §1.4 D4; fix-40): TTFS p90 <= refasr_partial_p90_ms
                    and RTF <= refasr_rtf_max on the calibration clip (fix-41: default the 195 s,
                    42-sentence refasr_calib_speech_3min.wav); "skipped" when no ASR is configured;
                    on a fail (or --keep-refasr-log) a diagnostics log
                    calibration-<ts>.refasr_log.json is written beside the JSON

`refasr_stage` streams the clip in 20 ms frames through `asr.stream()` on a
virtual real-time feed: frame k is due at (k+1)*20 ms of audio, starts at
max(due, previous frame done), and takes the processing time the clock measured
for its `push`. A word's return time = done time of the push that released it, so a
slow engine accumulates backlog exactly as it would live, without the stage sleeping
through the clip. RTF = summed processing time / clip duration. The gated latency is TTFS (Time To
Final Segment, pipecat stt-benchmark), see `refasr_stage`.

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
from duplexbench.metrics.activity import Segmenter, frame_rms_dbfs, is_active, segment
from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, Event, OverlayRow, ToolRow
from duplexbench.playout import Playout
from duplexbench.recorder import Recorder
from duplexbench.refasr.engine import finalize_if_supported, open_stream
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
# fix-39: real speech with a known transcript (.txt beside it), Fish s2.1-pro-free, 8 kHz.
# The old tone-burst babble clip (refasr_calib_60s.wav) made a real ASR emit no words.
# fix-41: the 195 s clip with 42 sentences (800 ms pauses) replaces the 60 s one (13
# sentences), so one late Deepgram final no longer decides the TTFS p90 on its own. The
# 60 s clip stays in tests/fixtures (other tests use it).
DEFAULT_REFASR_CLIP = ROOT / "tests" / "fixtures" / "refasr_calib_speech_3min.wav"
REFASR_LOG_SUFFIX = ".refasr_log.json"
REFASR_LOG_SCHEMA = "duplexbench.refasr_log/1"


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


def _sleep_until(clock: Clock, t_ns: int) -> None:
    """Blocking wait until `clock.now_ns() >= t_ns` (refasr_stage is synchronous)."""
    import time
    left = t_ns - clock.now_ns()
    if left > 0:
        time.sleep(left / _NS_PER_MS / _MS_PER_S)


def refasr_stage(asr: Any, clip_pcm8k: np.ndarray, th: Thresholds, clock: Clock,
                 diag: dict | None = None) -> dict:
    """Reference-ASR calibration stage (BUILD_PLAN M4-T01; spec §1.4 D4; fix-40 TTFS).

    Returns {ttfs_p50_ms, ttfs_p90_ms, n_utterances, n_ttfs, ttfs_ms, partial_p90_ms, rtf,
    passed, reason, n_words, version, detector, detector_hold_ms, finalize_supported,
    finalize_count}. `clock` measures the processing time of each push/finalize/finish
    (RealClock live; a fake clock in tests).

    Feeding. An engine with `realtime_input = True` (a network engine such as Deepgram,
    whose push() only enqueues and whose words arrive asynchronously) is fed at real
    time: frame k is pushed when its last sample is due, (k + 1) x 20 ms after the start
    on `clock`, and a word's return time is the time since the start when push/finish
    returned it. Other engines run on the virtual real-time feed of the module docstring.
    Return times are observed at push returns, so they are quantized to the 20 ms frame
    grid (late by 0-20 ms). RTF = (push + finalize + finish time) / clip length; for a
    network engine that is mostly the finish wait, not compute.

    Speech-end detector: the playout segmenter's own rule (metrics/activity.Segmenter,
    the one the simulated caller's stop event comes from), run on the clip's 20 ms
    frames: active iff frame RMS > rms_active_dbfs; an utterance ends at the first frame
    of an inactivity run of inactivity_run_ms; the stop is known only when that run is
    complete, i.e. `detector_hold_ms` = inactivity_run_ms after the speech end. Exactly as
    live, right after the push of the frame on which the segmenter reports the stop, the
    stage calls `finalize_if_supported(stream)` (Deepgram: Finalize mid-stream). An
    utterance still open at the end of the clip ends where `Segmenter.finish` puts it, and
    finish() (Finalize + CloseStream) settles it.

    TTFS (pipecat stt-benchmark: TTFS = final transcript receipt time - speech_end_time,
    speech_end_time = VAD stop timestamp - stop_secs). Here speech_end_ms(i) = the first
    frame of utterance i's ending inactivity run x 20 ms (the detector fire time minus
    its hold), so the hold is INSIDE TTFS, as pipecat's stop_secs is. Each final word is
    assigned to the utterance whose span [onset(i), onset(i+1)) holds the word's
    midpoint (before the first onset: utterance 0), so a word whose end_ms runs a few ms
    past the RMS end stays with its utterance. TTFS(i) = return time of the final that
    carries utterance i's last final word (the latest return among its final words: the
    utterance is complete then; pipecat measures to the last segment) - speech_end_ms(i).
    It is signed: a final that arrives before the RMS end is negative. Utterances with
    no final word are counted in n_utterances, not in n_ttfs or the percentiles.
    Engines without finalize() are measured the same way (their finals come at their own
    pace), so the number is comparable across engines.

    Gate: ttfs_p90_ms <= th.refasr_partial_p90_ms and rtf <= th.refasr_rtf_max.
    `partial_p90_ms` (fix-24 per-word first-release latency, provisional words included,
    see `first_release_latencies`) is kept for disclosure only, not gated.
    `version` is read after the stream, so it carries the model uuid it reported.

    fix-41: a `RefASRTransportError` from push/finalize/finish (including the finish()
    timeout) no longer escapes: the stage stops feeding, closes the stream, and returns a
    failed result built from what arrived so far, with `transport_error` set and the
    reason starting "transport error:". `RefASRVersionMismatch` still raises (B11).
    `diag`, when given, is filled with the raw material of the diagnostics log
    (`build_refasr_log`): stop and Finalize times, released words, the streamer's
    `messages` (None for an engine without them)."""
    x = np.asarray(clip_pcm8k, dtype=np.int16).ravel()
    flen = REFASR_RATE * REFASR_FRAME_MS // _MS_PER_S          # 160 samples
    clip_ms = len(x) * _MS_PER_S / REFASR_RATE
    paced = bool(getattr(asr, "realtime_input", False))
    st = open_stream(asr, provisional=True)
    seg = Segmenter(th)
    onsets: list[int] = []                         # frame index of each utterance onset
    ends: list[int] = []                           # frame index of each speech end
    finalized = 0
    released: list[tuple[float, Any]] = []         # (return time ms, Word)
    proc_ns_total = 0
    done_ms = 0.0
    n_frames = -(-len(x) // flen)
    stop_at: list[float] = []                      # ms when each speech end was detected
    finalize_at: list[float | None] = []           # ms Finalize was sent for it (or None)
    finish_at: float | None = None
    transport_error: str | None = None
    origin_ns = clock.now_ns()

    def at_ms(due: float) -> float:                # stage time: clock when paced, else audio
        return (clock.now_ns() - origin_ns) / _NS_PER_MS if paced else float(due)

    try:
        for k in range(n_frames + 1):               # the extra step is finish()
            due_ms = min((k + 1) * REFASR_FRAME_MS, clip_ms)
            if paced:
                _sleep_until(clock, origin_ns + int(due_ms * _NS_PER_MS))
            t0 = clock.now_ns()
            frame = x[k * flen:(k + 1) * flen]
            if k == n_frames:
                finish_at = at_ms(due_ms)
            words = st.push(frame) if k < n_frames else st.finish()
            evs = (seg.push(k, is_active(frame_rms_dbfs(frame), th)) if k < n_frames
                   else seg.finish(n_frames))
            for ev in evs:
                if ev.kind == "onset":
                    onsets.append(ev.frame_idx)
                    continue
                ends.append(ev.frame_idx)
                stop_at.append(at_ms(due_ms))
                finalize_at.append(None)
                if k < n_frames and finalize_if_supported(st):    # live: stop -> Finalize
                    finalized += 1
                    finalize_at[-1] = at_ms(due_ms)
            dt = clock.now_ns() - t0
            proc_ns_total += dt
            if paced:
                done_ms = (clock.now_ns() - origin_ns) / _NS_PER_MS
            else:
                done_ms = max(due_ms, done_ms) + dt / _NS_PER_MS
            released += [(done_ms, w) for w in words]
    except Exception as exc:  # noqa: BLE001 - only transport errors are kept, see below
        if not _is_transport_error(exc):
            raise
        transport_error = str(exc)
        close = getattr(st, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - already gone
                pass
    onset_ms = [i * REFASR_FRAME_MS for i in onsets]
    end_ms = [i * REFASR_FRAME_MS for i in ends]
    ttfs = ttfs_per_utterance(released, onset_ms, end_ms)
    got = [v for v in ttfs if v is not None]
    lat = first_release_latencies(released)
    p90_partial = _pct(lat, _P90)
    p50, p90 = _pct(got, _P50), _pct(got, _P90)
    rtf = (proc_ns_total / _NS_PER_MS) / clip_ms if clip_ms else float("inf")
    reasons = []
    if transport_error is not None:
        reasons.append(f"transport error: {transport_error}")
    if not end_ms:
        reasons.append("no speech end detected in the clip")
    elif p90 is None:
        reasons.append("no final words within any utterance")
    elif p90 > th.refasr_partial_p90_ms:
        reasons.append(f"TTFS p90 {p90:.1f} ms > refasr_partial_p90_ms {th.refasr_partial_p90_ms}")
    if rtf > th.refasr_rtf_max:
        reasons.append(f"RTF {rtf:.3f} > refasr_rtf_max {th.refasr_rtf_max}")
    if diag is not None:
        msgs = getattr(st, "messages", None)
        offset = None
        if isinstance(clock, RealClock) and hasattr(st, "t0_ns"):
            offset = (st.t0_ns - (clock._origin + origin_ns)) / _NS_PER_MS
        diag.update(clip_ms=clip_ms, paced=paced, onset_ms=onset_ms, end_ms=end_ms,
                    stop_ms=stop_at, finalize_sent_ms=finalize_at, finish_called_ms=finish_at,
                    released=released, ttfs=ttfs,
                    stream_messages=None if msgs is None else list(msgs),
                    stream_t0_offset_ms=offset)
    return {"ttfs_p50_ms": _r(p50), "ttfs_p90_ms": _r(p90),
            "n_utterances": len(end_ms), "n_ttfs": len(got),
            "ttfs_ms": [_r(v) for v in ttfs],
            "partial_p90_ms": _r(p90_partial), "rtf": _r(rtf, 4), "passed": not reasons,
            "reason": "; ".join(reasons) or "ok", "n_words": len(lat),
            "detector": "playout Segmenter (rms_active_dbfs, inactivity_run_ms)",
            "detector_hold_ms": th.inactivity_run_ms,
            "finalize_supported": callable(getattr(st, "finalize", None)),
            "finalize_count": finalized, "transport_error": transport_error,
            "version": f"{getattr(asr, 'name', '?')}:{getattr(asr, 'version', '?')}"}


def _is_transport_error(exc: BaseException) -> bool:
    from duplexbench.refasr.candidates.deepgram import RefASRTransportError
    return isinstance(exc, RefASRTransportError)


def utterance_finals(released: list[tuple[float, Any]], onset_ms: list[int],
                     end_ms: list[int]) -> list[list[tuple[float, Any]]]:
    """The (return time, Word) final words assigned to each utterance: word midpoint in
    [onset_ms[i], onset_ms[i+1]) (before the first onset: utterance 0)."""
    import bisect
    n = min(len(onset_ms), len(end_ms))
    out: list[list[tuple[float, Any]]] = [[] for _ in range(n)]
    for t, w in released:
        if not getattr(w, "final", True) or n == 0:
            continue
        mid = (w.start_ms + w.end_ms) / 2
        i = min(max(bisect.bisect_right(onset_ms[:n], mid) - 1, 0), n - 1)
        out[i].append((t, w))
    return out


def ttfs_per_utterance(released: list[tuple[float, Any]], onset_ms: list[int],
                       end_ms: list[int]) -> list[float | None]:
    """TTFS per utterance (fix-40; see refasr_stage): latest return time among the final
    words assigned to utterance i (word midpoint in [onset_ms[i], onset_ms[i+1])) minus
    end_ms[i]; None for an utterance with no final word."""
    return [max(t for t, _ in g) - end_ms[i] if g else None
            for i, g in enumerate(utterance_finals(released, onset_ms, end_ms))]


def first_release_latencies(released: list[tuple[float, Any]]) -> list[float]:
    """One latency per final word (fix-24): earliest return time among the final word and
    the provisional words overlapping it in time, minus the final word's end_ms."""
    prov = [(t, w) for t, w in released if not getattr(w, "final", True)]
    out = []
    for t, f in released:
        if not getattr(f, "final", True):
            continue
        first = min([t] + [tp for tp, p in prov
                           if p.start_ms < f.end_ms and f.start_ms < p.end_ms])
        out.append(first - f.end_ms)
    return out


def refasr_log_path(calibration_json: Path | str) -> Path:
    """`<calibration json stem>.refasr_log.json` next to the calibration JSON (fix-41)."""
    p = Path(calibration_json)
    return p.with_name(p.stem + REFASR_LOG_SUFFIX)


def build_refasr_log(res: dict, diag: dict, asr: Any, clip: Path | str | None,
                     written_because: str) -> dict:
    """The reference-ASR diagnostics log (fix-41). Times are ms. `stop_ms`,
    `finalize_sent_ms`, `finish_called_ms` and each utterance's `*_return_ms` are on the
    stage clock from the stage origin (the real clock for a paced network engine, audio
    time otherwise); `onset_ms` / `speech_end_ms` are audio time (frame index x 20 ms);
    a message's `_arrival_ms` is from the streamer's creation, which is
    `stream_t0_offset_ms` after the stage origin (None when the clock is not real)."""
    onset, end = diag.get("onset_ms", []), diag.get("end_ms", [])
    stops, fins = diag.get("stop_ms", []), diag.get("finalize_sent_ms", [])
    ttfs = diag.get("ttfs", [])
    groups = utterance_finals(diag.get("released", []), onset, end)
    utts = []
    for i, g in enumerate(groups):
        # latest return time; among words returned together, the latest word
        last = max(g, key=lambda tw: (tw[0], tw[1].end_ms)) if g else None
        utts.append({
            "i": i, "onset_ms": onset[i], "speech_end_ms": end[i],
            "stop_ms": _r(stops[i]) if i < len(stops) else None,
            "finalize_sent_ms": _r(fins[i]) if i < len(fins) and fins[i] is not None else None,
            "n_final_words": len(g),
            "first_final_return_ms": _r(min(t for t, _ in g)) if g else None,
            "last_final_return_ms": _r(last[0]) if last else None,
            "last_final_word": ({"text": last[1].text, "start_ms": last[1].start_ms,
                                 "end_ms": last[1].end_ms} if last else None),
            "ttfs_ms": _r(ttfs[i]) if i < len(ttfs) else None})
    msgs = diag.get("stream_messages")
    note = None
    if msgs is None:
        msgs = []
        note = (f"engine {getattr(asr, 'name', '?')} exposes no stream.messages; "
                "server messages not recorded")
    return {
        "schema": REFASR_LOG_SCHEMA,
        "written_because": written_because,
        "status": "pass" if res.get("passed") else "fail",
        "reason": res.get("reason"),
        "transport_error": res.get("transport_error"),
        "version": res.get("version"),
        "clip": str(clip or DEFAULT_REFASR_CLIP),
        "clip_ms": diag.get("clip_ms"),
        "paced": diag.get("paced"),
        "frame_ms": REFASR_FRAME_MS,
        "detector_hold_ms": res.get("detector_hold_ms"),
        "ttfs_p50_ms": res.get("ttfs_p50_ms"), "ttfs_p90_ms": res.get("ttfs_p90_ms"),
        "rtf": res.get("rtf"),
        "stream_t0_offset_ms": _r(diag.get("stream_t0_offset_ms"), 3),
        "stop_ms": [_r(v) for v in diag.get("stop_ms", [])],
        "finalize_sent_ms": [_r(v) for v in diag.get("finalize_sent_ms", []) if v is not None],
        "finish_called_ms": _r(diag.get("finish_called_ms")),
        "utterances": utts,
        "messages": msgs,
        "messages_note": note,
    }


def _scrubbed(obj: Any, scrub: Any) -> Any:
    """`obj` with `scrub` applied to every string (keys included)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {(scrub(k) if isinstance(k, str) else k): _scrubbed(v, scrub)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrubbed(v, scrub) for v in obj]
    return obj


def write_refasr_log(path: Path | str, log: dict, asr: Any) -> Path:
    """Write `log` as JSON, every string through `asr.scrub` (the API key never lands on
    disk) when the engine has one."""
    scrub = getattr(asr, "scrub", None)
    if callable(scrub):
        log = _scrubbed(log, scrub)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    return p


def refasr_result(asr: Any, clip: Path | str | None, th: Thresholds, clock: Clock | None = None,
                  *, log_path: Path | str | None = None, keep_log: bool = False) -> dict:
    """The `refasr` block of the calibration JSON: skipped without an ASR, else pass/fail.

    fix-41: with `log_path`, the diagnostics log (`build_refasr_log`) is written there when
    the stage fails (gate or transport error), or on a pass when `keep_log`; the block's
    `log_path` names it (None when not written)."""
    if asr is None:
        return {"status": "skipped",
                "reason": "no reference ASR configured (M4-T01: pinned model not chosen yet; "
                          "TTFS p90 <= refasr_partial_p90_ms, RTF <= refasr_rtf_max)"}
    pcm, sr = sf.read(str(clip or DEFAULT_REFASR_CLIP), dtype="int16")
    if sr != REFASR_RATE:
        raise ValueError(f"refasr calibration clip must be {REFASR_RATE} Hz, got {sr}")
    diag: dict = {}
    res = refasr_stage(asr, pcm, th, clock or RealClock(), diag=diag)
    written = None
    if log_path is not None and (not res["passed"] or keep_log):
        why = ("transport_error" if res.get("transport_error") else
               "gate_fail" if not res["passed"] else "keep_refasr_log")
        written = str(write_refasr_log(log_path, build_refasr_log(res, diag, asr, clip, why),
                                       asr))
    return {"status": "pass" if res["passed"] else "fail", **res, "log_path": written}


async def calibrate(config: Path | str | CalibConfig | None = None, *, virtual: bool = False,
                    th: Thresholds | None = None, out_dir: Path | None = None,
                    store: Any = None, asr: Any = None,
                    refasr_clip: Path | str | None = None,
                    keep_refasr_log: bool = False) -> dict:
    """Run the calibration; returns the result dict and writes calibration-<ts>.json.

    `asr`: a `RefASR`; when None the reference-ASR stage is reported as skipped.
    fix-41: a failed reference-ASR stage writes calibration-<ts>.refasr_log.json beside
    the JSON; `keep_refasr_log` writes it on a pass too."""
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
    dest = out_dir / f"calibration-{ts}.json"
    refasr = refasr_result(asr, refasr_clip, th, log_path=refasr_log_path(dest),
                           keep_log=keep_refasr_log)
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
        lines.append(f"{'refasr TTFS p90 ms / RTF':<38}{_f(ra['ttfs_p90_ms']):>10}  "
                     f"/ {ra['rtf']}  {'PASS' if ra['passed'] else 'FAIL'}  {ra['reason']}")
        lines.append(f"{'  TTFS p50 ms, n / utterances':<38}{_f(ra['ttfs_p50_ms']):>10}  "
                     f"n={ra['n_ttfs']}/{ra['n_utterances']}  finalize sent={ra['finalize_count']}"
                     f"  hold={ra['detector_hold_ms']} ms  partial p90 (not gated)="
                     f"{_f(ra['partial_p90_ms'])}")
        if ra.get("log_path"):
            lines.append(f"  refasr log: {ra['log_path']}")
    lines.append(f"failed calls: {res['failed_calls']}   RESULT: {'PASS' if res['pass'] else 'FAIL'}")
    return "\n".join(lines)


def _f(x) -> str:
    return "-" if x is None else f"{x:.2f}"
