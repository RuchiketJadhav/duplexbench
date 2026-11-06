"""Clustered and paired bootstrap, Holm correction (spec §9.1; BUILD_PLAN M7-T01).

Ported from asr-bench `src/asr_bench/metrics/stats.py` (same author, copied not
imported): `_resample_ids`, `ci` and the ID-based paired difference (renamed
`paired_diff_ids` here) are verbatim; `bootstrap_ci` is generalised to values +
clusters + statistic and reproduces the asr-bench boolean-mean CI exactly when
called the old way, `bootstrap_ci(flags)`.

New for duplex-bench:
  * `paired_diff(pairs, stat, resamples, seed)` on the pair-key rows of spec §9.1 /
    BUILD_PLAN §1 row 12: each pair is (a, b, cluster); a side that is invalid for
    the metric is None and the whole pair is dropped; drops are counted per side.
  * `holm(pvalues)` step-down correction.

Paired: both systems are scored on the same resampled pairs and the statistic is
recomputed on each resample. "Distinguishable" comes from the CI of the
*difference*, never from whether two separate CIs overlap. Clustered: whole
clusters (scenario_id) are resampled.
"""
from __future__ import annotations

import random
from typing import Callable, Hashable, Sequence

RESAMPLES = 1000  # threshold-ok: asr-bench default; callers pass thresholds.bootstrap_resamples
SEED = 12345


def _mean(xs: list) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pct(sorted_vals: list[float], alpha: float) -> tuple[float, float]:
    n = len(sorted_vals)
    return sorted_vals[int(alpha / 2 * n)], sorted_vals[min(n - 1, int((1 - alpha / 2) * n))]


# ---- verbatim asr-bench ------------------------------------------------------

def _resample_ids(ids: Sequence[Hashable], clusters: dict | None, rng: random.Random) -> list:
    if not clusters:
        return [ids[rng.randrange(len(ids))] for _ in ids]
    groups: dict = {}
    for i in ids:
        groups.setdefault(clusters.get(i, i), []).append(i)
    keys = list(groups)
    out = []
    for _ in keys:
        out.extend(groups[keys[rng.randrange(len(keys))]])
    return out


def ci(ids: Sequence[Hashable], stat: Callable[[list], float | None],
       clusters: dict | None = None, resamples: int = RESAMPLES, seed: int = SEED,
       alpha: float = 0.05) -> tuple[float | None, float | None, float | None]:
    """(point, lo, hi) for any statistic of a list of sample IDs."""
    point = stat(list(ids))
    if point is None or not ids:
        return point, None, None
    rng = random.Random(seed)
    vals = [v for v in (stat(_resample_ids(ids, clusters, rng)) for _ in range(resamples))
            if v is not None]
    vals.sort()
    lo = vals[int(alpha / 2 * len(vals))]
    hi = vals[min(len(vals) - 1, int((1 - alpha / 2) * len(vals)))]
    return point, lo, hi


def paired_diff_ids(ids: Sequence[Hashable], stat_a: Callable[[list], float | None],
                    stat_b: Callable[[list], float | None], clusters: dict | None = None,
                    resamples: int = RESAMPLES, seed: int = SEED, alpha: float = 0.05) -> dict:
    """asr-bench `paired_diff`: CI of stat_a - stat_b on identical resamples of IDs."""
    ids = list(ids)
    a0, b0 = stat_a(ids), stat_b(ids)
    if a0 is None or b0 is None:
        return {"diff": None, "lo": None, "hi": None, "verdict": "insufficient data"}
    rng = random.Random(seed)
    diffs = []
    for _ in range(resamples):
        rs = _resample_ids(ids, clusters, rng)
        a, b = stat_a(rs), stat_b(rs)
        if a is not None and b is not None:
            diffs.append(a - b)
    diffs.sort()
    lo = diffs[int(alpha / 2 * len(diffs))]
    hi = diffs[min(len(diffs) - 1, int((1 - alpha / 2) * len(diffs)))]
    verdict = ("A lower" if hi < 0 else "B lower" if lo > 0 else "not distinguishable")
    return {"diff": a0 - b0, "lo": lo, "hi": hi, "verdict": verdict, "n": len(ids)}


# ---- generalised / new -------------------------------------------------------

def bootstrap_ci(values: Sequence, clusters: Sequence[Hashable] | None = None,
                 stat: Callable[[list], float | None] | None = None,
                 resamples: int = RESAMPLES, seed: int = SEED) -> tuple[float, float]:
    """Percentile CI (lo, hi) of `stat` (default: mean) over `values`, resampling whole
    clusters when `clusters` (one key per value) is given. With no clusters and the
    default stat this is exactly asr-bench `bootstrap_ci(flags)` (dograh-experiments
    score.py@cd2ed09): same RNG draws, same percentile indices."""
    if not values:
        return (0.0, 0.0)
    stat = stat or _mean
    vals = list(values)
    ids = list(range(len(vals)))
    cl = {i: c for i, c in zip(ids, clusters)} if clusters is not None else None
    rng = random.Random(seed)
    stats = []
    for _ in range(resamples):
        s = stat([vals[i] for i in _resample_ids(ids, cl, rng)])
        if s is not None:
            stats.append(s)
    stats.sort()
    m = len(stats)
    return stats[int(0.025 * m)], stats[int(0.975 * m) - 1]


class PairedDiff(tuple):
    """(diff, lo, hi) that also carries n, drop counts and the CI-only verdict."""
    diff: float | None
    lo: float | None
    hi: float | None
    n: int
    dropped: int
    dropped_a: int          # pairs dropped because side A was invalid
    dropped_b: int          # pairs dropped because side B was invalid
    verdict: str

    def __new__(cls, diff, lo, hi, n, dropped, dropped_a, dropped_b, verdict):
        self = super().__new__(cls, (diff, lo, hi))
        self.diff, self.lo, self.hi = diff, lo, hi
        self.n, self.dropped, self.dropped_a, self.dropped_b = n, dropped, dropped_a, dropped_b
        self.verdict = verdict
        return self


def paired_diff(pairs: Sequence[tuple], stat: Callable[[list], float | None] | None = None,
                resamples: int = RESAMPLES, seed: int = SEED, alpha: float = 0.05) -> PairedDiff:
    """Paired, cluster-resampled difference stat(A) - stat(B).

    `pairs`: rows (a, b, cluster) already matched on the pair key (scenario_id,
    trial_index, condition), or (scenario_id, overlay_id, trial_index) for overlay
    events; cluster = scenario_id. A side that is invalid for this metric is None;
    such a pair is dropped and counted (`dropped_a` / `dropped_b`; a pair invalid on
    both sides counts in both, once in `dropped`). Returns (diff, lo, hi) with
    `.n`, `.dropped`, `.dropped_a`, `.dropped_b`, `.verdict` attributes.
    """
    stat = stat or _mean
    kept, dropped, da, db = [], 0, 0, 0
    for a, b, c in pairs:
        if a is None or b is None:
            dropped += 1
            da += a is None
            db += b is None
            continue
        kept.append((a, b, c))
    if not kept:
        return PairedDiff(None, None, None, 0, dropped, da, db, "insufficient data")
    a0, b0 = stat([p[0] for p in kept]), stat([p[1] for p in kept])
    if a0 is None or b0 is None:
        return PairedDiff(None, None, None, len(kept), dropped, da, db, "insufficient data")
    ids = list(range(len(kept)))
    cl = {i: kept[i][2] for i in ids}
    rng = random.Random(seed)
    diffs = []
    for _ in range(resamples):
        rs = _resample_ids(ids, cl, rng)
        a, b = stat([kept[i][0] for i in rs]), stat([kept[i][1] for i in rs])
        if a is not None and b is not None:
            diffs.append(a - b)
    diffs.sort()
    lo, hi = _pct(diffs, alpha)
    verdict = ("A lower" if hi < 0 else "B lower" if lo > 0 else "not distinguishable")
    return PairedDiff(a0 - b0, lo, hi, len(kept), dropped, da, db, verdict)


def holm(pvalues: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm step-down: reject[i] for each hypothesis, in input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    reject = [False] * m
    for rank, i in enumerate(order):
        if pvalues[i] <= alpha / (m - rank):
            reject[i] = True
        else:
            break
    return reject
