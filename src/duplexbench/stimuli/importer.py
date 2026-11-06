"""Stimulus importer: trim, speech onset/end, 24 kHz mono WAV (BUILD_PLAN M2-T03; spec §1.2, §3.1).

    meta = import_clip(Path("raw/hold_on.wav"), Path("scenarios/public-dev/audio"))
    meta.onset_ms, meta.end_ms     # -> CallerStep.speech_onset_ms / speech_end_ms

Leading and trailing silence is trimmed at `ImportParams.trim_dbfs` (short-window
RMS against int16 full scale) with `pad_ms` of the original audio kept on each
side, so a clip with enough leading silence has `onset_ms == pad_ms` and
`duration_ms == speech + 2 * pad_ms`. Caller-side anchors come from these
synthesized/imported timestamps, never from ASR (CLAUDE.md §5).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

_FULL_SCALE = 32768.0
_MS_PER_S = 1000              # threshold-ok: unit conversion ms per s


@dataclass(frozen=True)
class ImportParams:
    """Import-time parameters, not spec thresholds (BUILD_PLAN M2-T03 states them)."""
    trim_dbfs: float = -50.0     # threshold-ok: M2-T03 import trim level, not a spec [Proposed] number
    pad_ms: int = 50             # threshold-ok: M2-T03 import padding, not a spec [Proposed] number
    out_rate: int = 24000        # M2-T03: stored clips are 24 kHz mono
    rms_win_ms: float = 5.0      # detector window; bounds onset error to about half of it


@dataclass(frozen=True)
class ClipMeta:
    onset_ms: int        # speech onset within the written clip
    end_ms: int          # speech end within the written clip
    duration_ms: int     # written clip length
    rate: int            # written sample rate
    sha256: str          # of the written WAV file
    path: Path | None = None


def _mono_float(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if np.issubdtype(x.dtype, np.integer):
        return x.astype(np.float64) / _FULL_SCALE
    return x.astype(np.float64)


def speech_bounds(x: np.ndarray, rate: int, params: ImportParams = ImportParams()) -> tuple[int, int] | None:
    """(first, last+1) sample index whose centred short-window RMS exceeds the trim level, or None."""
    if len(x) == 0:
        return None
    win = max(1, int(round(params.rms_win_ms * rate / _MS_PER_S)))
    sq = np.concatenate([[0.0], np.cumsum(x * x)])
    half = win // 2
    idx = np.arange(len(x))
    lo = np.clip(idx - half, 0, len(x))
    hi = np.clip(idx - half + win, 0, len(x))
    rms = np.sqrt((sq[hi] - sq[lo]) / np.maximum(hi - lo, 1))
    thr = 10 ** (params.trim_dbfs / 20)
    active = np.flatnonzero(rms > thr)
    if len(active) == 0:
        return None
    return int(active[0]), int(active[-1]) + 1


def import_clip(path: Path | str, out_dir: Path | str,
                params: ImportParams = ImportParams()) -> ClipMeta:
    """Trim `path`, resample to `params.out_rate` mono int16, write `<out_dir>/<stem>.wav`."""
    path, out_dir = Path(path), Path(out_dir)
    data, rate = sf.read(str(path), always_2d=False)
    x = _mono_float(data)
    if rate != params.out_rate:
        x = soxr.resample(x, rate, params.out_rate, quality="HQ")
        rate = params.out_rate
    b = speech_bounds(x, rate, params)
    if b is None:
        raise ValueError(f"{path}: no audio above {params.trim_dbfs} dBFS")
    s0, s1 = b
    pad = int(round(params.pad_ms * rate / _MS_PER_S))
    a, z = max(0, s0 - pad), min(len(x), s1 + pad)
    y = x[a:z]
    pcm = np.clip(np.rint(y * _FULL_SCALE), -32768, 32767).astype(np.int16)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}.wav"
    sf.write(str(out), pcm, rate, subtype="PCM_16")
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    ms = lambda n: int(round(n * _MS_PER_S / rate))
    return ClipMeta(onset_ms=ms(s0 - a), end_ms=ms(s1 - a), duration_ms=ms(len(pcm)),
                    rate=rate, sha256=sha, path=out)
