"""M2-T11: early text report from hand-written observation rows."""
import json
import re

import pytest

from duplexbench.metrics.names import Metric
from duplexbench.report.text import CI_PLACEHOLDER, UnknownMetricError, render


def _obs(store, metric, value, valid=True, tags=(), system="mock-a", run="r1", call="c1"):
    store.put("observations", dict(run_id=run, call_id=call, metric=metric, scenario_id="s1",
                                   trial=0, condition="C0", system_id=system, event_id="o1",
                                   value=value, valid=int(valid), tags_json=json.dumps(list(tags))))


@pytest.fixture
def filled(store):
    for b, vals in ((500, [1, 0]), (1000, [1, 1, 0]), (2000, [1, 1, 1])):
        for v in vals:
            _obs(store, "yield_rate", v, tags=(f"budget={b}",))
    _obs(store, "yield_rate", None, valid=False, tags=("budget=1000", "ambiguous_end"))
    _obs(store, "hold_speaking_rate", 1.0)
    _obs(store, "hold_speaking_rate", 0.0, system="mock-b")
    _obs(store, "hold_speaking_rate", None, valid=False, tags=("not_injected",))
    _obs(store, "t_stop_ear", 250.0)
    _obs(store, "t_stop_ear", 350.0)
    _obs(store, "t_stop_ear", 900.0, tags=("contested",))
    _obs(store, "respond_resume", 1.0, tags=("classifier", "label=respond"))
    _obs(store, "tor", 1.0, run="other-run")          # another run: must not appear
    return store


def test_each_present_metric_once_with_counts(filled):
    out = render(filled, "r1")
    for m in ("yield_rate", "hold_speaking_rate", "t_stop_ear", "respond_resume"):
        assert len(re.findall(rf"^{m}\b", out, re.M)) == 1, m
    assert "tor" not in re.findall(r"^\w+", out, re.M)
    assert CI_PLACEHOLDER in out
    lines = out.splitlines()
    y1000 = next(l for l in lines if "budget=1000" in l)
    cells = y1000.split()
    # variant n n_all mean p50
    i = cells.index("budget=1000")
    assert cells[i + 1:i + 5] == ["3", "4", "0.6667", "1.0000"]
    assert "ambiguous_end:1" in y1000
    ts = next(l for l in lines if l.startswith("t_stop_ear"))
    assert ts.split()[3:7] == ["3", "3", "500.0000", "350.0000"]
    assert "classifier:1" in out and "label=respond:1" in out
    # budgets ordered numerically
    assert out.index("budget=500") < out.index("budget=1000") < out.index("budget=2000")


def test_caveat_lines(filled):
    out = render(filled, "r1")
    cav = [l for l in out.splitlines() if l.startswith("caveat:")]
    assert any("ambiguous_end on 1" in l and "yield_rate:1" in l for l in cav)
    assert any("not_injected on 1" in l for l in cav)
    assert any("contested on 1" in l and "t_stop_ear:1" in l for l in cav)
    assert not any("unmatched_control" in l for l in cav)


def test_rejects_unknown_metric(store):
    _obs(store, "yield_rate", 1.0)
    _obs(store, "composite_score", 0.5)
    with pytest.raises(UnknownMetricError, match="composite_score"):
        render(store, "r1")


def test_empty_run_and_path_input(store):
    _obs(store, "yield_rate", 1.0)
    store.flush_sync()
    assert "(no observations)" in render(store, "nope")
    assert "yield_rate" in render(store.path, "r1")
    assert all(m.value for m in Metric)
