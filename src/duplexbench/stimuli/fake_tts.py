"""Deterministic fake TTS vendor for tests and goldens (BUILD_PLAN M2-T04: "with a fake vendor client").

Each word is a tone burst whose length is proportional to the word's character
count (divided by the speaking rate), separated by short silences. Word
timestamps are exact by construction. No network, no spend.
"""
from __future__ import annotations

import numpy as np

from duplexbench.stimuli.tts import Word

_MS_PER_S = 1000              # threshold-ok: unit conversion ms per s


class FakeTTS:
    vendor = "fake"

    def __init__(self, sample_rate: int = 24000, ms_per_char: float = 65.0,
                 gap_ms: float = 80.0, freq_hz: float = 220.0, level_dbfs: float = -20.0):
        self.sample_rate = sample_rate
        self.ms_per_char = ms_per_char
        self.gap_ms = gap_ms
        self.freq_hz = freq_hz
        self.level_dbfs = level_dbfs
        self.calls = 0

    def synthesize(self, text: str, voice: str, rate: float) -> tuple[np.ndarray, int, list[Word]]:
        self.calls += 1
        sr = self.sample_rate
        amp = 10 ** (self.level_dbfs / 20) * 32768 * np.sqrt(2)   # sine RMS = level_dbfs (full scale 32768)
        # a voice-dependent pitch keeps distinct voices distinguishable in the mix
        f = self.freq_hz * (1 + (sum(map(ord, voice)) % 7) / 10)
        parts: list[np.ndarray] = []
        words: list[Word] = []
        t_ms = 0
        for i, w in enumerate(text.split()):
            if i:
                gap = int(round(self.gap_ms / rate))
                parts.append(np.zeros(gap * sr // _MS_PER_S, np.int16))
                t_ms += gap
            dur = max(1, int(round(len(w) * self.ms_per_char / rate)))
            n = dur * sr // _MS_PER_S
            tt = np.arange(n) / sr
            parts.append(np.clip(np.rint(amp * np.sin(2 * np.pi * f * tt)), -32768, 32767)
                         .astype(np.int16))
            words.append(Word(text=w, start_ms=t_ms, end_ms=t_ms + dur))
            t_ms += dur
        pcm = np.concatenate(parts) if parts else np.zeros(0, np.int16)
        return pcm, sr, words
