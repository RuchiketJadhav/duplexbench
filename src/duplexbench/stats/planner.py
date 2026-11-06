"""Sample-size planner (spec §9.1, §9.2; BUILD_PLAN M7-T02)."""
from __future__ import annotations

import math
from statistics import NormalDist

from duplexbench.stats.wilson import wilson

# spec §9.2 [Fact, arithmetic]: observations for a usable bootstrap CI per percentile
_PERCENTILE_MIN_N = {50: 30, 90: 100, 95: 200, 99: 1000}  # threshold-ok: spec §9.2 table


def half_width(n: int, p: float = 0.5) -> float:
    """Half-width of the 95 % Wilson interval at rate p and n events."""
    if float(p * n).is_integer():
        lo, hi = wilson(int(p * n), n)
    else:
        lo, hi = _wilson_p(p, n)
    return (hi - lo) / 2


def _wilson_p(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return center - half, center + half


def pairs_needed(delta: float, discordant: float, power: float = 0.8, alpha: float = 0.05) -> int:
    """McNemar pairs to detect a paired difference `delta` with discordant share d:
    n = (z_a*sqrt(d) + z_b*sqrt(d - delta^2))^2 / delta^2, two-sided alpha."""
    if not 0 < delta * delta < discordant:
        raise ValueError("pairs_needed: need 0 < delta^2 < discordant")
    nd = NormalDist()
    z_a = nd.inv_cdf(1 - alpha / 2)
    z_b = nd.inv_cdf(power)
    n = (z_a * math.sqrt(discordant) + z_b * math.sqrt(discordant - delta * delta)) ** 2 / (delta * delta)
    return math.ceil(n)


def percentile_min_n(p: int | float) -> int:
    """Minimum observations before percentile p is reported with a CI (spec §9.2)."""
    key = int(p)
    if key not in _PERCENTILE_MIN_N:
        raise ValueError(f"percentile_min_n: no spec §9.2 row for p{p}")
    return _PERCENTILE_MIN_N[key]
