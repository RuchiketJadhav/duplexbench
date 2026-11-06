"""Offline Silero VAD cross-check of RMS activity (spec §1.2, §1.4; BUILD_PLAN M1-T13).

Never imported by the live loop (`playout.py`, `runner.py`; a test asserts it):
agent activity at the ear is RMS-based; Silero is a cross-check only.

Model: the pinned `silero_vad.onnx` (MIT, snakers4/silero-vad, see docs/licenses.md
"Silero VAD model file"). It is looked up in `reference/silero-vad/...` (the M0
clone) and then `tests/fixtures/silero_vad.onnx`; its sha256 must equal
`MODEL_SHA256`.

Boundaries. The model is a causal RNN over 32 ms windows (256 samples at 8 kHz,
with a 32-sample context): its onsets are prompt but its probability decays
~100 ms after speech ends. So the check runs it twice: forward for onsets and on
the time-reversed signal for ends (a reversed end is an onset). Regions where
either pass gives p >= `THRESHOLD` (gaps < `MIN_GAP_MS` merged) are speech when
both passes fire inside them. Each region is then re-run from a fresh RNN state
(forward from `PAD_MS` before it, backward from `PAD_MS` after it); its onset is
the first forward window >= `THRESHOLD`, its end the last backward one.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
MODEL_CANDIDATES = (ROOT / "reference" / "silero-vad" / "src" / "silero_vad" / "data" / "silero_vad.onnx",
                    ROOT / "tests" / "fixtures" / "silero_vad.onnx")
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
MODEL_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
THRESHOLD = 0.5               # Silero's documented default speech threshold
MIN_GAP_MS = 100.0            # merge speech windows separated by less (Silero default min_silence_duration_ms)
PAD_MS = 256.0                # lead-in / lead-out of the per-region re-run (8 windows at 8 kHz)
PHASES = 8                    # window phases the boundaries are taken over (median)
_WINDOW = {8000: 256, 16000: 512}
_CONTEXT = {8000: 32, 16000: 64}
_MS_PER_S = 1e3

_session = None


def model_path() -> Path:
    for p in MODEL_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError(f"silero_vad.onnx not found; download {MODEL_URL} to {MODEL_CANDIDATES[-1]}")


def _sess():
    global _session
    if _session is None:
        import onnxruntime as ort
        p = model_path()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest != MODEL_SHA256:
            raise RuntimeError(f"{p}: sha256 {digest} != pinned {MODEL_SHA256}")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _session = ort.InferenceSession(str(p), sess_options=opts, providers=["CPUExecutionProvider"])
    return _session


def probabilities(pcm: np.ndarray, rate: int) -> np.ndarray:
    """Speech probability per window (`_WINDOW[rate]` samples, no overlap)."""
    if rate not in _WINDOW:
        raise ValueError(f"Silero supports 8000 or 16000 Hz, got {rate}")
    x = np.asarray(pcm, dtype=np.float32).ravel()
    if np.issubdtype(np.asarray(pcm).dtype, np.integer):
        x = x / 32768.0
    w, c = _WINDOW[rate], _CONTEXT[rate]
    sess = _sess()
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros((1, c), np.float32)
    sr = np.array(rate, dtype=np.int64)
    out = []
    for i in range(0, len(x) - w + 1, w):
        inp = np.concatenate([ctx, x[i:i + w][None]], axis=1)
        p, state = sess.run(None, {"input": inp, "state": state, "sr": sr})
        ctx = inp[:, -c:]
        out.append(float(np.asarray(p).ravel()[0]))
    return np.asarray(out)


def boundaries(wav_right: np.ndarray, rate: int) -> list[tuple[float, float]]:
    """Speech segments (onset_ms, end_ms) found by Silero on one channel: the per-segment
    median over `PHASES` window phases (see module docstring)."""
    x = np.asarray(wav_right).ravel()
    w = _WINDOW[rate]
    runs = []
    for k in range(PHASES):
        sh = k * w // PHASES
        lead_ms = sh / rate * _MS_PER_S
        segs = _boundaries_one_phase(np.concatenate([np.zeros(sh, x.dtype), x]), rate)
        runs.append([(a - lead_ms, b - lead_ms) for a, b in segs])
    counts = [len(r) for r in runs]
    n = max(set(counts), key=lambda c: (counts.count(c), -counts.index(c)))
    if n == 0:
        return []
    agree = np.array([r for r in runs if len(r) == n], dtype=float).reshape(-1, n, 2)
    return [(float(a), float(b)) for a, b in np.median(agree, axis=0)]


def _boundaries_one_phase(x: np.ndarray, rate: int) -> list[tuple[float, float]]:
    w = _WINDOW[rate]
    n = len(x) // w
    if n == 0:
        return []
    x = x[:n * w]
    fwd = probabilities(x, rate) >= THRESHOLD
    bwd = probabilities(x[::-1].copy(), rate)[::-1] >= THRESHOLD
    win_ms = w / rate * _MS_PER_S
    gap = int(np.ceil(MIN_GAP_MS / win_ms))
    pad = int(np.ceil(PAD_MS / win_ms))
    # candidate regions: union of both passes, gaps shorter than MIN_GAP_MS merged
    regions: list[list[int]] = []
    for i in np.flatnonzero(fwd | bwd):
        if regions and i - regions[-1][1] <= gap:
            regions[-1][1] = i
        else:
            regions.append([i, i])
    out = []
    for a, b in regions:
        # fresh RNN state per region: forward from `pad` windows before it, backward
        # from `pad` windows after it (state carried over from an earlier utterance
        # delays the next onset by up to ~200 ms)
        a0, b1 = max(0, a - pad), min(n, b + 1 + pad)
        f = np.flatnonzero(probabilities(x[a0 * w:(b + 1) * w], rate) >= THRESHOLD)
        seg = x[a * w:b1 * w][::-1].copy()
        r = np.flatnonzero(probabilities(seg, rate)[::-1] >= THRESHOLD)
        if len(f) == 0 or len(r) == 0:
            continue                    # seen by one pass only: not speech
        on, end = a0 + f[0], a + r[-1] + 1
        if end > on:
            out.append((on * win_ms, end * win_ms))
    return out


def disagreement_ms(rms_segments: list[tuple[float, float]],
                    silero_segments: list[tuple[float, float]]) -> float:
    """Max absolute boundary difference (onset and end) over matched segments.

    Each RMS segment is matched to the Silero segment it overlaps most; an RMS
    segment with no overlapping Silero segment (or vice versa) counts as infinite
    disagreement, so a missed utterance fails the §1.4 check."""
    if not rms_segments and not silero_segments:
        return 0.0
    worst = 0.0
    used: set[int] = set()
    for a, b in rms_segments:
        best, best_ov = None, 0.0
        for j, (c, d) in enumerate(silero_segments):
            ov = min(b, d) - max(a, c)
            if ov > best_ov:
                best, best_ov = j, ov
        if best is None:
            return float("inf")
        used.add(best)
        c, d = silero_segments[best]
        worst = max(worst, abs(a - c), abs(b - d))
    if len(used) < len(silero_segments):
        return float("inf")
    return worst
