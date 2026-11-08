"""`duplexbench score <run_id> [--db PATH] [--yes]` (default db results/<run_id>/events.sqlite) (BUILD_PLAN M2-T12; spec §1.1 "metrics are computed after the run").

    calls of the run (ORDER BY call_id) --CallRecord.load--> observers --> observations rows
                                                                       --> report/text.render

Observers, in this order per call: metrics.latency, metrics.floor_control,
metrics.diagnostics, then metrics.task.score_track_c and metrics.entities.score when
those modules exist (imported lazily; a missing module is reported as a note and
skipped). Control-twin calls are loaded as the `twin` of their overlay call and are
not scored themselves (aggregate never puts them in main rows, spec §3.4).
`t_stop_ear` comes from floor_control only (it carries the control-twin validity
tags); latency's copy is dropped. Likewise on Track C calls `t_tool_offset` comes from
metrics.task (ToolDef.anchor_step, domain tag); latency's copy is dropped. Re-scoring a run replaces its observations.
Ordering is deterministic: calls by call_id, observations by (metric, event_id,
tags) within a call.

Reference-ASR words of the ear (fix-23; spec §4.2, §4.3). Calls that need words: every
Track C call (score_track_c: required_spoken, t_tool_to_info_ear, the G10 refusal-report
check) and every call whose scenario has `entities` (entities.score). For each, `score`
builds the engine from the run's stored `simulator.refasr` block
(runs.config_json["run"]["simulator"]["refasr"], through `cli_run.build_refasr`) and
streams the ear channel through it:

    ear channel   the right channel of the call WAV (recorder.py). Playout frame k sits at
                  sample s0 + k * frame_len. The recorder origin is not stored, so s0 is
                  found by matching the frame RMS of the WAV against the playout rows'
                  rms_dbfs (exact: the same int16 samples). Audio fed = the playout frames
                  only, resampled to 8 kHz when the condition rate differs
                  (channel/resample.py, sample-aligned), pushed as 20 ms frames, then
                  finish(). No real-time pacing: a network engine's words arrive
                  asynchronously and finish() collects every one.
    word times    engine ms from the first ear frame + the call-clock time of frame 0, i.e.
                  ms on the call clock (the time base metrics/task.py expects).
    cache         `<call_id>.refasr.json` next to the recording: {engine (name:version),
                  name, version, refasr_config (secrets dropped), wav_sha256,
                  ear_start_sample, t0_ms, audio_s, words [{text, start_ms, end_ms, conf}]}.
                  Reused when name, config and WAV hash match and the version matches (or,
                  for an engine with `pinned_model_uuid`, when it is unpinned or pinned to
                  the cached uuid). A rerun streams nothing and charges nothing.
    cost gate     a paid engine (engine.paid or Candidate.paid) prints the projection
                  (uncached ear-audio minutes x the engine module's usd_per_audio_min())
                  and refuses without --yes (exit 1, "No paid API was called.").
    pinning       kwargs.pinned_model_uuid from the run config; when absent and the run
                  card's summary.refasr_version_final names the same engine with a uuid,
                  that uuid is pinned. RefASRVersionMismatch stops scoring (exit 2).
    record        runs.run_card_json["score"] {refasr_version, refasr_config,
                  streamed_calls, cached_calls, ear_audio_min, paid, usd_projected}; the
                  version is printed too.

No `simulator.refasr` in the run config (and no injected engine): Track C / D score with
an empty transcript, one warning line is printed, and every observation that needs words
is tagged `no_refasr`: readback_error_rate and t_tool_to_info_ear become invalid;
pass_at_1 becomes invalid when the scenario has required_spoken or the call has a
policy-violation row and the end state holds (a failed end state stays a valid 0.0);
policy_violation_rate keeps its value (it needs no words) but its `refusal_reported:*`
tag is replaced by `no_refasr`. `required_spoken` is scored for Track C only (fix-32):
Track A/D calls never reach score_track_c.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from duplexbench.storage import canonical_json

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord, Observation
    from duplexbench.refasr.engine import Word
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.thresholds import Thresholds

RESULTS = Path("results")
NO_REFASR = "no_refasr"
REFASR_LINE_RATE = 8_000                  # every refasr engine takes 8 kHz int16 (candidates.LINE_RATE)
_FRAME_MS = 20
_MS_PER_S = 1_000
_NS_PER_MS = 1_000_000
_S_PER_MIN = 60.0                         # threshold-ok: unit conversion s per min
_FULL_SCALE_DB = 20.0 * float(np.log10(32768.0))
_RMS_TOL_DB = 1e-6                        # threshold-ok: float equality of the same samples
_VERIFY_FRAMES = 8
EXIT_NOT_APPROVED = 1
EXIT_ERROR = 2


def default_db(run_id: str) -> Path:
    """`duplexbench run` writes results/<run_id>/events.sqlite (same default as report/gate)."""
    return RESULTS / run_id / "events.sqlite"
FLOOR_OWNED = frozenset({"t_stop_ear"})
OPTIONAL = (("duplexbench.metrics.task", "score_track_c"), ("duplexbench.metrics.entities", "score"))

Observer = Callable[["CallRecord", "Thresholds"], list["Observation"]]
WordsOf = Callable[[str], "list[Word] | None"]


def needs_words(scenario: Any) -> bool:
    """Track C (score_track_c) or any scenario entity (entities.score)."""
    return getattr(scenario, "track", None) == "C" or bool(getattr(scenario, "entities", None))


def observers(notes: list[str] | None = None,
              words_of: WordsOf | None = None) -> list[tuple[str, Observer]]:
    """`words_of(call_id)`: the call's reference-ASR words, or None (no reference ASR: the
    word-dependent observations are tagged `no_refasr`). Default: None for every call."""
    from duplexbench.metrics import diagnostics, floor_control, latency
    wo: WordsOf = words_of or (lambda _cid: None)

    def lat(rec, th):
        drop = set(FLOOR_OWNED)
        if getattr(rec.scenario, "track", None) == "C":
            drop.add("t_tool_offset")       # Track C: task.score_track_c owns it (anchor_step)
        return [o for o in latency.observe(rec, th) if o.metric not in drop]

    out: list[tuple[str, Observer]] = [("latency", lat), ("floor_control", floor_control.observe),
                                       ("diagnostics", diagnostics.observe)]
    for mod, fn in OPTIONAL:
        try:
            m = importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name != mod:
                raise
            if notes is not None:
                notes.append(f"note: {mod} not built yet; skipped")
            continue
        f = getattr(m, fn, None)
        if f is None:
            if notes is not None:
                notes.append(f"note: {mod}.{fn} missing; skipped")
            continue
        out.append((f"{mod.rsplit('.', 1)[1]}.{fn}", _ADAPT[fn](f, wo)))
    return out


def _tag(o: "Observation", *, invalidate: bool, drop_prefix: str | None = None) -> "Observation":
    tags = tuple(t for t in o.tags if not (drop_prefix and t.startswith(drop_prefix)))
    tags = tags if NO_REFASR in tags else (*tags, NO_REFASR)
    if invalidate:
        return replace(o, value=None, valid=False, tags=tags)
    return replace(o, tags=tags)


def _tag_track_c(rec: "CallRecord", obs: list["Observation"]) -> list["Observation"]:
    """No reference ASR: tag (and invalidate) what score_track_c decided from words."""
    from duplexbench.metrics import task
    from duplexbench.tools.state import expected_equals
    scen = rec.scenario
    violation = any(o.metric == "policy_violation_rate" and o.value == 1.0 for o in obs)
    state = task._final_state(rec)
    state_ok = state is not None and expected_equals(state, scen.expected_state)
    out = []
    for o in obs:
        if o.metric == "pass_at_1" and o.valid and state_ok and (scen.required_spoken or violation):
            o = _tag(o, invalidate=True)
        elif o.metric == "t_tool_to_info_ear":
            o = _tag(o, invalidate=True)
        elif o.metric == "policy_violation_rate" and o.value == 1.0:
            o = _tag(o, invalidate=False, drop_prefix=f"{task.REFUSAL_REPORTED_TAG}:")
        out.append(o)
    return out


def _task_adapter(f: Callable, words_of: WordsOf) -> Observer:
    """metrics.task.score_track_c(rec, scenario, tool_rows, refasr_words, th): Track C calls only."""
    def run(rec, th):
        if getattr(rec.scenario, "track", None) != "C":
            return []
        words = words_of(rec.call_id)
        obs = f(rec, rec.scenario, list(rec.tool_calls), list(words or []), th)
        return obs if words is not None else _tag_track_c(rec, obs)
    return run


def _entities_adapter(f: Callable, words_of: WordsOf) -> Observer:
    """metrics.entities.score(rec, scenario, refasr_words, floors, th): scenarios with entities."""
    def run(rec, th):
        if not getattr(rec.scenario, "entities", None):
            return []
        words = words_of(rec.call_id)
        obs = f(rec, rec.scenario, list(words or []), None, th)
        if words is None:
            obs = [_tag(o, invalidate=True) if o.metric == "readback_error_rate" else o
                   for o in obs]
        return obs
    return run


_ADAPT: dict[str, Callable[[Callable, WordsOf], Observer]] = {"score_track_c": _task_adapter,
                                                               "score": _entities_adapter}


def _sort_key(o: "Observation") -> tuple:
    return (o.metric, o.event_id or "", o.tags, -1 if o.value is None else o.value)


def _call_ids(db: Path, run_id: str) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute(
            "SELECT call_id FROM calls WHERE run_id = ? AND COALESCE(is_control, 0) = 0 "
            "ORDER BY call_id", (run_id,))]
    finally:
        conn.close()


def score_run(db: Path | str, run_id: str, th: "Thresholds", *,
              scenarios: "Mapping[str, Scenario] | None" = None,
              notes: list[str] | None = None,
              refasr_words: "Mapping[str, list[Word]] | None" = None) -> list["Observation"]:
    """Score every overlay/clean call of `run_id`; replace its `observations` rows; return them.

    `refasr_words`: call_id -> the ear's reference-ASR words on the call clock (ms). None
    means no reference ASR for the run; a call missing from the mapping has none either.
    Either way its word-dependent observations are tagged `no_refasr` (module docstring)."""
    from duplexbench.metrics.base import CallRecord
    db = Path(db)
    ids = _call_ids(db, run_id)
    words = dict(refasr_words) if refasr_words is not None else None
    obs_fns = observers(notes, (lambda cid: None) if words is None else words.get)
    out: list[Observation] = []
    missing = 0
    for cid in ids:
        rec = CallRecord.load(db, cid, scenarios=scenarios)
        if needs_words(rec.scenario) and (words is None or cid not in words):
            missing += 1
        per: list[Observation] = []
        for _name, fn in obs_fns:
            per += fn(rec, th)
        out += sorted(per, key=_sort_key)
    if missing and notes is not None:
        why = ("no simulator.refasr in the run config" if words is None
               else "no reference-ASR words for some calls")
        notes.append(f"warning: {why}: {missing} Track C/D call(s) scored with an empty "
                     f"transcript; required_spoken, t_tool_to_info_ear, readback_error_rate "
                     f"and the refusal-report check are tagged {NO_REFASR}")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM observations WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO observations (run_id, call_id, metric, scenario_id, trial, condition, "
            "system_id, event_id, value, valid, tags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, o.call_id, o.metric, o.scenario_id, o.trial, o.condition, o.system_id,
              o.event_id, o.value, int(o.valid), canonical_json(list(o.tags))) for o in out])
        conn.commit()
    finally:
        conn.close()
    return out


# -- the ear channel through the reference ASR (fix-23) -------------------------------------
class EarError(RuntimeError):
    """The call's recording cannot be read as its ear channel."""


def _resolve_wav(path: Path, db: Path) -> Path:
    if str(path) and path.is_file():
        return path
    alt = db.parent / path.name
    if path.name and alt.is_file():
        return alt
    raise EarError(f"recording not found: {path}")


def ear_start_sample(right: np.ndarray, playout: np.ndarray, frame_len: int) -> int:
    """Sample index, in the WAV's right channel, of playout frame 0 (module docstring)."""
    idx = np.asarray(playout["frame_idx"], dtype=np.int64)
    rms = np.asarray(playout["rms_dbfs"], dtype=np.float64)
    fin = np.flatnonzero(np.isfinite(rms))
    if not len(fin):
        return 0                                    # all silence: no word, any offset will do
    x = np.asarray(right, dtype=np.int64)
    if len(x) < frame_len:
        raise EarError("recording shorter than one frame")
    cs = np.concatenate([[0], np.cumsum(x * x)])
    win = (cs[frame_len:] - cs[:-frame_len]).astype(np.float64)   # win[p] = sum x[p:p+L]^2

    def db_at(p: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore"):
            return 10.0 * np.log10(win[p] / frame_len) - _FULL_SCALE_DB

    k0, r0 = int(idx[fin[0]]), float(rms[fin[0]])
    pos = np.flatnonzero(win > 0)
    cand = pos[np.abs(db_at(pos) - r0) < _RMS_TOL_DB] - k0 * frame_len
    check = fin[1:1 + _VERIFY_FRAMES]
    for s0 in cand[cand >= 0]:
        p = s0 + idx[check] * frame_len
        if np.all(p < len(win)) and np.all(np.abs(db_at(p) - rms[check]) < _RMS_TOL_DB):
            return int(s0)
    raise EarError("the right channel does not match the call's playout rows")


def ear_audio(rec: "CallRecord", db: Path,
              frame_ms: int = _FRAME_MS) -> tuple[np.ndarray, float, int, Path]:
    """(ear pcm at 8 kHz, t0_ms = call-clock time of frame 0, s0, wav path)."""
    import soundfile as sf
    wav = _resolve_wav(Path(rec.wav_path), db)
    data, rate = sf.read(str(wav), dtype="int16", always_2d=True)
    if data.shape[1] < 2:
        raise EarError(f"{wav.name}: not a stereo call recording")
    pl = rec.playout
    if pl is None or not len(pl):
        return np.zeros(0, np.int16), 0.0, 0, wav
    frame_len = int(rate) * frame_ms // _MS_PER_S
    right = np.ascontiguousarray(data[:, 1])
    s0 = ear_start_sample(right, pl, frame_len)
    n = (int(np.max(pl["frame_idx"])) + 1) * frame_len
    seg = right[s0:s0 + n]
    if len(seg) < n:
        seg = np.concatenate([seg, np.zeros(n - len(seg), np.int16)])
    if int(rate) != REFASR_LINE_RATE:
        from duplexbench.channel.resample import Resampler
        rs = Resampler(int(rate), REFASR_LINE_RATE)
        m = int(round(len(seg) * REFASR_LINE_RATE / int(rate)))
        seg = np.concatenate([rs.push(seg), rs.flush()])[:m].astype(np.int16)
    i0 = int(np.argmin(pl["frame_idx"]))
    t0_ns = int(pl["t_ns"][i0]) - int(pl["frame_idx"][i0]) * frame_ms * _NS_PER_MS
    return seg, t0_ns / _NS_PER_MS, s0, wav


def transcribe_ear(engine: Any, pcm8k: np.ndarray, t0_ms: float) -> list["Word"]:
    """Push 20 ms frames (unpaced), finish(), shift the words onto the call clock.

    fix-24: only final words are kept (`engine.settled`); the stream is opened with
    plain `stream()`, which releases final words only, so the filter is a guard against
    an engine that releases provisional words (Word.final False) anyway."""
    from duplexbench.refasr.engine import Word, settled
    flen = REFASR_LINE_RATE * _FRAME_MS // _MS_PER_S
    if hasattr(engine, "finish_timeout_s"):     # network engine: its backlog drains on finish
        engine.finish_timeout_s = max(float(engine.finish_timeout_s),
                                      len(pcm8k) / REFASR_LINE_RATE)
    st = engine.stream()
    got: list[Word] = []
    try:
        for i in range(0, len(pcm8k), flen):
            got += st.push(pcm8k[i:i + flen])
        got += st.finish()
    finally:
        close = getattr(st, "close", None)
        if callable(close):
            close()
    shift = int(round(t0_ms))
    return sorted((Word(w.text, int(w.start_ms) + shift, int(w.end_ms) + shift, float(w.conf))
                   for w in settled(got)), key=lambda w: (w.start_ms, w.end_ms))


def safe_config(spec: Any) -> dict:
    """The simulator.refasr block without secrets (kwargs naming a key, except *_env)."""
    spec = dict(spec or {})
    kw = {k: v for k, v in dict(spec.get("kwargs") or {}).items()
          if not ("key" in k.lower() and not k.lower().endswith("_env"))}
    return {"candidate": spec.get("candidate"), "kwargs": kw}


def cache_path(wav: Path, call_id: str) -> Path:
    return wav.with_name(f"{call_id}.refasr.json")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_ok(cached: Mapping, engine: Any, config: dict, wav_sha: str) -> bool:
    if cached.get("name") != getattr(engine, "name", None):
        return False
    if cached.get("refasr_config") != config or cached.get("wav_sha256") != wav_sha:
        return False
    if cached.get("version") == getattr(engine, "version", None):
        return True
    if not hasattr(engine, "pinned_model_uuid"):
        return False
    pin = engine.pinned_model_uuid
    return pin is None or str(cached.get("version", "")).rsplit("/", 1)[-1] == str(pin)


def words_from_cache(cached: Mapping) -> list["Word"]:
    from duplexbench.refasr.engine import Word
    # fix-24: the cache holds final words only (transcribe_ear); an entry marked
    # "final": false (never written by this module) is skipped on read
    return [Word(str(w["text"]), int(w["start_ms"]), int(w["end_ms"]), float(w.get("conf", 1.0)))
            for w in cached.get("words") or [] if w.get("final", True) is not False]


@dataclass
class EarJob:
    call_id: str
    wav: Path | None = None
    pcm: np.ndarray | None = None
    t0_ms: float = 0.0
    s0: int = 0
    sha: str = ""
    cached: list | None = None           # words from a valid cache
    cached_version: str | None = None
    error: str | None = None

    @property
    def audio_s(self) -> float:
        return 0.0 if self.pcm is None else len(self.pcm) / REFASR_LINE_RATE


def plan_ear_jobs(db: Path, run_id: str, engine: Any, config: dict, *,
                  scenarios: "Mapping[str, Scenario] | None" = None) -> list[EarJob]:
    """One job per scored call that needs words: its ear audio and, if valid, its cache."""
    from duplexbench.metrics.base import CallRecord
    jobs: list[EarJob] = []
    for cid in _call_ids(db, run_id):
        rec = CallRecord.load(db, cid, scenarios=scenarios, with_twin=False)
        if not needs_words(rec.scenario):
            continue
        job = EarJob(cid)
        try:
            job.pcm, job.t0_ms, job.s0, job.wav = ear_audio(rec, db)
        except (EarError, RuntimeError, OSError) as e:
            job.error = str(e)
            jobs.append(job)
            continue
        job.sha = _sha256(job.wav)
        cp = cache_path(job.wav, cid)
        if cp.is_file():
            try:
                cached = json.loads(cp.read_text(encoding="utf-8"))
            except ValueError:
                cached = None
            if isinstance(cached, dict) and cache_ok(cached, engine, config, job.sha):
                job.cached = words_from_cache(cached)
                job.cached_version = str(cached.get("engine"))
        jobs.append(job)
    return jobs


def write_cache(job: EarJob, engine: Any, config: dict, words: list["Word"]) -> Path:
    from duplexbench.cli_run import refasr_id
    p = cache_path(job.wav, job.call_id)
    body = {"call_id": job.call_id, "engine": refasr_id(engine),
            "name": getattr(engine, "name", None), "version": getattr(engine, "version", None),
            "refasr_config": config, "wav": job.wav.name, "wav_sha256": job.sha,
            "ear_start_sample": job.s0, "t0_ms": job.t0_ms, "audio_s": round(job.audio_s, 3),
            "words": [{"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms,
                       "conf": w.conf} for w in words]}
    p.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return p


def _run_rows(db: Path, run_id: str) -> tuple[dict, dict | None]:
    conn = sqlite3.connect(str(db))
    try:
        r = conn.execute("SELECT config_json, run_card_json FROM runs WHERE run_id = ?",
                         (run_id,)).fetchone()
    finally:
        conn.close()
    if r is None:
        return {}, None
    cfg = json.loads(r[0]) if r[0] else {}
    card = json.loads(r[1]) if r[1] else None
    return (cfg if isinstance(cfg, dict) else {}), (card if isinstance(card, dict) else None)


def run_refasr_spec(cfg: dict) -> dict | None:
    """runs.config_json["run"]["simulator"]["refasr"], or None."""
    run = cfg.get("run") if isinstance(cfg.get("run"), dict) else {}
    sim = run.get("simulator") if isinstance(run.get("simulator"), dict) else {}
    return sim.get("refasr") or None


def _run_uuid(card: dict | None, name: str) -> str | None:
    """The uuid of summary.refasr_version_final (`name:model/uuid`) when it names `name`."""
    v = ((card or {}).get("summary") or {}).get("refasr_version_final")
    if not isinstance(v, str) or not v.startswith(f"{name}:") or "/" not in v:
        return None
    u = v.rsplit("/", 1)[-1]
    return None if u in ("", "unpinned") else u


def _is_paid(engine: Any, spec: dict | None) -> bool:
    if getattr(engine, "paid", False):
        return True
    if spec:
        from duplexbench.refasr.candidates_table import CANDIDATES
        c = {c.name: c for c in CANDIDATES}.get(spec.get("candidate"))
        return bool(c is not None and c.paid)
    return False


def _update_card(db: Path, run_id: str, record: dict) -> None:
    conn = sqlite3.connect(str(db))
    try:
        r = conn.execute("SELECT run_card_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if r is None:
            return
        card = json.loads(r[0]) if r[0] else {}
        card = card if isinstance(card, dict) else {}
        card["score"] = record
        conn.execute("UPDATE runs SET run_card_json = ? WHERE run_id = ?",
                     (canonical_json(card), run_id))
        conn.commit()
    finally:
        conn.close()


def projection_line(engine_id: str, n_calls: int, audio_s: float, rate: float,
                    usd: float, src: str) -> str:
    """`  projection: <name:version>: <n> call(s) x <min> ear-audio min total x <rate>
    USD/audio min = <usd> USD [<source_url> checked <date>]` (cli_run's projection style)."""
    return (f"  projection: {engine_id}: {n_calls} call(s) x {audio_s / _S_PER_MIN:.2f} "
            f"ear-audio min total x {rate:.4f} USD/audio min = {usd:.4f} USD{src}")


def score_command(run_id: str, db: Path | None = None, *, yes: bool = False,
                  echo: Callable[..., Any] | None = None, refasr_engine: Any = None,
                  th: "Thresholds | None" = None) -> int:
    """The `score` command body; returns the exit code. `refasr_engine`: an engine used
    instead of the run's simulator.refasr block (tests inject FakeASR)."""
    import typer

    from duplexbench.cli_run import _refasr_rate, build_refasr, refasr_id
    from duplexbench.refasr.candidates import CandidateUnavailable
    from duplexbench.refasr.candidates.deepgram import RefASRVersionMismatch
    from duplexbench.report.text import render
    from duplexbench.storage import connect
    from duplexbench.thresholds import Thresholds
    echo = echo or typer.echo

    db = Path(db) if db is not None else default_db(run_id)
    if not db.exists():
        echo(f"no event store at {db}", err=True)
        return EXIT_ERROR
    connect(db).close()                        # ensure the schema (observations table) exists
    th = th or Thresholds.load()
    cfg, card = _run_rows(db, run_id)
    spec = run_refasr_spec(cfg)
    engine = refasr_engine
    if engine is None and spec is not None:
        try:
            engine = build_refasr({"refasr": spec})
            uuid = _run_uuid(card, getattr(engine, "name", ""))
            if uuid and getattr(engine, "pinned_model_uuid", "") is None:
                spec = {**spec, "kwargs": {**dict(spec.get("kwargs") or {}),
                                           "pinned_model_uuid": uuid}}
                engine = build_refasr({"refasr": spec})
                echo(f"reference ASR: pinned to the run's model uuid {uuid} "
                     "(run card summary.refasr_version_final)")
        except (ValueError, CandidateUnavailable) as e:
            echo(f"config error: simulator.refasr: {e}", err=True)
            return EXIT_ERROR
    words: dict[str, list] | None = None
    record: dict | None = None
    notes: list[str] = []
    if engine is not None:
        config = safe_config(spec) if refasr_engine is None else \
            {"candidate": None, "kwargs": {}, "injected": refasr_id(engine)}
        jobs = plan_ear_jobs(db, run_id, engine, config)
        todo = [j for j in jobs if j.cached is None and j.error is None]
        cached = [j for j in jobs if j.cached is not None]
        audio_s = sum(j.audio_s for j in todo)
        paid = _is_paid(engine, spec)
        usd = 0.0
        echo(f"reference ASR {refasr_id(engine)}: {len(jobs)} call(s) need ear words: "
             f"{len(todo)} to stream ({audio_s / _S_PER_MIN:.2f} ear-audio min), "
             f"{len(cached)} cached")
        if paid and todo:
            src_obj = engine if callable(getattr(engine, "usd_per_audio_min", None)) else \
                importlib.import_module(type(engine).__module__)
            rate, price_ref = _refasr_rate(src_obj)
            usd = audio_s / _S_PER_MIN * rate
            src = ""
            if price_ref:
                from duplexbench.systems import load_prices
                pr = load_prices(price_ref) or {}
                src = f" [{pr.get('source_url')} checked {pr.get('checked')}]"
            echo(projection_line(refasr_id(engine), len(todo), audio_s, rate, usd, src))
            if not yes:
                echo("Re-run with --yes to approve. No paid API was called.")
                return EXIT_NOT_APPROVED
        if cached:
            echo(f"  {len(cached)} call(s) reuse cached words (<call_id>.refasr.json): "
                 "0 USD, no audio sent")
        words = {}
        versions: set[str] = set()
        for j in cached:
            words[j.call_id] = j.cached
            versions.add(j.cached_version or refasr_id(engine))
        for j in jobs:
            if j.error is not None:
                notes.append(f"warning: {j.call_id}: no ear words ({j.error}); tagged {NO_REFASR}")
        for j in todo:
            try:
                w = transcribe_ear(engine, j.pcm, j.t0_ms)
            except RefASRVersionMismatch as e:
                echo(f"error: reference-ASR version mismatch on {j.call_id}: {e}. Scoring "
                     "stopped; nothing was scored with a different model.", err=True)
                return EXIT_ERROR
            except Exception as e:  # noqa: BLE001 - transport / model failure: stop, keep caches
                echo(f"error: reference ASR failed on {j.call_id}: {type(e).__name__}: {e}. "
                     "Scoring stopped; calls streamed before it are cached.", err=True)
                return EXIT_ERROR
            write_cache(j, engine, config, w)
            words[j.call_id] = w
            versions.add(refasr_id(engine))
        ver = ", ".join(sorted(versions)) if versions else refasr_id(engine)
        if len(versions) > 1:
            notes.append(f"warning: more than one reference-ASR version across calls: {ver}")
        record = {"refasr_version": ver, "refasr_config": config,
                  "streamed_calls": len(todo), "cached_calls": len(cached),
                  "ear_audio_min": round(audio_s / _S_PER_MIN, 4), "paid": paid,
                  "usd_projected": round(usd, 6)}
    obs = score_run(db, run_id, th, notes=notes, refasr_words=words)
    if record is not None:
        _update_card(db, run_id, record)
    for n in notes:
        echo(n)
    if record is not None:
        echo(f"refasr_version: {record['refasr_version']}")
    echo(f"scored {len({o.call_id for o in obs})} call(s), {len(obs)} observation(s)")
    echo(render(db, run_id), nl=False)
    return 0


def register(app: Any) -> None:
    import typer

    @app.command()
    def score(run_id: str = typer.Argument(..., help="run id in the event store"),
              db: Path = typer.Option(None, "--db", help="SQLite event store "
                                                         "(default results/<run_id>/events.sqlite)"),
              yes: bool = typer.Option(False, "--yes", help="approve a paid reference ASR on "
                                                            "the ear channel")) -> None:
        """Score a run: load every call, compute observations, print the text report (spec §4, §10)."""
        code = score_command(run_id, db, yes=yes)
        if code:
            raise typer.Exit(code)
