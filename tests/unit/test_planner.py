import pytest

from duplexbench.stats.planner import half_width, pairs_needed, percentile_min_n


@pytest.mark.parametrize("n,pts", [(50, 13.6), (100, 9.7), (200, 6.9), (400, 4.9)])
def test_half_width_matches_spec_table(n, pts):
    assert abs(half_width(n) * 100 - pts) <= 0.3


@pytest.mark.parametrize("n,pts", [(50, 13.36), (100, 9.62), (200, 6.86), (400, 4.88)])
def test_half_width_exact_wilson(n, pts):
    # hand arithmetic, z = 1.96: half = z*sqrt(.25/n + z^2/(4n^2)) / (1 + z^2/n)
    assert abs(half_width(n) * 100 - pts) <= 0.01


def test_pairs_needed_10pt_30pct():
    assert abs(pairs_needed(0.10, 0.30) - 233) <= 5


def test_pairs_needed_20pt_40pct():
    assert abs(pairs_needed(0.20, 0.40) - 76) <= 5


def test_pairs_needed_exact():
    # (1.95996*sqrt(.3) + .84162*sqrt(.29))^2 / .01 = 233.1 -> 234
    assert pairs_needed(0.10, 0.30) == 234
    # (1.95996*sqrt(.4) + .84162*sqrt(.36))^2 / .04 = 76.09 -> 77
    assert pairs_needed(0.20, 0.40) == 77


def test_percentile_min_n():
    assert [percentile_min_n(p) for p in (50, 90, 95, 99)] == [30, 100, 200, 1000]
    with pytest.raises(ValueError):
        percentile_min_n(75)
