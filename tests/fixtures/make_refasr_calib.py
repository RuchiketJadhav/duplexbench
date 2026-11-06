"""Generate tests/fixtures/refasr_calib_60s.wav (BUILD_PLAN M4-T01; spec §1.4 D4).

60 s, 8 kHz, mono int16 (960 KB). Babble-like speech stand-in, no TTS spend:
syllable-length tone bursts (120-320 ms) with random pitch (110-260 Hz) and a
few harmonics, grouped into "words" with 60-200 ms gaps and 0.4-1.2 s pauses
between phrases, under a slow amplitude envelope. Deterministic (seed 4101).

    uv run python tests/fixtures/make_refasr_calib.py
"""
from pathlib import Path

import numpy as np
import soundfile as sf

RATE = 8_000
DUR_S = 60
SEED = 4101
OUT = Path(__file__).with_name("refasr_calib_60s.wav")


def make(seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = RATE * DUR_S
    y = np.zeros(n)
    t = 0
    while t < n:
        for _ in range(rng.integers(3, 9)):                 # syllables in a phrase
            L = int(rng.uniform(0.12, 0.32) * RATE)
            if t + L >= n:
                break
            tt = np.arange(L) / RATE
            f0 = rng.uniform(110, 260)
            sig = sum(np.sin(2 * np.pi * f0 * h * tt + rng.uniform(0, 6.28)) / h for h in (1, 2, 3, 4))
            env = np.sin(np.pi * np.arange(L) / L) ** 0.7
            y[t:t + L] += sig * env * rng.uniform(0.15, 0.35)
            t += L + int(rng.uniform(0.06, 0.2) * RATE)
        t += int(rng.uniform(0.4, 1.2) * RATE)
    y += rng.normal(0, 0.003, n)
    return np.clip(np.rint(y * 32767 * 0.5), -32768, 32767).astype(np.int16)


if __name__ == "__main__":
    sf.write(str(OUT), make(), RATE, subtype="PCM_16")
    print(OUT, OUT.stat().st_size, "bytes")
