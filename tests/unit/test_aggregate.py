"""M7-T03: aggregation on a hand-computed two-system fixture store."""
import math

import pytest

from _report_fixture import HASHES, build
from duplexbench.report.aggregate import (INSUFFICIENT, MixedHashError, TooManyComparisonsError,
                                          UnknownMetricError, aggregate, percentile, to_json)
from duplexbench.thresholds import Thresholds

TH = Thresholds()


def _wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


@pytest.fixture(scope="module")
def agg(tmp_path_factory):
    p = build(tmp_path_factory.mktemp("agg") / "e.sqlite")
    return aggregate(p, "r1", TH)


def test_rates_counts_and_wilson(agg):
    a = agg["metrics"]["pass_at_1"]["sysA"]["C0"]
    b = agg["metrics"]["pass_at_1"]["sysB"]["C0"]
    assert (a["n"], a["k"], b["n"], b["k"]) == (12, 7, 12, 5)
    assert a["value"] == pytest.approx(7 / 12, abs=1e-6)
    # hand: Wilson(7, 12) = (0.3195, 0.8067)
    assert a["ci"] == pytest.approx([0.3195, 0.8067], abs=5e-4)
    assert a["ci"] == pytest.approx(list(_wilson(7, 12)), abs=1e-6)
    assert a["kind"] == "rate" and a["headline"] and a["markers"] == []
    assert a["breakdowns"]["domain"]["billing"]["k"] == 5   # s1 3 + s2 2
    assert a["breakdowns"]["domain"]["orders"]["n"] == 6


def test_control_twin_not_in_main_rows(agg):
    assert agg["metrics"]["pass_at_1"]["sysA"]["C0"]["n_rows"] == 12
    s = agg["interrupted_vs_clean"]["pass_at_1"]["sysA"]["C0"]
    assert (s["interrupted"], s["clean"], s["n"], s["diff"]) == (1.0, 1.0, 1, 0.0)


def test_variant_rows_and_exclusions(agg):
    y = agg["metrics"]["yield_rate@1000"]["sysA"]["C0"]
    assert (y["n"], y["k"], y["n_rows"]) == (4, 3, 8)
    assert y["tags"] == {"ambiguous_end": 1, "contested": 1, "not_injected": 1,
                         "unmatched_control": 1}
    assert agg["metrics"]["yield_rate@1000"]["sysB"]["C0"]["value"] == 0.25
    ex = agg["exclusions"]["sysA"]["counts"]
    assert ex["slow_caller"] == 1 and ex["unscripted_overlap"] == 1 and ex["ambiguous_end"] == 2


def test_percentiles_ci_completion_p99(agg):
    g = agg["metrics"]["t_turn_gap_ear"]["sysA"]["C0"]
    assert g["n"] == 10
    assert g["percentiles"]["p50"]["value"] == 550.0
    assert g["percentiles"]["p90"]["value"] == 910.0
    assert g["percentiles"]["p95"]["value"] == 955.0
    assert g["percentiles"]["p99"] == INSUFFICIENT
    lo, hi = g["percentiles"]["p50"]["ci"]
    assert lo <= 550.0 <= hi and 100 <= lo and hi <= 1000
    assert g["low_n"] == ["p50", "p90", "p95"]
    assert g["completion_rate"] == 1.0     # slow_caller / unscripted_overlap are exclusions
    s = agg["metrics"]["t_stop_ear"]["sysA"]["C0"]
    assert s["completion_rate"] == 0.8     # 4 stopped, 1 not_stopped, 1 ambiguous_end excluded
    assert s["value"] == 350.0


def test_p99_reported_at_min_n(tmp_path):
    th = Thresholds(p99_min_n=10)
    p = build(tmp_path / "e.sqlite")
    g = aggregate(p, "r1", th)["metrics"]["t_turn_gap_ear"]["sysA"]["C0"]
    assert g["percentiles"]["p99"]["value"] == pytest.approx(percentile(
        [100.0 * (k + 1) for k in range(10)], 99))


def test_paired_difference_and_holm(agg):
    (c,) = agg["comparisons"]
    assert (c["metric"], c["a"], c["b"], c["condition"]) == ("pass_at_1", "sysA", "sysB", "C0")
    assert c["diff"] == pytest.approx(2 / 12, abs=1e-6)
    assert (c["n"], c["dropped"], c["dropped_a"], c["dropped_b"]) == (12, 0, 0, 0)
    assert c["ci"][0] <= c["diff"] <= c["ci"][1]
    assert c["verdict"] == "not distinguishable" and not c["distinguishable"]
    assert 0 < c["p"] <= 1


def test_pass_pow_3_by_scenario(agg):
    s = agg["metrics"]["pass_pow_3"]["sysA"]["C0"]
    assert s["by_scenario"] == {"s1": True, "s2": False, "s3": False, "s4": False}
    assert (s["n"], s["value"]) == (4, 0.25)


def test_condition_delta(agg):
    d = agg["condition_deltas"]["pass_at_1"]["sysA"]["C1"]
    assert d["diff"] == pytest.approx(1 / 12, abs=1e-6)
    assert d["minus"] == "C0" and d["n"] == 12


def test_markers_and_flags(agg):
    u = agg["metrics"]["unsupported_claim_rate"]["sysA"]["C0"]
    assert u["markers"] == ["classifier"] and not u["headline"]
    assert agg["metrics"]["anchor_offset_in_utt_ms"]["sysA"]["C0"]["markers"] == ["diagnostic"]
    assert agg["metrics"]["anchor_offset_in_utt_ms"]["sysA"]["C0"]["share_over_1s"] == 0.75
    assert agg["metrics"]["pass_at_1"]["sysB"]["C0"]["self_hosted"] is True
    assert agg["metrics"]["pass_at_1"]["sysA"]["C0"]["self_hosted"] is False
    assert agg["systems"]["sysB"]["cancel_event"] is False


def test_validated_classifier_loses_marker(tmp_path):
    p = build(tmp_path / "e.sqlite", classifier_kappa=0.8)
    u = aggregate(p, "r1", TH)["metrics"]["unsupported_claim_rate"]["sysA"]["C0"]
    assert u["markers"] == []


def test_mixed_hashes_raise(tmp_path):
    p = tmp_path / "e.sqlite"
    build(p, run="r1")
    build(p, run="r2", hashes={**HASHES, "pack": "p2"})
    with pytest.raises(MixedHashError):
        aggregate(p, ["r1", "r2"], TH)


def test_same_hashes_aggregate(tmp_path):
    p = tmp_path / "e.sqlite"
    build(p, run="r1")
    build(p, run="r2")
    # call ids collide across runs in this fixture: calls is keyed by call_id, observations are not
    a = aggregate(p, ["r1", "r2"], TH)
    assert a["metrics"]["pass_at_1"]["sysB"]["C0"]["n"] == 24
    assert a["run_ids"] == ["r1", "r2"]


def test_unknown_metric_raises(tmp_path):
    p = build(tmp_path / "e.sqlite", extra_metric="barge_in_latency")
    with pytest.raises(UnknownMetricError):
        aggregate(p, "r1", TH)


def test_too_many_comparisons_raise(tmp_path):
    comps = [{"metric": "pass_at_1", "a": "sysA", "b": "sysB"}] * (TH.holm_max_comparisons + 1)
    p = build(tmp_path / "e.sqlite", comparisons=comps)
    with pytest.raises(TooManyComparisonsError):
        aggregate(p, "r1", TH)


def test_unknown_comparison_metric_raises(tmp_path):
    p = build(tmp_path / "e.sqlite", comparisons=[{"metric": "composite", "a": "sysA",
                                                     "b": "sysB"}])
    with pytest.raises(UnknownMetricError):
        aggregate(p, "r1", TH)


def test_deterministic(tmp_path, agg):
    p = build(tmp_path / "e.sqlite")
    assert to_json(aggregate(p, "r1", TH)) == to_json(agg)


def test_missing_run_raises(tmp_path):
    p = build(tmp_path / "e.sqlite")
    with pytest.raises(KeyError):
        aggregate(p, "nope", TH)
