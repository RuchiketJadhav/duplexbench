"""Reference-ASR candidate wrappers (BUILD_PLAN M4a-T02; spec §4.3, §7).

One thin `RefASR` wrapper per candidate. Every heavy dependency is imported
lazily, inside the wrapper, so this package imports on a bare install; a
missing package raises `CandidateUnavailable` with the pip command, and a
model that is not on disk raises it too (downloads cost bandwidth and disk and
need approval: `duplexbench refasr-bench --yes`). No candidate is chosen here:
a human picks the model (CLAUDE.md §6 item 3) and pins it in `refasr/pinned.py`.

Helpers shared by the wrappers:

    require(module, pip_hint)   import or raise CandidateUnavailable
    upsample(pcm8k, rate)       8 kHz int16 -> float32 at the model rate
    BufferedStreamer(fn)        Streamer for batch-only engines: buffers frames and
                                transcribes on finish() (no partials; partial
                                latency is then unmeasurable by construction)
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

import numpy as np

from duplexbench.refasr.engine import Word

LINE_RATE = 8000
_FULL_SCALE = 32768.0


class CandidateUnavailable(RuntimeError):
    """A candidate cannot run here; the message says what to install or approve."""


def require(module: str, pip_hint: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise CandidateUnavailable(f"{module} not installed: pip install {pip_hint}") from exc


def has_module(module: str) -> bool:
    """True when `module` can be imported here (checked without importing it)."""
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def upsample(pcm8k: np.ndarray, rate: int) -> np.ndarray:
    """8 kHz int16 line audio -> float32 in [-1, 1] at `rate` (the model's input rate)."""
    from duplexbench.channel.resample import Resampler

    x = np.asarray(pcm8k, dtype=np.int16).ravel()
    if rate != LINE_RATE:
        rs = Resampler(LINE_RATE, rate)
        n = int(round(len(x) * rate / LINE_RATE))
        x = np.concatenate([rs.push(x), rs.flush()])[:n]
    return (x.astype(np.float32) / _FULL_SCALE).astype(np.float32)


def frame_pcm(frame: np.ndarray | bytes) -> np.ndarray:
    """A pushed frame as int16 pcm (mu-law bytes are decoded)."""
    if isinstance(frame, (bytes, bytearray)):
        from duplexbench.channel import mulaw
        return mulaw.decode(bytes(frame))
    return np.asarray(frame, dtype=np.int16).ravel()


class BufferedStreamer:
    """Streamer over a batch engine: no partial words; everything on finish()."""

    def __init__(self, transcribe: Callable[[np.ndarray], list[Word]]) -> None:
        self._fn = transcribe
        self._buf: list[np.ndarray] = []

    def push(self, frame: np.ndarray | bytes) -> list[Word]:
        self._buf.append(frame_pcm(frame))
        return []

    def finish(self) -> list[Word]:
        pcm = np.concatenate(self._buf) if self._buf else np.zeros(0, np.int16)
        self._buf = []
        return self._fn(pcm)


__all__ = ["CandidateUnavailable", "require", "has_module", "upsample", "frame_pcm",
           "BufferedStreamer", "LINE_RATE"]
