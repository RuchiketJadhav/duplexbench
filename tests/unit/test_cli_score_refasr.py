"""fix-23 / fix-32: `cli score` runs the reference ASR on the ear channel ($0, no network:
FakeASR and the fake Deepgram server on 127.0.0.1).

Each test builds a one-call event store by hand: a stereo WAV whose right channel is the
ear audio placed `s0` samples after the recorder origin, playout rows computed from the
same samples, events, and a runs row carrying the scenario and the run config.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from fake_deepgram_server import KEY, UUID, FakeDeepgramServer  # noqa: E402

from duplexbench import cli_score  # noqa: E402
from duplexbench.metrics.activity import frame_rms_dbfs, is_active  # noqa: E402
from duplexbench.refasr.engine import FakeASR, Word  # noqa: E402
from duplexbench.storage import canonical_json, connect  # noqa: E402
from duplexbench.thresholds import Thresholds  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TH = Thresholds()
RUN = "r-fix23"
CALL = "c1"
NS_MS = 1_000_000


def _scen(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


PD_D = ROOT / "scenarios" / "public-dev" / "pd_d_001.yaml"
TRACK_C = ROOT / "tests" / "fixtures" / "scenarios" / "public-dev" / "track_c_001.yaml"


def _ear(n_frames: int = 150, rate: int = 8000, active=((10, 60),), seed: int = 1) -> np.ndarray:
    flen = rate // 50
    x = np.zeros(n_frames * flen, np.int16)
    rng = np.random.default_rng(seed)
    for a, b in active:
        x[a * flen:b * flen] = rng.integers(-4000, 4000, (b - a) * flen).astype(np.int16)
    return x


def make_run(tmp: Path, scen: dict, *, simulator: dict | None = None, rate: int = 8000,
             t0_ms: int = 1000, ear: np.ndarray | None = None, events=(), tools=(),
             run_card: dict | None = None) -> Path:
    """One call `c1`: ear audio starts at call-clock t0_ms; the recorder origin is 0."""
    ear = _ear(rate=rate) if ear is None else ear
    flen = rate // 50
    s0 = t0_ms * rate // 1000
    out = tmp / "results" / RUN
    out.mkdir(parents=True)
    wav = out / f"{CALL}.wav"
    right = np.concatenate([np.zeros(s0, np.int16), ear, np.zeros(rate // 2, np.int16)])
    sf.write(str(wav), np.stack([np.zeros_like(right), right], axis=1), rate, subtype="PCM_16")
    db = out / "events.sqlite"
    conn = connect(db)
    cfg = {"scenarios": {scen["id"]: scen}, "run": {"simulator": simulator or {}}}
    conn.execute("INSERT INTO runs (run_id, config_json, run_card_json) VALUES (?,?,?)",
                 (RUN, canonical_json(cfg), canonical_json(run_card or {"summary": {}})))
    conn.execute("INSERT INTO calls (call_id, run_id, scenario_id, trial, condition, system_id, "
                 "is_control, outcome, started_ns, ended_ns, wav_path) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?)",
                 (CALL, RUN, scen["id"], 0, "C0", "mock", 0, "completed", 0,
                  (t0_ms + len(ear) * 1000 // rate) * NS_MS, str(wav)))
    rows = []
    for k in range(len(ear) // flen):
        r = frame_rms_dbfs(ear[k * flen:(k + 1) * flen])
        rows.append((RUN, CALL, (t0_ms + 20 * k) * NS_MS, k, r if np.isfinite(r) else None,
                     int(is_active(r, TH)), 0, 0.0, 0))
    conn.executemany("INSERT INTO playout VALUES (?,?,?,?,?,?,?,?,?)", rows)
    for i, (t_ms, kind, payload) in enumerate(events):
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                     (RUN, CALL, t_ms * NS_MS, i, kind, "harness", canonical_json(payload)))
    for t in tools:
        conn.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (RUN, CALL, t["id"], t["name"], "{}", t["start_ms"] * NS_MS,
                      t["result_ms"] * NS_MS, 0, None, json.dumps(t["result"])))
    conn.commit()
    conn.close()
    return db


class CountingASR(FakeASR):
    """FakeASR that counts pushed frames across its streams."""

    def __init__(self, script, **kw):
        super().__init__(script, **kw)
        self.pushed = 0

    def stream(self):
        st = super().stream()
        push = st.push

        def counted(frame):
            self.pushed += 1
            return push(frame)
        st.push = counted
        return st


def _run(db, **kw):
    lines: list[str] = []
    code = cli_score.score_command(RUN, db, echo=lambda m="", **_: lines.append(str(m)), th=TH, **kw)
    return code, "\n".join(lines)


def _obs(db) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r, tags=json.loads(r["tags_json"])) for r in con.execute(
            "SELECT * FROM observations WHERE run_id = ? ORDER BY metric, event_id", (RUN,))]
    finally:
        con.close()


# -- Track D: steps, the ear offset, words on the call clock -------------------------------
D_EVENTS = [(1200, "caller_turn_end", {"step_id": "s2"}),
            (1500, "caller_turn_end", {"step_id": "s3"}),
            (1800, "caller_turn_end", {"step_id": "s4"})]
# FakeASR times are ms from the first ear frame; t0 = 1000 ms on the call clock.
PHONE = [Word(w, 300 + 40 * i, 330 + 40 * i) for i, w in
         enumerate("two oh two five five five zero one six three".split())]       # call 1300+
ZIP = [Word(w, 20 * i, 15 + 20 * i) for i, w in
       enumerate("nine four one oh seven, two two one eight.".split())]           # call 1000+ < s3 end
CODE = [Word(w, 1000 + 40 * i, 1030 + 40 * i) for i, w in
        enumerate("r t four four seven one".split())]                             # call 2000+


def test_ear_start_sample_found_and_words_shifted(tmp_path):
    db = make_run(tmp_path, _scen(PD_D), t0_ms=1000)
    from duplexbench.metrics.base import CallRecord
    rec = CallRecord.load(db, CALL)
    pcm, t0_ms, s0, _ = cli_score.ear_audio(rec, db)
    assert s0 == 8000 and t0_ms == 1000.0 and len(pcm) == 150 * 160
    assert np.array_equal(pcm, _ear())
    words = cli_score.transcribe_ear(FakeASR([Word("hi", 10, 50)]), pcm, t0_ms)
    assert words == [Word("hi", 1010, 1050)]


def test_ear_resampled_from_16k(tmp_path):
    db = make_run(tmp_path, _scen(PD_D), rate=16000, t0_ms=40)
    from duplexbench.metrics.base import CallRecord
    pcm, t0_ms, s0, _ = cli_score.ear_audio(CallRecord.load(db, CALL), db)
    assert s0 == 640 and t0_ms == 40.0 and len(pcm) == 150 * 160


def test_track_d_entity_step(tmp_path):
    db = make_run(tmp_path, _scen(PD_D), events=D_EVENTS)
    code, out = _run(db, refasr_engine=FakeASR(PHONE + ZIP + CODE))
    assert code == 0, out
    rb = {o["event_id"]: o for o in _obs(db) if o["metric"] == "readback_error_rate"}
    assert rb["e1"]["value"] == 0.0 and rb["e1"]["valid"]        # read back after s2 ended
    assert rb["e2"]["value"] == 1.0 and rb["e2"]["valid"]        # heard before s3 ended: not counted
    assert rb["e3"]["value"] == 0.0
    # fix-32: required_spoken is Track C only -> no pass_at_1 on this Track D call
    assert not [o for o in _obs(db) if o["metric"] == "pass_at_1"]


def test_entity_without_step_uses_whole_transcript_and_unreached_step(tmp_path):
    scen = _scen(PD_D)
    del scen["entities"][1]["step"]                               # e2 without step
    db = make_run(tmp_path, scen, events=D_EVENTS[:1])            # s3, s4 never ended
    assert _run(db, refasr_engine=FakeASR(PHONE + ZIP + CODE))[0] == 0
    rb = {o["event_id"]: o for o in _obs(db) if o["metric"] == "readback_error_rate"}
    assert rb["e2"]["value"] == 0.0                               # whole transcript, as before
    assert rb["e3"]["value"] is None and not rb["e3"]["valid"]
    assert "step_not_reached" in rb["e3"]["tags"]


def test_cache_written_and_reused_with_zero_frames(tmp_path):
    db = make_run(tmp_path, _scen(PD_D), events=D_EVENTS)
    asr = CountingASR(PHONE)
    code, out = _run(db, refasr_engine=asr)
    assert code == 0 and asr.pushed == 150, out
    cache = db.parent / f"{CALL}.refasr.json"
    body = json.loads(cache.read_text(encoding="utf-8"))
    assert body["engine"] == "fake:0" and body["ear_start_sample"] == 8000
    assert body["words"][0] == {"text": "two", "start_ms": 1300, "end_ms": 1330, "conf": 1.0}
    assert "refasr_version: fake:0" in out
    asr2 = CountingASR(PHONE)
    code, out2 = _run(db, refasr_engine=asr2)
    assert code == 0 and asr2.pushed == 0, out2
    assert "1 call(s) reuse cached words (<call_id>.refasr.json): 0 USD, no audio sent" in out2
    con = sqlite3.connect(str(db))
    card = json.loads(con.execute("SELECT run_card_json FROM runs").fetchone()[0])
    con.close()
    assert card["score"]["refasr_version"] == "fake:0" and card["score"]["cached_calls"] == 1


# -- no reference ASR: no_refasr tagging --------------------------------------------------
C_STATE = {"bookings": {"BK-4471": {"date": "2026-11-13", "time": "09:00", "status": "confirmed"}}}
C_TOOLS = [{"id": "t1", "name": "check_availability", "start_ms": 1100, "result_ms": 1200,
            "result": {"slots": ["9:00"]}},
           {"id": "t2", "name": "reschedule_booking", "start_ms": 1300, "result_ms": 1400,
            "result": {"error": "policy_violation", "code": "auth_required"}}]


def test_no_refasr_tags_word_dependent_observations(tmp_path):
    db = make_run(tmp_path, _scen(TRACK_C), tools=C_TOOLS,
                  events=[(5000, "final_state", C_STATE)])
    code, out = _run(db)
    assert code == 0, out
    warn = [ln for ln in out.splitlines() if ln.startswith("warning:")]
    assert len(warn) == 1 and "no simulator.refasr" in warn[0] and "no_refasr" in warn[0]
    by = {o["metric"]: o for o in _obs(db)}
    p1 = by["pass_at_1"]
    assert p1["value"] is None and not p1["valid"] and "no_refasr" in p1["tags"]
    ti = by["t_tool_to_info_ear"]
    assert not ti["valid"] and "no_refasr" in ti["tags"]
    pv = by["policy_violation_rate"]
    assert pv["value"] == 1.0 and pv["valid"] and "no_refasr" in pv["tags"]
    assert not any(t.startswith("refusal_reported:") for t in pv["tags"])
    assert not (db.parent / f"{CALL}.refasr.json").exists()


def test_no_refasr_failed_state_stays_valid_zero(tmp_path):
    db = make_run(tmp_path, _scen(TRACK_C), events=[(5000, "final_state", {"bookings": {}})])
    assert _run(db)[0] == 0
    p1 = next(o for o in _obs(db) if o["metric"] == "pass_at_1")
    assert p1["value"] == 0.0 and p1["valid"]


def test_track_c_with_words(tmp_path):
    db = make_run(tmp_path, _scen(TRACK_C), tools=C_TOOLS[:1],
                  events=[(5000, "final_state", C_STATE)])
    said = [Word(w, 300 + 60 * i, 340 + 60 * i) for i, w in
            enumerate("friday november 13 at nine".split())]       # call 1300+, after result 1200
    code, out = _run(db, refasr_engine=FakeASR(said))
    assert code == 0, out
    by = {o["metric"]: o for o in _obs(db)}
    assert by["pass_at_1"]["value"] == 1.0 and "no_refasr" not in by["pass_at_1"]["tags"]
    ti = by["t_tool_to_info_ear"]
    assert ti["valid"] and ti["value"] == pytest.approx(1200 - 1200, abs=1)   # utterance onset 1200 ms


# -- paid path: Deepgram through the fake server -------------------------------------------
def _dg_sim(s, **kw):
    return {"refasr": {"candidate": "deepgram",
                       "kwargs": {"url": s.url, "finish_timeout_s": 3.0, **kw}}}


def test_paid_refused_without_yes_then_scored_then_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", KEY)
    with FakeDeepgramServer() as s:
        db = make_run(tmp_path, _scen(PD_D), events=D_EVENTS,
                      simulator=_dg_sim(s, pinned_model_uuid=UUID))
        code, out = _run(db)
        assert code == 1, out
        assert (f"  projection: deepgram:nova-3/{UUID}: 1 call(s) x 0.05 ear-audio min total x "
                "0.0077 USD/audio min = 0.0004 USD [https://deepgram.com/pricing checked "
                "2026-11-06]") in out
        assert out.rstrip().endswith("No paid API was called.")
        assert s.paths == [] and not _obs(db)
        code, out = _run(db, yes=True)
        assert code == 0, out
        assert len(s.paths) == 1 and s.audio_bytes[0] == 150 * 160 * 2
        assert f"refasr_version: deepgram:nova-3/{UUID}" in out
        body = json.loads((db.parent / f"{CALL}.refasr.json").read_text(encoding="utf-8"))
        assert [w["text"] for w in body["words"]] == ["hello", "there", "order"]
        assert body["words"][0]["start_ms"] == 1100                 # 0.1 s + t0 1000 ms
        assert "url" in body["refasr_config"]["kwargs"]
        code, out = _run(db)                                        # cached: no --yes needed
        assert code == 0 and len(s.paths) == 1, out
        assert "projection" not in out and "reuse cached words" in out


def test_paid_version_mismatch_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", KEY)
    with FakeDeepgramServer() as s:
        db = make_run(tmp_path, _scen(PD_D), events=D_EVENTS,
                      simulator=_dg_sim(s, pinned_model_uuid="uuid-other"))
        code, out = _run(db, yes=True)
    assert code == 2, out
    assert "version mismatch" in out and "nothing was scored with a different model" in out
    assert not (db.parent / f"{CALL}.refasr.json").exists() and not _obs(db)


def test_unpinned_run_pins_the_runs_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", KEY)
    with FakeDeepgramServer() as s:
        db = make_run(tmp_path, _scen(PD_D), events=D_EVENTS, simulator=_dg_sim(s),
                      run_card={"summary": {"refasr_version_final": "deepgram:nova-3/uuid-run"}})
        code, out = _run(db, yes=True)
    assert "pinned to the run's model uuid uuid-run" in out
    assert code == 2 and "version mismatch" in out                 # the fake reports UUID


def test_cli_score_has_yes_flag(tmp_path):
    from typer.testing import CliRunner

    from duplexbench.cli import app
    db = make_run(tmp_path, _scen(PD_D), events=D_EVENTS)
    res = CliRunner().invoke(app, ["score", RUN, "--db", str(db), "--yes"])
    assert res.exit_code == 0, res.output
    assert "warning: no simulator.refasr" in res.output


# -- fix-24: provisional words never reach the score transcript or the cache ----------------
def test_transcribe_ear_keeps_final_words_only():
    eng = FakeASR([Word("helo", 0, 250, 0.4, False), Word("hello", 0, 300),
                   Word("there", 400, 800)])
    words = cli_score.transcribe_ear(eng, np.zeros(8000, np.int16), 1000.0)
    assert words == [Word("hello", 1000, 1300), Word("there", 1400, 1800)]
    cached = {"words": [{"text": "helo", "start_ms": 0, "end_ms": 250, "final": False},
                        {"text": "hello", "start_ms": 0, "end_ms": 300}]}
    assert cli_score.words_from_cache(cached) == [Word("hello", 0, 300)]


def test_transcribe_ear_unpaced_deepgram_with_interims_one_final_copy_each():
    from duplexbench.refasr.candidates.deepgram import DeepgramASR
    script = [{"word": f"w{k}", "start": 0.1 + 0.3 * k, "end": 0.35 + 0.3 * k,
               "confidence": 0.9} for k in range(8)]
    with FakeDeepgramServer(script=script, chunk_ms=1_000, interim_ms=100,
                            misheard={"w0": "wo"}) as s:
        eng = DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0)
        words = cli_score.transcribe_ear(eng, np.zeros(8000 * 3, np.int16), 0.0)
        assert any(not m["is_final"] for m in s.sent[0])          # interims were sent
    assert [w.text for w in words] == [f"w{k}" for k in range(8)]  # each once, no "wo"
    assert all(w.final for w in words)
