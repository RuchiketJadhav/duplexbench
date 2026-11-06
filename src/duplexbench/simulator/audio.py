"""Simulated-caller voice: TTS -> 8 kHz line audio (BUILD_PLAN M4-T09; spec §7 "Speaks").

    voice = CallerVoice(tts, voice="v1", rate=1.0, cache_dir=None)
    pcm8k = await voice.synth("my order number is 4 4 7")      # int16, 8 kHz, trimmed
    utt = await voice.synth_utterance(text)                     # pcm + word timestamps
    voice.prepare(key, text); utt = await voice.take(key)       # speculative synthesis
    frames = to_frames(pcm8k)                                   # 20 ms, 160 samples, tail zero-padded

* The TTS call reuses `stimuli.tts.synthesize` (and its on-disk cache when
  `cache_dir` is set). A vendor call is a blocking network wait: it runs in a
  worker thread and is awaited directly (clock rule: never through `clock.idle`).
* Resampling goes through `channel.resample.Resampler` (push + flush; its output
  is already delay-aligned with the input), cut to the nominal length, then
  leading/trailing digital silence is trimmed with word times shifted, so word
  timestamps stay valid on the 8 kHz timeline.
* Speculative cache: `prepare(key, text)` starts synthesis of a reply generated
  from partial context; `take(key)` returns it only when the key still matches
  (key = (prompt_hash, transcript_hash)); otherwise None and the caller
  synthesizes the final text. `discard()` drops every pending entry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from duplexbench.channel.resample import Resampler
from duplexbench.stimuli import tts as _tts
from duplexbench.stimuli.tts import TTSClient, Word

LINE_RATE = 8000
_FRAMES_PER_S = 50
FRAME_SAMPLES = LINE_RATE // _FRAMES_PER_S      # 160
_MS_PER_S = 1_000

SpecKey = tuple[str, str]


def transcript_hash(transcript: Iterable[tuple[str, str]]) -> str:
    """Stable hash of a (role, text) transcript (speculative-cache key half)."""
    data = json.dumps([[r, t] for r, t in transcript], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


@dataclass
class Utterance:
    text: str
    pcm: np.ndarray                               # int16 at LINE_RATE
    words: list[Word] = field(default_factory=list)

    def words_json(self) -> str:
        return json.dumps([asdict(w) for w in self.words], separators=(",", ":"))


def resample_to_line(pcm: np.ndarray, sr: int, dst: int = LINE_RATE) -> np.ndarray:
    """Resample int16 mono `pcm` at `sr` to `dst`, delay-compensated and length-exact."""
    x = np.asarray(pcm, dtype=np.int16).ravel()
    if int(sr) == dst:
        return x.copy()
    rs = Resampler(int(sr), dst)
    y = np.concatenate([rs.push(x), rs.flush()])
    n_out = int(round(len(x) * dst / int(sr)))
    y = y[:n_out]                       # push+flush output is already delay-aligned
    if len(y) < n_out:
        y = np.concatenate([y, np.zeros(n_out - len(y), np.int16)])
    return y.astype(np.int16)


def trim_silence(pcm: np.ndarray, words: list[Word], rate: int = LINE_RATE
                 ) -> tuple[np.ndarray, list[Word]]:
    """Cut leading and trailing digital silence (exact zeros); shift word times."""
    nz = np.flatnonzero(pcm)
    if len(nz) == 0:
        return pcm[:0].copy(), []
    a, b = int(nz[0]), int(nz[-1]) + 1
    shift_ms = a * _MS_PER_S // rate
    out = [Word(w.text, max(0, w.start_ms - shift_ms), max(0, w.end_ms - shift_ms)) for w in words]
    return pcm[a:b].copy(), out


def to_frames(pcm8k: np.ndarray, frame_samples: int = FRAME_SAMPLES) -> list[np.ndarray]:
    """Cut 8 kHz int16 pcm into 20 ms frames; the tail frame is zero-padded."""
    x = np.asarray(pcm8k, dtype=np.int16).ravel()
    n = -(-len(x) // frame_samples)
    buf = np.zeros(n * frame_samples, np.int16)
    buf[:len(x)] = x
    return [buf[i * frame_samples:(i + 1) * frame_samples].copy() for i in range(n)]


class CallerVoice:
    def __init__(self, tts: TTSClient, voice: str, rate: float = 1.0,
                 cache_dir: Path | str | None = None, *, threaded: bool = True) -> None:
        self.tts = tts
        self.voice = voice
        self.rate = float(rate)
        self.cache_dir = cache_dir
        self.threaded = threaded                  # False: call TTS inline (deterministic tests)
        self._spec: dict[SpecKey, asyncio.Future] = {}
        self.calls = 0
        self.spec_hits = 0
        self.spec_discards = 0

    # -- synthesis -------------------------------------------------------------
    def _synth_sync(self, text: str) -> Utterance:
        pcm, sr, words = _tts.synthesize(text, self.voice, self.rate, self.tts, self.cache_dir)
        y = resample_to_line(pcm, sr)
        y, words = trim_silence(y, list(words))
        return Utterance(text, y, words)

    async def synth_utterance(self, text: str) -> Utterance:
        self.calls += 1
        if self.threaded:
            return await asyncio.to_thread(self._synth_sync, text)   # network wait: direct await
        return self._synth_sync(text)

    async def synth(self, text: str) -> np.ndarray:
        """-> int16 pcm at 8 kHz, leading/trailing silence trimmed."""
        return (await self.synth_utterance(text)).pcm

    # -- speculative cache -------------------------------------------------------
    def prepare(self, key: SpecKey, text: str) -> None:
        """Start synthesizing `text` for `key` (replaces an older entry for that key)."""
        old = self._spec.pop(key, None)
        if old is not None and not old.done():
            old.cancel()
        self._spec[key] = asyncio.ensure_future(self.synth_utterance(text))

    def has(self, key: SpecKey) -> bool:
        return key in self._spec

    async def take(self, key: SpecKey) -> Utterance | None:
        """The prepared utterance for `key`, or None (other entries are discarded)."""
        fut = self._spec.pop(key, None)
        self.discard()
        if fut is None:
            return None
        self.spec_hits += 1
        return await fut

    def discard(self) -> None:
        for f in self._spec.values():
            self.spec_discards += 1
            if not f.done():
                f.cancel()
        self._spec.clear()
