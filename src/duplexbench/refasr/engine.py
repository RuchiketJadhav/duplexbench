"""Reference-ASR engine interface and a scripted fake (BUILD_PLAN M4a-T01; spec §4.3, §7).

    RefASR.transcribe(pcm8k)            batch: whole utterance -> words with times
    RefASR.stream() -> Streamer
        Streamer.push(frame) -> [Word]  partial words released by this frame (may be empty)
        Streamer.finish()    -> [Word]  every word not yet released

Word times are milliseconds from the start of the audio fed to the engine.
`FakeASR(script)` releases each scripted word once the audio pushed so far
reaches the word's `end_ms`, so streaming tests see words appear "as heard".
The real engine is chosen by a human (never-decide #3) and pinned in
`refasr/pinned.py`; this module carries no model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

_MS_PER_S = 1_000
DEFAULT_RATE = 8_000


@dataclass(frozen=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    conf: float = 1.0


@runtime_checkable
class Streamer(Protocol):
    def push(self, frame: np.ndarray | bytes) -> list[Word]: ...
    def finish(self) -> list[Word]: ...


@runtime_checkable
class RefASR(Protocol):
    name: str
    version: str

    def transcribe(self, pcm8k: np.ndarray) -> list[Word]: ...
    def stream(self) -> Streamer: ...


def text_of(words: list[Word]) -> str:
    return " ".join(w.text for w in words)


class FakeStreamer:
    """Releases script words whose end_ms is covered by the audio pushed so far."""

    def __init__(self, script: list[Word], rate: int) -> None:
        self._pending = sorted(script, key=lambda w: (w.end_ms, w.start_ms))
        self._rate = rate
        self._samples = 0
        self._done = False

    @property
    def elapsed_ms(self) -> float:
        return self._samples * _MS_PER_S / self._rate

    def push(self, frame: np.ndarray | bytes) -> list[Word]:
        if self._done:
            raise RuntimeError("push after finish")
        self._samples += len(frame)          # int16 samples, or μ-law bytes (1 per sample)
        out = []
        while self._pending and self._pending[0].end_ms <= self.elapsed_ms:
            out.append(self._pending.pop(0))
        return out

    def finish(self) -> list[Word]:
        self._done = True
        out, self._pending = self._pending, []
        return out


class FakeASR:
    """Scripted engine for tests: batch returns the script; streaming releases it by time."""

    name = "fake"
    version = "0"

    def __init__(self, script: list[Word], rate: int = DEFAULT_RATE) -> None:
        self.script = list(script)
        self.rate = rate

    def transcribe(self, pcm8k: np.ndarray) -> list[Word]:
        return list(self.script)

    def stream(self) -> FakeStreamer:
        return FakeStreamer(self.script, self.rate)
