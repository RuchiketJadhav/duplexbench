# --- copied UNCHANGED from asr-bench tests/test_stats.py (regression contract) -----
# Only the import line differs: module path, and asr-bench's ID-based `paired_diff`
# lives here as `paired_diff_ids` (duplex-bench's `paired_diff` takes pair rows,
# BUILD_PLAN M7-T01), imported under its original name so the bodies stay verbatim.
import random

from duplexbench.stats.paired_bootstrap import _resample_ids, bootstrap_ci, ci
from duplexbench.stats.paired_bootstrap import paired_diff_ids as paired_diff


def _mean(src):
    return lambda s: sum(src[i] for i in s) / len(s)


def test_ported_bootstrap_ci_deterministic():
    flags = [True] * 30 + [False] * 70
    assert bootstrap_ci(flags) == bootstrap_ci(flags)
    lo, hi = bootstrap_ci(flags)
    assert lo < 0.3 < hi


def test_paired_diff_detects_consistent_gap():
    ids = list(range(200))
    r = paired_diff(ids, _mean({i: 0.10 for i in ids}), _mean({i: 0.20 for i in ids}), resamples=200)
    assert r["verdict"] == "A lower" and abs(r["diff"] + 0.10) < 1e-12


def test_paired_diff_noise_is_not_distinguishable():
    rng = random.Random(1)
    ids = list(range(100))
    a = {i: rng.random() for i in ids}
    b = {i: a[i] + rng.uniform(-0.05, 0.05) for i in ids}
    assert paired_diff(ids, _mean(a), _mean(b), resamples=300)["verdict"] == "not distinguishable"


def test_paired_beats_overlap_on_correlated_data():
    # Two models with large per-sample variance but a consistent 1-point gap:
    # separate CIs overlap, the paired difference CI excludes zero.
    rng = random.Random(2)
    ids = list(range(300))
    base = {i: rng.uniform(0, 0.5) for i in ids}
    a = {i: base[i] for i in ids}
    b = {i: base[i] + 0.01 for i in ids}
    _, alo, ahi = ci(ids, _mean(a), resamples=300)
    _, blo, bhi = ci(ids, _mean(b), resamples=300)
    assert alo < bhi and blo < ahi            # overlap
    assert paired_diff(ids, _mean(a), _mean(b), resamples=300)["verdict"] == "A lower"


def test_clustered_resample_keeps_whole_clusters():
    ids = ["a1", "a2", "b1", "b2", "c1"]
    clusters = {"a1": "A", "a2": "A", "b1": "B", "b2": "B", "c1": "C"}
    for seed in range(20):
        rs = _resample_ids(ids, clusters, random.Random(seed))
        for c in ("A", "B"):
            assert len([i for i in rs if clusters[i] == c]) % 2 == 0


def test_ci_point_is_full_sample():
    p, lo, hi = ci([1, 2, 3, 4], lambda s: sum(s) / len(s), resamples=100)
    assert p == 2.5 and lo <= p <= hi
# --- end of copied asr-bench tests ----------------------------------------------

# --- new cases, BUILD_PLAN M7-T01 -----------------------------------------------
import pytest  # noqa: E402

from duplexbench.stats.paired_bootstrap import holm  # noqa: E402
from duplexbench.stats.paired_bootstrap import paired_diff as paired_rows  # noqa: E402
from duplexbench.stats.wilson import wilson  # noqa: E402


def _close(got, want, tol=0.0005):
    return all(abs(g - w) <= tol for g, w in zip(got, want))


def test_wilson_50_of_100():
    assert _close(wilson(50, 100), (0.4038, 0.5962))


def test_wilson_0_of_10():
    assert _close(wilson(0, 10), (0.0, 0.2775))


def test_wilson_rejects_bad_input():
    with pytest.raises(ValueError):
        wilson(1, 0)
    with pytest.raises(ValueError):
        wilson(5, 4)


def test_bootstrap_ci_reproduces_asr_bench_algorithm():
    # independent re-implementation of asr-bench bootstrap_ci (score.py@cd2ed09)
    flags = [True] * 30 + [False] * 70
    rng = random.Random(12345)
    means = sorted(sum(flags[rng.randrange(100)] for _ in range(100)) / 100 for _ in range(1000))
    assert bootstrap_ci(flags) == (means[25], means[974])


def test_bootstrap_ci_clustered_with_stat():
    vals = [1.0, 1.0, 3.0, 3.0, 2.0]
    lo, hi = bootstrap_ci(vals, clusters=["A", "A", "B", "B", "C"], stat=max, resamples=200, seed=1)
    assert 1.0 <= lo <= hi <= 3.0


def test_paired_rows_identical_sides():
    rng = random.Random(3)
    pairs = [(v, v, f"s{i % 10}") for i, v in enumerate(rng.random() for _ in range(100))]
    diff, lo, hi = paired_rows(pairs, resamples=300, seed=1)
    assert diff == 0 and lo <= 0 <= hi


def test_paired_rows_constant_gap_excludes_zero():
    rng = random.Random(4)
    pairs = []
    for i in range(100):
        b = rng.random()
        pairs.append((b + 0.2, b, f"s{i % 10}"))
    r = paired_rows(pairs, resamples=300, seed=1)
    diff, lo, hi = r
    assert abs(diff - 0.2) < 1e-9 and lo > 0 and r.verdict == "B lower"


def test_paired_rows_asymmetric_drop_is_counted():
    pairs = [(1.0, 0.0, "s1"), (1.0, None, "s1"), (None, 0.0, "s2"), (None, None, "s3"),
             (1.0, 0.0, "s2"), (0.0, 0.0, "s3")]
    r = paired_rows(pairs, resamples=200, seed=1)
    assert r.n == 3 and r.dropped == 3
    assert r.dropped_a == 2 and r.dropped_b == 2
    assert abs(r.diff - 2 / 3) < 1e-12           # only the three valid pairs count


def test_paired_rows_all_dropped():
    r = paired_rows([(None, 1.0, "s"), (1.0, None, "s")])
    assert tuple(r) == (None, None, None) and r.dropped == 2 and r.verdict == "insufficient data"


def test_holm():
    assert holm([0.01, 0.04, 0.03]) == [True, False, False]
    assert holm([0.01, 0.02, 0.03]) == [True, True, True]
    assert holm([]) == []
