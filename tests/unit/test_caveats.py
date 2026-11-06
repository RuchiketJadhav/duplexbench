"""M7-T04: every spec §10 item 10 condition produces a caveat line on the fixture."""
import pytest

from _report_fixture import build
from duplexbench.report.aggregate import aggregate
from duplexbench.report.caveats import caveats, lines
from duplexbench.thresholds import Thresholds

TH = Thresholds()


@pytest.fixture(scope="module")
def cav(tmp_path_factory):
    p = build(tmp_path_factory.mktemp("cav") / "e.sqlite")
    return caveats(aggregate(p, "r1", TH), TH)


def _texts(cav, kind):
    return [t for k, t in cav if k == kind]


def test_low_n_percentiles(cav):
    low = _texts(cav, "low_n")
    assert any("t_turn_gap_ear [sysA, C0] n=10" in t and "p90" in t for t in low)


@pytest.mark.parametrize("tag", ["ambiguous_end", "not_injected", "unmatched_control",
                                 "contested"])
def test_exclusion_counts(cav, tag):
    assert any(tag in t and t.startswith("sysA:") for t in _texts(cav, "exclusion"))


@pytest.mark.parametrize("tag", ["slow_caller", "unscripted_overlap"])
def test_simulator_counts(cav, tag):
    assert any(tag in t for t in _texts(cav, "simulator"))


def test_mishearing_and_regeneration_rates(cav):
    sim = _texts(cav, "simulator")
    assert any("mishearing rate 0.250" in t for t in sim)
    # 0.25 > sim_regen_review_rate (0.10): flagged for review
    assert any("regeneration rate 0.250" in t and "review" in t for t in sim)


def test_phone_path_disagreement(cav):
    assert any("yield_rate" in t for t in _texts(cav, "phone_path"))


def test_unvalidated_classifier(cav):
    (t,) = _texts(cav, "classifier")
    assert "unsupported_claim_rate" in t and "not validated" in t and "not measured" in t


def test_validated_classifier_line(tmp_path):
    p = build(tmp_path / "e.sqlite", classifier_kappa=0.81)
    (t,) = _texts(caveats(aggregate(p, "r1", TH), TH), "classifier")
    assert "validated (kappa 0.810)" in t and "not" not in t


def test_missing_cancel_event(cav):
    c = _texts(cav, "cancel")
    assert len(c) == 1 and c[0].startswith("sysB:")


def test_no_caveats_on_clean_aggregate():
    agg = {"metrics": {}, "exclusions": {"x": {"units": 3, "counts": {}}}, "classifiers": {},
           "systems": {"x": {"cancel_event": True}}, "phone_path": {"disagreements": []}}
    assert lines(agg, TH) == []
