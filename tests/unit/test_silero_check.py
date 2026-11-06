"""M1-T13: offline Silero cross-check of RMS activity (spec §1.2, §1.4)."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from duplexbench.metrics.activity import frame_rms_dbfs, is_active, segment
from duplexbench.thresholds import Thresholds

FIX = Path(__file__).resolve().parents[1] / "fixtures"
TH = Thresholds()

silero = pytest.importorskip("duplexbench.metrics.silero_check")


def _model_or_skip() -> Path:
    try:
        return silero.model_path()
    except FileNotFoundError as e:
        pytest.skip(str(e))


def _rms_segments(x: np.ndarray, rate: int) -> list[tuple[float, float]]:
    n = rate // 50
    act = np.array([is_active(frame_rms_dbfs(x[i:i + n]), TH) for i in range(0, len(x) - n + 1, n)])
    return [(a * TH.frame_ms * 1.0, b * TH.frame_ms * 1.0) for a, b in segment(act, TH)]


def test_pinned_model_hash():
    p = _model_or_skip()
    assert hashlib.sha256(p.read_bytes()).hexdigest() == silero.MODEL_SHA256


def test_fixture_is_reproducible():
    sys.path.insert(0, str(FIX))
    try:
        import make_tone_segments as m
    finally:
        sys.path.pop(0)
    x, rate = sf.read(str(FIX / "tone_segments.wav"), dtype="int16")
    assert rate == m.RATE and np.array_equal(x, m.build())


def test_tone_segments_disagreement_below_threshold():
    _model_or_skip()
    x, rate = sf.read(str(FIX / "tone_segments.wav"), dtype="int16")
    rms = _rms_segments(x, rate)
    sil = silero.boundaries(x, rate)
    assert len(rms) == 3 and len(sil) == 3
    assert silero.disagreement_ms(rms, sil) < TH.silero_disagree_ms


def test_pure_tone_is_not_speech_to_silero():
    """Why the mock's calibration voice is babble, not the 440 Hz tone."""
    _model_or_skip()
    rate = 8000
    t = np.arange(rate) / rate
    x = np.concatenate([np.zeros(rate, np.int16),
                        np.rint(3277 * np.sqrt(2) * np.sin(2 * np.pi * 440 * t)).astype(np.int16),
                        np.zeros(rate, np.int16)])
    assert silero.boundaries(x, rate) == []


def test_disagreement_rules():
    assert silero.disagreement_ms([(0, 100)], [(10, 130)]) == 30
    assert silero.disagreement_ms([], []) == 0.0
    assert silero.disagreement_ms([(0, 100)], []) == float("inf")             # missed utterance
    assert silero.disagreement_ms([(0, 100)], [(0, 100), (500, 600)]) == float("inf")


def test_never_imported_by_the_live_loop():
    code = ("import sys; import duplexbench.playout, duplexbench.runner; "
            "bad = [m for m in ('duplexbench.metrics.silero_check', 'onnxruntime') if m in sys.modules]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)
