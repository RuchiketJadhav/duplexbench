"""M2-T12: CallRecord.load and `duplexbench score` on a mock overlay + control-twin run."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from duplexbench.cli import app
from duplexbench.cli_score import score_run
from duplexbench.metrics.base import CallRecord
from duplexbench.thresholds import Thresholds

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
import make_golden_m2 as g  # noqa: E402

TH = Thresholds()


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return g.run_calls(tmp_path_factory.mktemp("score"), TH, conditions=("C0",))


def test_load_overlay_call_with_twin(db):
    rec = CallRecord.load(db, "track_a_002-C0-t0-ovl")
    assert rec.scenario.id == "track_a_002" and len(rec.scenario.overlays) == 5
    assert not rec.is_control and rec.condition == "C0" and rec.system_id == "mock"
    assert [o.overlay_id for o in rec.overlays] == ["o1", "o2", "o3", "o4", "o5"]
    keys = [(e.t_ns, e.seq) for e in rec.events]
    assert keys == sorted(keys)
    assert len(rec.playout) > 0 and np.all(np.diff(rec.playout["t_ns"]) > 0)
    assert rec.twin is not None and rec.twin.is_control
    assert {o.cls for o in rec.twin.scenario.overlays} == {"P-PAUSE", "E-END"}   # make_twin
    assert rec.wav_path.name == "track_a_002-C0-t0-ovl.wav"


def test_load_unknown_call(db):
    with pytest.raises(KeyError):
        CallRecord.load(db, "nope")


def test_score_writes_observations_deterministically(db):
    a = score_run(db, g.RUN_ID, TH)
    b = score_run(db, g.RUN_ID, TH)                 # re-score replaces, same order
    assert a == b and a
    assert {o.call_id for o in a} == {"track_a_002-C0-t0-ovl"}       # twins are not scored
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM observations WHERE run_id = ?", (g.RUN_ID,)).fetchone()[0]
    assert n == len(a)
    assert "t_stop_ear" in {o.metric for o in a}
    assert sum(o.metric == "t_stop_ear" for o in a) == 2             # floor_control's only


def test_cli_score_prints_report(db):
    res = CliRunner().invoke(app, ["score", g.RUN_ID, "--db", str(db)])
    assert res.exit_code == 0, res.output
    assert "yield_rate" in res.output and "@1000" in res.output
    assert "caveat: not_injected" in res.output

