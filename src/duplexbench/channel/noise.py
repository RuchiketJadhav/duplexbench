"""Seeded noise bed for condition C1 (spec §5; BUILD_PLAN M1-T05).

Noise power is set against the mean power of the clean signal's speech-active
frames (the mask comes from the caller, computed with `rms_active_dbfs` from
thresholds; this module imports no threshold). The level drifts as a seeded random
walk, updated once per second and clipped to +/- drift_db.

Streaming: state (rng, drift, sample position, running active-power estimate)
carries across `apply` calls, so applying chunk by chunk equals applying once
when the active power is stationary.
"""
from __future__ import annotations

import numpy as np

_FRAMES_PER_S = 50              # 20 ms frames (BUILD_PLAN §2.2)
_DRIFT_STEP_FRACTION = 0.5      # random-walk step s.d. per second, as a fraction of drift_db


class NoiseBed:
    def __init__(self, seed: int, snr_db: float, drift_db: float, rate: int):
        self.seed, self.snr_db, self.drift_db, self.rate = seed, float(snr_db), float(drift_db), int(rate)
        self._rng = np.random.default_rng(seed)
        self._pos = 0                       # samples emitted so far
        self._drift = 0.0                   # current drift, dB
        self._active_energy = 0.0           # running sum of squares over active samples
        self._active_n = 0

    def _sample_mask(self, mask: np.ndarray, n: int) -> np.ndarray:
        m = np.asarray(mask, dtype=bool).ravel()
        if len(m) == n:
            return m
        frame = self.rate // _FRAMES_PER_S
        return np.repeat(m, frame)[:n] if len(m) * frame >= n else np.pad(
            np.repeat(m, frame), (0, n - len(m) * frame))

    def _drift_track(self, n: int) -> np.ndarray:
        """Per-sample drift (dB) for the next n samples; walk steps on second boundaries."""
        out = np.empty(n)
        i, pos = 0, self._pos
        while i < n:
            if pos % self.rate == 0 and pos > 0 and self.drift_db > 0:
                step = self._rng.normal(0.0, self.drift_db * _DRIFT_STEP_FRACTION)
                self._drift = float(np.clip(self._drift + step, -self.drift_db, self.drift_db))
            k = min(n - i, self.rate - pos % self.rate)
            out[i:i + k] = self._drift
            i += k
            pos += k
        return out

    def apply(self, pcm: np.ndarray, speech_active_mask: np.ndarray) -> np.ndarray:
        """`speech_active_mask`: one bool per sample or one per 20 ms frame."""
        x = np.asarray(pcm, dtype=np.float64).ravel()
        n = len(x)
        m = self._sample_mask(speech_active_mask, n)
        self._active_energy += float(np.sum(x[m] ** 2))
        self._active_n += int(m.sum())
        drift = self._drift_track(n)
        white = self._rng.standard_normal(n)
        self._pos += n
        if self._active_n == 0:             # no speech seen yet: no reference level
            return np.asarray(pcm, dtype=np.int16).ravel().copy()
        p_sig = self._active_energy / self._active_n
        sigma = np.sqrt(p_sig / 10 ** (self.snr_db / 10)) * 10 ** (drift / 20)
        y = x + white * sigma
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16)
