"""Wilson score interval for a rate (spec §9.1; BUILD_PLAN M7-T01)."""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % (default z) Wilson interval for k successes in n trials."""
    if n <= 0:
        raise ValueError("wilson: n must be > 0")
    if not 0 <= k <= n:
        raise ValueError("wilson: need 0 <= k <= n")
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)
