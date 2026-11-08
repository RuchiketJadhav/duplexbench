"""Reference-ASR engine interface and a scripted fake (BUILD_PLAN M4a-T01; spec §4.3, §7).

    RefASR.transcribe(pcm8k)            batch: whole utterance -> words with times
    RefASR.stream() -> Streamer
        Streamer.push(frame) -> [Word]  partial words released by this frame (may be empty)
        Streamer.finish()    -> [Word]  every word not yet released
    open_stream(asr, provisional=True)  asr.stream(provisional=True) when the engine
                                        supports it, else asr.stream()
    settled(words)                      the final words only (w.final)
    merge_provisional(pending, w)       latest provisional release per word slot (fix-42)
    finalize_if_supported(streamer)     streamer.finalize() when the engine has it (fix-40)

Provisional words (fix-24): `Word.final` is True for a settled word. An engine whose
`stream()` accepts `provisional=True` (Deepgram) may then also release words with
`final=False` before they settle; the same spoken word is then delivered twice, once
provisional (early, its text and times may still change; a text correction is released
again as provisional) and once final (settled; never changes, never repeated). Without
`provisional=True` a stream releases final words only, as every engine did before, so
consumers that never opt in see no change. Consumers that build a transcript keep only
final words (`settled`); consumers that need low latency may read provisional words and
accept they may be corrected.

Finalize (fix-40, optional): a streamer MAY offer `finalize()`, which asks the engine to
settle the words of the audio pushed so far now, mid-stream, without closing the stream
(Deepgram: `{"type":"Finalize"}`, the way pipecat sends it on its local VAD stop). It
returns at once; the settled words come back as final words from later push()/finish()
calls. It is not part of the `Streamer` protocol, so FakeStreamer, BufferedStreamer and
every other engine are unchanged; callers use `finalize_if_supported(streamer)`.

Word times are milliseconds from the start of the audio fed to the engine.
`FakeASR(script)` releases each scripted word once the audio pushed so far
reaches the word's `end_ms`, so streaming tests see words appear "as heard".
The real engine is chosen by a human (never-decide #3) and pinned in
`refasr/pinned.py`; this module carries no model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np

_MS_PER_S = 1_000
DEFAULT_RATE = 8_000


@dataclass(frozen=True)
class Word:
    """One recognized word; times in ms from the start of the audio fed to the engine.

    `final` (fix-24): True = settled, the word will not change and is released once;
    False = provisional (released early from a streaming engine's interim result, only
    on a `stream(provisional=True)`): the same word comes again later as final=True,
    possibly with other text or times. Defaults to True, so every engine that does not
    release provisional words is unchanged."""
    text: str
    start_ms: int
    end_ms: int
    conf: float = 1.0
    final: bool = True


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


def settled(words: list[Word]) -> list[Word]:
    """Only the final (settled) words: what a transcript is built from."""
    return [w for w in words if w.final]


def merge_provisional(pending: list[Word], w: Word) -> list[Word]:
    """Latest provisional release per word slot (fix-42): `pending` with `w` in place of
    every earlier provisional word it overlaps in time (same start, or intervals that
    intersect), sorted by start. Deepgram re-releases a corrected interim word at the same
    chunk index with near-identical times, so a time overlap identifies the slot without
    the index the engine does not expose."""
    keep = [p for p in pending
            if not (p.start_ms == w.start_ms
                    or (p.start_ms < w.end_ms and w.start_ms < p.end_ms))]
    return sorted(keep + [w], key=lambda x: (x.start_ms, x.end_ms))


def finalize_if_supported(streamer: Any) -> bool:
    """Call `streamer.finalize()` when the streamer has one (fix-40); True when called.
    Engines without it are left alone: their finals arrive at their own pace."""
    fn = getattr(streamer, "finalize", None)
    if streamer is None or not callable(fn):
        return False
    fn()
    return True


def open_stream(asr: Any, *, provisional: bool = False) -> Any:
    """`asr.stream(provisional=True)` when asked and the engine's stream() takes that
    keyword (an engine that releases provisional words); otherwise `asr.stream()`."""
    if provisional:
        import inspect
        try:
            params = inspect.signature(asr.stream).parameters
        except (TypeError, ValueError):
            params = {}
        if "provisional" in params:
            return asr.stream(provisional=True)
    return asr.stream()


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
