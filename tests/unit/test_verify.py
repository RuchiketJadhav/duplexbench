"""M7-T05: verify recomputes the aggregate from the store and diffs the rendered JSON."""
import json

import pytest

from _report_fixture import build
from duplexbench.report import html as html_mod
from duplexbench.report.aggregate import aggregate
from duplexbench.report.run_card import build as build_card
from duplexbench.report.verify import diff, verify
from duplexbench.thresholds import Thresholds

TH = Thresholds()


@pytest.fixture(params=[False, True], ids=["private", "public"])
def rendered(request, tmp_path):
    db = build(tmp_path / "e.sqlite")
    out = tmp_path / "rep"
    html_mod.render(aggregate(db, "r1", TH), build_card(db, "r1", TH), out, TH,
                    public=request.param, clips=None)
    return db, out


def test_clean_report_verifies(rendered):
    db, out = rendered
    assert verify(db, "r1", out, TH) == []


def test_tampered_json_gives_one_diff(rendered):
    db, out = rendered
    p = out / "aggregate.json"
    agg = json.loads(p.read_text(encoding="utf-8"))
    agg["metrics"]["pass_at_1"]["sysA"]["C0"]["value"] = 0.9
    p.write_text(json.dumps(agg, sort_keys=True, indent=1), encoding="utf-8")
    d = verify(db, "r1", out, TH)
    assert len(d) == 1
    assert d[0].startswith("metrics.pass_at_1.sysA.C0.value: report=0.9 recomputed=0.583333")


def test_store_change_is_caught(rendered):
    db, out = rendered
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE observations SET value = 1 WHERE metric = 'pass_at_1' AND "
                "system_id = 'sysB' AND scenario_id = 's4' AND trial = 0 AND condition = 'C0'")
    con.commit()
    con.close()
    assert verify(db, "r1", out, TH) != []


def test_missing_report(tmp_path):
    db = build(tmp_path / "e.sqlite")
    (d,) = verify(db, "r1", tmp_path / "none", TH)
    assert "not found" in d


def test_diff_shapes():
    assert diff({"a": [1, 2]}, {"a": [1, 2, 3]}) == ["a: length report=2 recomputed=3"]
    assert diff({"a": 1}, {"b": 1}) == ["a: not in recomputed (report=1)",
                                        "b: missing in report (recomputed=1)"]
    assert diff({"x": 0.1 + 0.2}, {"x": 0.3}) == []
    assert diff({"x": True}, {"x": 1}) != []
