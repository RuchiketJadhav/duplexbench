"""Regenerate tests/fixtures/tone_segments.wav (BUILD_PLAN M1-T13 fixture).

Three bursts of the mock agent's speech-like voice (`voice="babble"`: harmonic
formant babble, -20 dBFS, 8 kHz mono int16) separated by silence, each burst
starting the voice afresh like a mock reply. A pure tone is not used: Silero
scores a pure tone as non-speech (p < 0.15), so it could not cross-check anything.
Bursts are >= 640 ms: Silero is unreliable on shorter synthetic fragments. Deterministic: rerunning gives identical bytes.

    uv run python tests/fixtures/make_tone_segments.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from duplexbench.adapters.mock_agent import babble_chunk

RATE = 8000
# (silence_before_ms, burst_ms); then trailing silence
LAYOUT = [(500, 900), (700, 1200), (900, 1000)]
TAIL_MS = 500
PATH = Path(__file__).with_name("tone_segments.wav")


def segments_ms() -> list[tuple[int, int]]:
    out, t = [], 0
    for gap, dur in LAYOUT:
        t += gap
        out.append((t, t + dur))
        t += dur
    return out


def build() -> np.ndarray:
    parts = []
    for gap, dur in LAYOUT:          # every burst starts the voice afresh, like a mock reply
        parts.append(np.zeros(gap * RATE // 1_000, np.int16))
        parts.append(babble_chunk(RATE, 0, dur * RATE // 1_000))
    parts.append(np.zeros(TAIL_MS * RATE // 1_000, np.int16))
    return np.concatenate(parts)


def main() -> Path:
    sf.write(str(PATH), build(), RATE, subtype="PCM_16")
    return PATH


if __name__ == "__main__":
    print(main())
