"""Frame activity and agent-utterance segmentation (spec §1.2; BUILD_PLAN M1-T08).

A played 20 ms frame is *active* if its RMS exceeds `th.rms_active_dbfs`. An
*inactivity run* is at least `th.inactivity_run_ms` of inactive frames. An agent
utterance is a maximal sequence of active frames not interrupted by an
inactivity run. The *stop* of an utterance is the first frame of the inactivity
run that ends it (spec §1.5 anchor "stop").

One implementation serves the live loop and post-run scoring: the live
`Segmenter` and the batch `segment` both drive `_scan`, so they cannot disagree.
No VAD and no `onnxruntime` here; Silero is an offline cross-check
(`metrics/silero_check.py`).

Live latency: a stop is only known once its run has lasted `inactivity_run_ms`,
so `Segmenter.push` reports it that many frames late, carrying the run's *start*
index.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple

import numpy as np

if TYPE_CHECKING:
    from duplexbench.thresholds import Thresholds

_FULL_SCALE = 32768.0


class ActivityEvent(NamedTuple):
    kind: Literal["onset", "stop"]
    frame_idx: int


def frame_rms_dbfs(pcm: np.ndarray) -> float:
    """RMS of an int16 frame in dBFS (full scale 32768); silence -> -inf."""
    x = np.asarray(pcm, dtype=np.float64).ravel()
    if len(x) == 0:
        return float("-inf")
    ms = float(np.mean(x * x))
    if ms <= 0.0:
        return float("-inf")
    return 10.0 * math.log10(ms) - 20.0 * math.log10(_FULL_SCALE)


def is_active(rms: float, th: "Thresholds") -> bool:
    """Spec §1.2: active iff RMS exceeds `rms_active_dbfs`."""
    return rms > th.rms_active_dbfs


def run_frames(th: "Thresholds") -> int:
    """Inactivity run length in frames: ceil(inactivity_run_ms / frame_ms)."""
    return max(1, math.ceil(th.inactivity_run_ms / th.frame_ms))


@dataclass
class _State:
    run: int                 # inactive frames needed to end an utterance
    in_utt: bool = False
    inactive: int = 0        # consecutive inactive frames inside an utterance
    run_start: int = -1      # first frame of the current inactive streak


def _scan(st: _State, idx: int, active: bool) -> list[ActivityEvent]:
    """Advance the segmentation state by one frame; return events it produced."""
    if active:
        st.inactive = 0
        if not st.in_utt:
            st.in_utt = True
            return [ActivityEvent("onset", idx)]
        return []
    if not st.in_utt:
        return []
    if st.inactive == 0:
        st.run_start = idx
    st.inactive += 1
    if st.inactive >= st.run:
        st.in_utt = False
        st.inactive = 0
        return [ActivityEvent("stop", st.run_start)]
    return []


def _finish(st: _State, n_frames: int) -> list[ActivityEvent]:
    """Close an utterance still open at the end of the stream: it ends at the
    start of its trailing inactive streak, or at `n_frames` if active to the end."""
    if not st.in_utt:
        return []
    end = st.run_start if st.inactive > 0 else n_frames
    st.in_utt = False
    st.inactive = 0
    return [ActivityEvent("stop", end)]


class Segmenter:
    """Live utterance segmentation over played frames (spec §1.2)."""

    def __init__(self, th: "Thresholds"):
        self.th = th
        self._st = _State(run=run_frames(th))

    def push(self, frame_idx: int, active: bool) -> list[ActivityEvent]:
        return _scan(self._st, frame_idx, bool(active))

    def finish(self, n_frames: int) -> list[ActivityEvent]:
        """End of stream: close an open utterance (same rule as `segment`)."""
        return _finish(self._st, n_frames)

    @property
    def in_utterance(self) -> bool:
        return self._st.in_utt


def segment(active: np.ndarray, th: "Thresholds") -> list[tuple[int, int]]:
    """Post-run segmentation: list of (onset_idx, end_idx) with end exclusive
    (end = first frame of the ending inactivity run)."""
    st = _State(run=run_frames(th))
    evs: list[ActivityEvent] = []
    a = np.asarray(active, dtype=bool).ravel()
    for i, v in enumerate(a):
        evs += _scan(st, i, bool(v))
    evs += _finish(st, len(a))
    return events_to_segments(evs)


def events_to_segments(evs: list[ActivityEvent]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    onset: int | None = None
    for e in evs:
        if e.kind == "onset":
            onset = e.frame_idx
        elif onset is not None:
            out.append((onset, e.frame_idx))
            onset = None
    return out
