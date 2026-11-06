"""Stereo call recording (spec §1.1; BUILD_PLAN §2.2, M1-T10).

    left  = caller audio as delivered to the system under test (decoded)
    right = agent audio at the caller's ear (playout output, spec §1.3)

16-bit PCM WAV at the condition rate (8 kHz; 16 kHz under W). Each channel is
placed by time: a frame stamped `t_ns` starts at sample
round((t_ns - origin_ns) * rate / 1e9);
a gap before it is padded with zeros, so the channels stay aligned by frame index
even when one side writes nothing for a while. Rows are streamed to the file as
soon as both channels have them; `close()` pads the shorter channel with zeros.

The event log has its own writer thread (storage.py); WAV writes are small
(one 20 ms block) and happen on the loop.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import soundfile as sf

_NS_PER_S = 1_000_000_000


class Recorder:
    def __init__(self, path: Path | str, rate: int, origin_ns: int = 0):
        self.path = Path(path)
        self.rate = int(rate)
        self.origin_ns = int(origin_ns)      # clock time of sample 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f: sf.SoundFile | None = sf.SoundFile(
            str(self.path), mode="w", samplerate=self.rate, channels=2, subtype="PCM_16",
            format="WAV")
        self._buf: list[list[np.ndarray]] = [[], []]
        self._pending = [0, 0]       # samples buffered, not yet written, per channel
        self._pos = [0, 0]           # samples placed so far (written + buffered), per channel
        self._written = 0            # stereo rows written
        self._sha: str | None = None

    # -- writing ------------------------------------------------------------
    def write_left(self, frame_pcm: np.ndarray, t_ns: int) -> None:
        self._put(0, frame_pcm, t_ns)

    def write_right(self, frame_pcm: np.ndarray, t_ns: int) -> None:
        self._put(1, frame_pcm, t_ns)

    def _put(self, ch: int, frame_pcm: np.ndarray, t_ns: int) -> None:
        if self._f is None:
            raise RuntimeError("recorder is closed")
        x = np.asarray(frame_pcm, dtype=np.int16).ravel()
        start = int(round((t_ns - self.origin_ns) * self.rate / _NS_PER_S))
        if start > self._pos[ch]:
            self._append(ch, np.zeros(start - self._pos[ch], np.int16))
        self._append(ch, x)
        self._drain()

    def _append(self, ch: int, x: np.ndarray) -> None:
        if len(x):
            self._buf[ch].append(x)
            self._pending[ch] += len(x)
            self._pos[ch] += len(x)

    def _take(self, ch: int, n: int) -> np.ndarray:
        out = np.concatenate(self._buf[ch]) if self._buf[ch] else np.zeros(0, np.int16)
        head, tail = out[:n], out[n:]
        self._buf[ch] = [tail] if len(tail) else []
        self._pending[ch] -= n
        return head

    def _drain(self) -> None:
        n = min(self._pending)
        if n <= 0:
            return
        rows = np.stack([self._take(0, n), self._take(1, n)], axis=1)
        self._f.write(rows)
        self._written += n

    # -- end ------------------------------------------------------------------
    @property
    def samples(self) -> int:
        """Stereo rows placed so far (the longer channel)."""
        return max(self._pos)

    def close(self) -> str:
        """Pad the shorter channel, close the file and return its sha256. Idempotent."""
        if self._f is not None:
            n = max(self._pending)
            for ch in (0, 1):
                if self._pending[ch] < n:
                    self._append(ch, np.zeros(n - self._pending[ch], np.int16))
            self._drain()
            self._f.close()
            self._f = None
            self._sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return self._sha or ""
