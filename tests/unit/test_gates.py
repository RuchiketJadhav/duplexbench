"""gates.py (M8 acceptance, CLAUDE.md §4 M8; BUILD_PLAN §1 row 9) on the M7 report fixture."""
from __future__ import annotations

import json
import sqlite3

import pytest

from duplexbench.gates import GATES, check_run, print_gate
from duplexbench.storage import Store
from duplexbench.thresholds import Thresholds

import _report_fixture as fx

TH = Thresholds()


def _patch_run(path, **changes):
    """Rewrite the fixture's runs row (INSERT OR REPLACE) with M8 pre-registration fields."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (fx.RUN,)).fetchone())
    conn.close()
    cfg = json.loads(row["config_json"])
    cfg["comparisons"] = [{"metric": "pass_at_1", "a": "sysA", "b": "sysB",
                           "delta": 0.2, "discordant": 0.4}]
    cfg["coverage"] = {"pass_at_1": 12, "yield_rate@1000": 4}
    for k, v in changes.items():
        if k in ("calibration_json",):
            row[k] = v
        else:
            cfg[k] = v
    row["config_json"] = json.dumps(cfg)
    s = Store(path)
    s.put("runs", row)
    s.close()


@pytest.fixture
def db(tmp_path):
    p = fx.build(tmp_path / "events.sqlite")
    _patch_run(p)
    return p


def test_fixture_passes_every_gate(db):
    rep = check_run(db, fx.RUN, TH)
    assert [c.name for c in rep.checks] == list(GATES)
    assert rep.passed, print_gate(rep)
    assert rep.failures == []
    assert rep.harness_invalid["k"] == 0 and rep.harness_invalid["n"] == 37
    assert rep.coverage["pass_at_1"]["sysA"] == {"need": 12, "have": 12}
    # exclusions reported per system: sysA carries one of each exclusion tag on yield_rate
    assert rep.exclusion_rates["sysA"]["ambiguous_end"]["k"] >= 1
    assert set(rep.exclusion_rates) == {"sysA", "sysB"}
    # planner note on the pre-registered comparison: 77 pairs needed (spec 9.2: about 75), 12 present
    assert any("needs 77 pairs, has 12" in n and "UNDERPOWERED" in n for n in rep.power_notes)
    text = print_gate(rep)
    assert text.startswith("gate r1: PASS") and "power notes" in text
    text.encode("ascii")


def test_missing_calibration_is_a_named_failure(db):
    _patch_run(db, calibration_json=None)
    rep = check_run(db, fx.RUN, TH)
    assert not rep.passed
    assert "calibration_passed" in rep.failures
    assert rep.check("calibration_passed").detail == "no calibration recorded"
    assert "run_card_complete" in rep.failures and "calibration" in rep.run_card_missing


def test_harness_invalid_share_over_max(tmp_path):
    p = fx.build(tmp_path / "events.sqlite")
    _patch_run(p)
    s = Store(p)
    for i in range(2):                    # 2 of 39 calls > 2 %
        fx._call(s, f"bad{i}", "s1", 9, "C0", "sysA", outcome="transport_error")
    s.close()
    rep = check_run(p, fx.RUN, TH)
    assert "harness_invalid_share" in rep.failures
    assert rep.harness_invalid["reasons"] == {"transport_error": 2}


def test_load_invalid_and_pacing_count_as_harness_invalid(tmp_path):
    from duplexbench.gates import harness_invalid_reason
    assert harness_invalid_reason({"outcome": "completed",
                                   "flags_json": json.dumps({"load_invalid": True})}, TH) == "load_invalid"
    slip = TH.pacing_slip_p99_ms + 1
    assert harness_invalid_reason({"outcome": "completed",
                                   "flags_json": json.dumps({"pacing_slip_p99_ms": slip})},
                                  TH) == "pacing_fail"
    assert harness_invalid_reason({"outcome": "timeout", "flags_json": "{}"}, TH) is None


def test_coverage_and_attempts(tmp_path):
    p = fx.build(tmp_path / "events.sqlite")
    _patch_run(p, coverage={"pass_at_1": 100}, planned_calls=1000)
    rep = check_run(p, fx.RUN, TH)
    assert "coverage_n_met" in rep.failures and "attempts_preserved" in rep.failures
    assert rep.coverage["pass_at_1"]["sysB"] == {"need": 100, "have": 12}


def test_no_preregistered_power_or_coverage_fails(tmp_path):
    p = fx.build(tmp_path / "events.sqlite")      # fixture alone: no delta/discordant, no coverage
    rep = check_run(p, fx.RUN, TH)
    assert {"power_notes", "coverage_n_met"} <= set(rep.failures)
