"""Seeded Poisson noise bursts for condition C1 (spec §5; BUILD_PLAN M1-T05).

Burst onsets form a Poisson process at `per_min` per minute; each burst lasts
U(len_ms[0], len_ms[1]) ms and is white noise at `level_db` dBFS RMS, added to the
signal. State carries across `apply` calls (a burst may straddle chunks).
`events` lists every burst started so far as (start_sample, length_samples).
"""
from __future__ import annotations

import numpy as np

_S_PER_MIN = 60.0
_MS_PER_S = 1e3
_FULL_SCALE = 32768.0


class Bursts:
    def __init__(self, seed: int, per_min: float, len_ms: tuple[float, float] | list[float],
                 level_db: float, rate: int):
        self.seed, self.per_min, self.rate = seed, float(per_min), int(rate)
        self.len_ms = (float(len_ms[0]), float(len_ms[1]))
        self.level_db = float(level_db)
        self._rng = np.random.default_rng(seed)
        self._pos = 0
        self.events: list[tuple[int, int]] = []
        self._next_start = self._draw_gap()
        self._cur_end = 0                    # end sample of the burst in progress

    def _draw_gap(self) -> int:
        if self.per_min <= 0:
            return np.iinfo(np.int64).max
        mean_s = _S_PER_MIN / self.per_min
        return self._pos_base() + int(round(self._rng.exponential(mean_s) * self.rate))

    def _pos_base(self) -> int:
        return self.events[-1][0] if self.events else 0

    def apply(self, pcm_stream: np.ndarray) -> np.ndarray:
        x = np.asarray(pcm_stream, dtype=np.float64).ravel()
        n = len(x)
        start, end = self._pos, self._pos + n
        noise = np.zeros(n)
        sigma = _FULL_SCALE * 10 ** (self.level_db / 20)
        # continue a burst that began in an earlier chunk
        if self._cur_end > start:
            k = min(self._cur_end, end) - start
            noise[:k] = self._rng.standard_normal(k) * sigma
        while self._next_start < end:
            s = self._next_start
            length = int(round(self._rng.uniform(*self.len_ms) / _MS_PER_S * self.rate))
            self.events.append((s, length))
            self._cur_end = s + length
            a, b = s - start, min(s + length, end) - start
            noise[a:b] = self._rng.standard_normal(b - a) * sigma
            self._next_start = self._draw_gap()
            if self._next_start < self._cur_end:     # no overlap: next onset after this burst
                self._next_start = self._cur_end
        self._pos = end
        if not noise.any():
            return np.asarray(pcm_stream, dtype=np.int16).ravel().copy()
        return np.clip(np.rint(x + noise), -32768, 32767).astype(np.int16)
