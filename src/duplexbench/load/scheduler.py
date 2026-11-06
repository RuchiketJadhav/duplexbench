# Copied from C:\Users\Lenovo\research\asr-bench\src\asr_bench\load\scheduler.py (asr-bench, same author).
# Unchanged except this header (BUILD_PLAN M9-T04).
"""Arrival schedules.

Closed loop (level 1): one session at a time, back to back. Labelled "closed".
Open loop (level > 1) [D21]: Poisson arrivals at rate
    lambda = target_concurrency / mean_session_s
so the offered load does not depend on how fast the vendor answers. A slow
vendor gets more simultaneous streams, exactly as production would.
Phase is assigned by *scheduled arrival time*:
    ramp      t < ramp_s
    sustain   ramp_s <= t <= last_arrival - cool_s
    cooldown  t > last_arrival - cool_s
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Arrival:
    index: int
    t_s: float
    phase: str


def open_loop(n: int, level: float, mean_session_s: float, seed: int,
              ramp_s: float | None = None, cool_s: float | None = None) -> list[Arrival]:
    rate = level / mean_session_s
    rng = random.Random(seed)
    t, times = 0.0, []
    for _ in range(n):
        times.append(t)
        t += rng.expovariate(rate)
    ramp = mean_session_s if ramp_s is None else ramp_s
    cool = mean_session_s if cool_s is None else cool_s
    last = times[-1] if times else 0.0
    out = []
    for i, ti in enumerate(times):
        phase = "ramp" if ti < ramp else ("cooldown" if ti > last - cool else "sustain")
        out.append(Arrival(i, ti, phase))
    return out


def closed_loop(n: int) -> list[Arrival]:
    return [Arrival(i, 0.0, "sustain") for i in range(n)]
