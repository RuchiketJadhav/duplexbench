"""fix-34 (WORKFLOW_SPEC gap G1): Track A lead-in `scripted: {lead_in: agent_first}`.

Mock Track A runs through `duplexbench run` (RealClock, $0): a mock that greets first
(`greets_ms`) -> step 1 starts after the greeting's end + `eend_silence_ms`, the
greeting stays in the ear recording and is not counted as the reply to step 1; a mock
that never greets -> `no_greeting` after `stall_ms` and step 1 still runs; the default
`caller_first` builds the same call as before (no lead-in flags, step 1 at the call start).
Thresholds are shortened for wall-clock time only (eend_silence_ms 1500, stall_ms 3000).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import yaml

from duplexbench import cli_run
from duplexbench.metrics.base import CallRecord
from duplexbench.metrics.latency import t_turn_gap_ear
from duplexbench.thresholds import Thresholds

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "configs" / "runs" / "smoke_mock.yaml"
EEND_MS, STALL_MS = 1500, 3000
GREETS_MS, FIRST_AUDIO_MS = 1000, 400
_NS = 1_000_000


def _run(tmp_path: Path, *, greets: bool, lead_in: str | None) -> tuple[sqlite3.Connection, Path]:
    sysdir = tmp_path / "systems"
    sysdir.mkdir()
    raw = yaml.safe_load((ROOT / "configs" / "systems" / "mock_stops.yaml").read_text())
    if greets:
        raw["params"]["mock"]["greets_ms"] = GREETS_MS
    (sysdir / "mock_stops.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    th = tmp_path / "thresholds.yaml"
    th.write_text(f"eend_silence_ms: {EEND_MS}\nstall_ms: {STALL_MS}\n", encoding="utf-8")
    cfg = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
    cfg.update(systems=["mock_stops"], systems_dir=str(sysdir), thresholds=str(th),
               results_dir=str(tmp_path / "results"), scenarios=["smoke_a_001"], limit=1)
    if lead_in is not None:
        cfg["scripted"] = {"lead_in": lead_in}
    p = tmp_path / "run.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    lines: list[str] = []
    out = cli_run.run_command(p, yes=True, max_usd=None, allow_unregistered=True,
                              echo=lines.append)
    conn = sqlite3.connect(str(out["db"]))
    conn.row_factory = sqlite3.Row
    return conn, out["db"]


def _call(conn: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM calls")]
    assert len(rows) == 1
    return rows[0]


def _first(conn, call_id: str, kind: str) -> int | None:
    r = conn.execute("SELECT t_ns FROM events WHERE call_id = ? AND kind = ? "
                     "ORDER BY t_ns, seq LIMIT 1", (call_id, kind)).fetchone()
    return None if r is None else int(r[0])


def _th() -> Thresholds:
    return Thresholds(eend_silence_ms=EEND_MS, stall_ms=STALL_MS)


def test_agent_first_waits_for_the_greeting(tmp_path):
    conn, db = _run(tmp_path, greets=True, lead_in="agent_first")
    c = _call(conn)
    assert c["outcome"] == "completed"
    flags = json.loads(c["flags_json"])
    assert flags["lead_in"] == "agent_first" and flags["no_greeting"] is False
    assert flags["greeting_utterances"] == 1
    # greeting = 400 ms .. 1400 ms after open (+ jitter buffer / tick slack at the ear)
    g = flags["greeting_end_ms"]
    assert FIRST_AUDIO_MS + GREETS_MS <= g <= FIRST_AUDIO_MS + GREETS_MS + 200, flags
    # step 1 starts eend_silence_ms after the greeting's end (one-frame tick slack)
    assert EEND_MS <= flags["step1_start_ms"] - g <= EEND_MS + 60, flags
    start = int(c["started_ns"])
    onset = _first(conn, c["call_id"], "caller_turn_onset")
    assert (onset - start) / _NS >= g + EEND_MS
    agent_on = _first(conn, c["call_id"], "agent_onset")
    assert agent_on < onset                       # the greeting was heard before step 1
    # nothing cut: the ear channel carries the greeting before step 1
    data, rate = sf.read(c["wav_path"], dtype="int16", always_2d=True)
    pre = data[: int(rate * (onset - start) / 1e9), 1]
    assert np.abs(pre).max() > 0
    # the greeting is not the reply to step 1: the gap is the mock's programmed reply
    rec = CallRecord.load(db, c["call_id"])
    gaps = [o for o in t_turn_gap_ear(rec, _th()) if o.event_id == "s1"]
    assert len(gaps) == 1 and gaps[0].value is not None
    assert FIRST_AUDIO_MS <= gaps[0].value <= FIRST_AUDIO_MS + 200, gaps
    card = json.loads(conn.execute("SELECT run_card_json FROM runs").fetchone()[0])
    assert card["scripted"] == {"lead_in": "agent_first"}
    # fix-27: a scripted-only run discloses only the lead-in and the clips' TTS (the smoke
    # clips have no .json sidecars, so no vendor / voice is known)
    assert card["simulator"] == {"scripted": {"lead_in": "agent_first", "tts": {
        "source": "stimulus_clips", "vendors": [], "voices": []}}}
    cfgj = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
    assert cfgj["run"]["scripted"] == {"lead_in": "agent_first"}
    conn.close()


def test_agent_first_no_greeting_guard(tmp_path):
    conn, _db = _run(tmp_path, greets=False, lead_in="agent_first")
    c = _call(conn)
    assert c["outcome"] == "completed"
    flags = json.loads(c["flags_json"])
    assert flags["lead_in"] == "agent_first" and flags["no_greeting"] is True
    assert flags["greeting_end_ms"] is None and flags["greeting_utterances"] == 0
    assert STALL_MS <= flags["step1_start_ms"] <= STALL_MS + 100, flags
    onset = _first(conn, c["call_id"], "caller_turn_onset")
    assert onset is not None and (onset - int(c["started_ns"])) / _NS >= STALL_MS
    assert _first(conn, c["call_id"], "agent_onset") > onset    # the mock replied to step 1
    conn.close()


@pytest.mark.parametrize("lead_in", [None, "caller_first"])
def test_caller_first_unchanged(tmp_path, lead_in):
    conn, _db = _run(tmp_path, greets=False, lead_in=lead_in)
    c = _call(conn)
    assert c["outcome"] == "completed"
    flags = json.loads(c["flags_json"])
    assert not {"lead_in", "greeting_end_ms", "no_greeting"} & set(flags)
    onset = _first(conn, c["call_id"], "caller_turn_onset")
    assert (onset - int(c["started_ns"])) / _NS < 200                # step 1 from the start
    card = json.loads(conn.execute("SELECT run_card_json FROM runs").fetchone()[0])
    assert card["scripted"] == {"lead_in": "caller_first"}
    conn.close()


def test_scripted_config_rejects_unknown_lead_in():
    with pytest.raises(Exception):
        cli_run.RunConfig.model_validate({"name": "x", "pack": "p", "systems": ["s"],
                                          "scripted": {"lead_in": "whoever"}})
