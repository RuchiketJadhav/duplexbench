"""Seeded muffling of a share of caller utterances, condition C1 (spec §5; M1-T05).

Each utterance is muffled independently with probability `share` (seeded
Bernoulli); muffling is a linear-phase low-pass at `cutoff_hz` (Blackman-windowed
sinc, zero delay). `muffled` holds the decisions of the last `apply` call.
"""
from __future__ import annotations

import numpy as np

_TAPS_PER_KHZ = 32              # 257 taps at 8 kHz: ~170 Hz transition, ~74 dB stopband
_MS_PER_S = 1e3


def _lowpass(cutoff_hz: float, rate: int) -> np.ndarray:
    n = int(np.ceil(_TAPS_PER_KHZ * rate / _MS_PER_S)) | 1
    m = np.arange(n) - (n - 1) / 2
    h = 2 * cutoff_hz / rate * np.sinc(2 * cutoff_hz / rate * m) * np.blackman(n)
    return h / h.sum()


class Muffle:
    def __init__(self, seed: int, share: float, cutoff_hz: float, rate: int):
        self.seed, self.share, self.cutoff_hz, self.rate = seed, float(share), float(cutoff_hz), int(rate)
        self._rng = np.random.default_rng(seed)
        self._h = _lowpass(self.cutoff_hz, self.rate)
        self.muffled: list[bool] = []

    def _filter(self, pcm: np.ndarray) -> np.ndarray:
        x = np.asarray(pcm, dtype=np.float64).ravel()
        if len(x) == 0:
            return np.zeros(0, np.int16)
        d = (len(self._h) - 1) // 2
        y = np.convolve(x, self._h, mode="full")[d:d + len(x)]
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16)

    def apply(self, utterances: list[np.ndarray]) -> list[np.ndarray]:
        self.muffled = [bool(self._rng.random() < self.share) for _ in utterances]
        return [self._filter(u) if m else np.asarray(u, dtype=np.int16).copy()
                for u, m in zip(utterances, self.muffled)]
