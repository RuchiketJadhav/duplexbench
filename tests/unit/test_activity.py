"""M1-T08: RMS activity and utterance segmentation (spec §1.2)."""
from __future__ import annotations

import subprocess
import sys

import numpy as np

from duplexbench.metrics.activity import (Segmenter, events_to_segments, frame_rms_dbfs, is_active,
                                          run_frames, segment)
from duplexbench.thresholds import Thresholds

TH = Thresholds()


def _tone(dbfs: float, n: int = 160, rate: int = 8000) -> np.ndarray:
    amp = 32768 * 10 ** (dbfs / 20) * np.sqrt(2)
    return np.rint(amp * np.sin(2 * np.pi * 440 * np.arange(n) / rate)).astype(np.int16)


def _live(pattern) -> list[tuple[int, int]]:
    seg = Segmenter(TH)
    evs = []
    for i, a in enumerate(pattern):
        evs += seg.push(i, bool(a))
    evs += seg.finish(len(pattern))
    return events_to_segments(evs)


def test_silence_is_minus_inf_and_inactive():
    r = frame_rms_dbfs(np.zeros(160, np.int16))
    assert r == float("-inf")
    assert not is_active(r, TH)


def test_tone_levels():
    assert abs(frame_rms_dbfs(_tone(-40)) - (-40)) < 0.1
    assert is_active(frame_rms_dbfs(_tone(-40)), TH)
    assert not is_active(frame_rms_dbfs(_tone(-50)), TH)


def test_run_length_from_thresholds():
    assert run_frames(TH) == 25
    assert run_frames(Thresholds(inactivity_run_ms=300)) == 15


def test_gap_480ms_is_one_utterance():
    p = [1] * 50 + [0] * 24 + [1] * 10
    assert segment(np.array(p, bool), TH) == [(0, 84)]
    assert _live(p) == [(0, 84)]


def test_gap_500ms_is_two_utterances_stop_at_run_start():
    p = [1] * 50 + [0] * 25 + [1] * 10
    assert segment(np.array(p, bool), TH) == [(0, 50), (75, 85)]
    seg = Segmenter(TH)
    evs = [e for i, a in enumerate(p) for e in seg.push(i, bool(a))]
    assert [(e.kind, e.frame_idx) for e in evs] == [("onset", 0), ("stop", 50), ("onset", 75)]


def test_stop_reported_when_the_run_completes():
    seg = Segmenter(TH)
    out = {}
    for i, a in enumerate([1] * 10 + [0] * 30):
        evs = seg.push(i, bool(a))
        if evs:
            out[i] = evs
    assert out[34][0].kind == "stop" and out[34][0].frame_idx == 10   # 10 + 25 - 1


def test_trailing_inactive_shorter_than_run_closes_at_its_start():
    assert segment(np.array([0, 1, 1, 0, 0], bool), TH) == [(1, 3)]
    assert segment(np.array([1, 1], bool), TH) == [(0, 2)]
    assert segment(np.zeros(10, bool), TH) == []


def test_live_equals_batch_on_random_patterns():
    rng = np.random.default_rng(7)
    for _ in range(1_000):
        n = int(rng.integers(1, 300))
        # bursty patterns: runs of random lengths so both short and long gaps occur
        p, v = [], bool(rng.integers(0, 2))
        while len(p) < n:
            p += [v] * int(rng.integers(1, 40))
            v = not v
        p = np.array(p[:n], bool)
        assert _live(p) == segment(p, TH)


def test_segmenter_does_not_import_onnxruntime():
    code = ("import sys; import duplexbench.metrics.activity; "
            "assert 'onnxruntime' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)
