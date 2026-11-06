"""Streaming resampler and telephone band-limit (spec §5; BUILD_PLAN M1-T04).

Design note (measured 2026-11-04, python-soxr 1.1.0): soxr.ResampleStream at
LQ/MQ/HQ/VHQ withholds output in blocks, 15 to 128 ms behind the input, released
in bursts (e.g. 16k->8k HQ returns nothing for the first four 20 ms chunks). That
would put a variable, bursty delay on agent audio before the playout queue. So the
rate change itself uses ResampleStream at quality "QQ" (cubic, ~0 ms hold-back,
carries the fractional position across chunks) and the anti-alias / anti-image
filtering is a streaming windowed-sinc FIR owned here, whose hold-back is fixed
and known. `delay_ms` is the measured streaming latency of the whole object.

Output is sample-aligned to input (the FIR's group delay is held back, not added as
leading silence), so the sample-index -> time mapping is exact; `delay_ms` is how
long after an input sample arrives its output sample is released.
"""
from __future__ import annotations

import numpy as np
import soxr

_FRAMES_PER_S = 50          # 20 ms frames (BUILD_PLAN §2.2)
_MS_PER_S = 1e3
_AA_TRANSITION_HZ = 500.0   # anti-alias transition width; stopband starts at the new Nyquist
_HAMMING_K = 3.3            # Hamming window: transition ~ 3.3*fs/N, ~53 dB stopband
_G712_LO_HZ = 300.0         # ITU-T G.712 telephone passband edges (Fact, not [Proposed])
_G712_HI_HZ = 3400.0
_BL_TAPS_PER_KHZ = 32       # band_limit FIR length per kHz of sample rate (257 taps at 8 kHz)


def _lowpass_taps(cutoff_hz: float, rate: int, n: int, window: str = "hamming") -> np.ndarray:
    m = np.arange(n) - (n - 1) / 2
    h = 2 * cutoff_hz / rate * np.sinc(2 * cutoff_hz / rate * m)
    h = h * (np.hamming(n) if window == "hamming" else np.blackman(n))
    return h / h.sum()


def _odd(n: float) -> int:
    k = int(np.ceil(n))
    return k if k % 2 else k + 1


class _StreamFIR:
    """Linear-phase FIR with state; holds back its (N-1)/2 group delay, so output is
    sample-aligned with input and len(out) == len(in) once flushed."""

    def __init__(self, taps: np.ndarray):
        self.h = taps
        self.d = (len(taps) - 1) // 2
        self.tail = np.zeros(len(taps) - 1)
        self.skip = self.d

    def push(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.tail, x])
        y = np.convolve(buf, self.h, mode="valid")
        self.tail = buf[len(buf) - (len(self.h) - 1):]
        if self.skip:
            k = min(self.skip, len(y))
            y = y[k:]
            self.skip -= k
        return y

    def flush(self) -> np.ndarray:
        return self.push(np.zeros(self.d))


class Resampler:
    """Streaming mono int16 resampler. `push` never drops samples; `flush` drains."""

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate, self.dst_rate = int(src_rate), int(dst_rate)
        self._frame_buf = np.zeros(0, np.int16)
        self._build()
        self.delay_ms: float = self._measure_delay_ms()
        self._build()                                   # fresh state after measuring

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        s, d = self.src_rate, self.dst_rate
        self._pre = self._post = None
        self._stream = None
        if s == d:
            return
        cutoff = min(s, d) / 2 - _AA_TRANSITION_HZ / 2
        if d < s:   # anti-alias at the source rate, before decimation
            self._pre = _StreamFIR(_lowpass_taps(cutoff, s, _odd(_HAMMING_K * s / _AA_TRANSITION_HZ)))
        else:       # anti-image at the destination rate, after interpolation
            self._post = _StreamFIR(_lowpass_taps(cutoff, d, _odd(_HAMMING_K * d / _AA_TRANSITION_HZ)))
        self._stream = soxr.ResampleStream(s, d, 1, dtype="float64", quality="QQ")

    def _measure_delay_ms(self) -> float:
        """Push an impulse in 1 ms chunks; latency = input time at which the output
        peak is released minus the peak's own time on the output timeline."""
        if self.src_rate == self.dst_rate:
            return 0.0
        chunk = max(1, int(self.src_rate / _MS_PER_S))
        total = chunk * 100                               # 100 ms of input
        x = np.zeros(total)
        x[0] = 1.0
        outs, released_at, produced = [], [], 0
        for i in range(0, total, chunk):
            o = self._push_float(x[i:i + chunk])
            outs.append(o)
            produced += len(o)
            released_at.append((i + chunk, produced))
        y = np.concatenate(outs + [self._flush_float()])
        peak = int(np.argmax(np.abs(y)))
        consumed = next((c for c, p in released_at if p > peak), total)
        return (consumed / self.src_rate - peak / self.dst_rate) * _MS_PER_S

    # -- streaming ----------------------------------------------------------
    def _push_float(self, x: np.ndarray) -> np.ndarray:
        if self._pre is not None:
            x = self._pre.push(x)
        y = self._stream.resample_chunk(np.ascontiguousarray(x, dtype=np.float64))
        if self._post is not None:
            y = self._post.push(y)
        return y

    def _flush_float(self) -> np.ndarray:
        parts = []
        if self._pre is not None:
            parts.append(self._stream.resample_chunk(self._pre.flush()))
        parts.append(self._stream.resample_chunk(np.zeros(0), last=True))
        y = np.concatenate(parts)
        if self._post is not None:
            y = np.concatenate([self._post.push(y), self._post.flush()])
        return y

    @staticmethod
    def _to_int16(y: np.ndarray) -> np.ndarray:
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16)

    def push(self, pcm: np.ndarray) -> np.ndarray:
        if self._stream is None:
            return np.asarray(pcm, dtype=np.int16).ravel().copy()
        return self._to_int16(self._push_float(np.asarray(pcm, dtype=np.float64).ravel()))

    def flush(self) -> np.ndarray:
        """Drain the filters and the stream; the object is then spent."""
        if self._stream is None:
            return np.zeros(0, np.int16)
        return self._to_int16(self._flush_float())

    def to_frames(self, pcm: np.ndarray, rate: int) -> list[np.ndarray]:
        """Cut pcm (at `rate`) into 20 ms frames; a partial tail stays in the object
        and is prepended on the next call."""
        n = int(rate) // _FRAMES_PER_S
        buf = np.concatenate([self._frame_buf, np.asarray(pcm, dtype=np.int16).ravel()])
        k = len(buf) // n
        frames = [buf[i * n:(i + 1) * n].copy() for i in range(k)]
        self._frame_buf = buf[k * n:].copy()
        return frames


def band_limit(pcm: np.ndarray, rate: int, lo_hz: float = _G712_LO_HZ,
               hi_hz: float = _G712_HI_HZ) -> np.ndarray:
    """Telephone-band (ITU-T G.712 passband) linear-phase band-pass with zero delay
    ('same' alignment). Stateless: apply to whole utterances, not to 20 ms frames."""
    x = np.asarray(pcm, dtype=np.float64).ravel()
    if len(x) == 0:
        return np.zeros(0, np.int16)
    n = _odd(_BL_TAPS_PER_KHZ * rate / _MS_PER_S)
    h = _lowpass_taps(hi_hz, rate, n, "blackman") - _lowpass_taps(lo_hz, rate, n, "blackman")
    d = (n - 1) // 2
    y = np.convolve(x, h, mode="full")[d:d + len(x)]
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)
