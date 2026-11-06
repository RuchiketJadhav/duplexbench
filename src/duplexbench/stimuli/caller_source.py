"""Caller sources: the protocol the runner pulls caller audio from (BUILD_PLAN §2.2, §2.7).

    class CallerSource(Protocol):
        def frames(self) -> AsyncIterator[Frame]            # paced 20 ms frames
        def inject(self, clip_pcm, level_db, t_ns) -> str    # overlay mixing (M2 engine)
        def turn_ends(self) -> AsyncIterator[tuple[str, int]]  # (step_id, t_ns)

Contract
--------
* `frames()` paces itself on the clock: it sleeps until each frame's `t_ns`
  (through `clock.sleep_until_ns`) before yielding it, so the consumer's task is
  the one that waits and an `inject` made before `t_ns` is mixed into that frame.
  Frames are at the line rate of the condition (`rate`/50 samples, int16). The
  iterator ends when the source has nothing more to say (end of script); the
  runner then ends the call as `completed`.
* `Frame.speech` marks frames carrying caller speech (scripted turn or injected
  overlay); `step_id` names the caller step being spoken; `overlay_id` names the
  injected clip that is sounding in the frame.
* `turn_ends()` yields `(step_id, t_ns)` for each caller turn, where `t_ns` is the
  source time of the turn's last speech frame (spec §1.2 "caller turn end"), and
  ends after the last turn. Waits inside it go through `clock.idle`.

`ToneCaller` is the M1 test implementation: scripted turns of a pure tone. The
stimuli milestone adds `ScriptedCaller` beside it.
"""
from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import numpy as np

from duplexbench.frames import Frame as _BaseFrame

if TYPE_CHECKING:
    from duplexbench.clock import Clock

_NS_PER_MS = 1_000_000
_NS_PER_S = 1_000_000_000
_FRAMES_PER_S = 50
_FULL_SCALE = 32768.0


@dataclass
class Frame(_BaseFrame):
    """One 20 ms frame (BUILD_PLAN §2.2) plus the caller-side ground truth the
    runner logs (spec §1.2: caller anchors come from what the harness synthesized).
    A subclass of `duplexbench.frames.Frame`, so every caller source yields both."""
    t_ns: int
    seq: int
    src: Literal["caller", "agent"]
    pcm: np.ndarray | None
    mulaw: bytes | None = None
    speech: bool = False
    step_id: str | None = None
    overlay_id: str | None = None


@runtime_checkable
class CallerSource(Protocol):
    def frames(self) -> AsyncIterator[Frame]: ...
    def inject(self, clip_pcm: np.ndarray, level_db: float, t_ns: int | None = None) -> str: ...
    def turn_ends(self) -> AsyncIterator[tuple[str, int]]: ...


def tone(rate: int, n: int, hz: float, level_dbfs: float, phase0: int = 0) -> np.ndarray:
    """Sine of RMS `level_dbfs`, samples phase0 .. phase0+n-1 of a continuous tone."""
    amp = _FULL_SCALE * 10 ** (level_dbfs / 20) * np.sqrt(2)
    t = (np.arange(n) + phase0) / rate
    return np.clip(np.rint(amp * np.sin(2 * np.pi * hz * t)), -32768, 32767).astype(np.int16)


@dataclass(frozen=True)
class ToneTurn:
    step_id: str
    start_ms: int
    dur_ms: int


class ToneCaller:
    """Scripted caller speaking pure-tone turns (M1 tests, calibration, golden).

    `turns`: (step_id, start_ms, dur_ms) relative to the first frame.
    `total_ms`: length of the call script; `None` = silence frames forever after
    the last turn (the runner's end rules then decide).
    """

    def __init__(self, clock: "Clock", turns: Sequence[tuple[str, int, int]], *,
                 rate: int = 8000, total_ms: int | None = None, hz: float = 660.0,
                 level_dbfs: float = -20.0):
        self.clock = clock
        self.rate = int(rate)
        self.frame_len = self.rate // _FRAMES_PER_S
        self.frame_ms = 1_000 // _FRAMES_PER_S   # frames are 20 ms by contract (§2.2)
        self.turns = [ToneTurn(*t) for t in turns]
        self.total_ms = total_ms
        self.hz, self.level_dbfs = hz, level_dbfs
        self._ids = itertools.count()
        self._inj: list[tuple[str, int, np.ndarray]] = []   # (id, start sample, pcm)
        self._turn_q: asyncio.Queue = asyncio.Queue()
        self._t0: int | None = None
        self.exhausted = False

    # -- injection ----------------------------------------------------------------
    def inject(self, clip_pcm: np.ndarray, level_db: float, t_ns: int) -> str:
        """Mix `clip_pcm` (line rate) scaled by `level_db` from clock time `t_ns`."""
        oid = f"inj-{next(self._ids)}"
        gain = 10 ** (level_db / 20)
        x = np.clip(np.rint(np.asarray(clip_pcm, np.float64) * gain), -32768, 32767).astype(np.int16)
        self._inj.append((oid, int(t_ns), x))
        return oid

    # -- frames ---------------------------------------------------------------------
    def _sample(self, ms: int) -> int:
        return ms * self.rate // 1_000

    async def frames(self) -> AsyncIterator[Frame]:
        self._t0 = self.clock.now_ns()
        frame_ns = self.frame_ms * _NS_PER_MS
        spans = [(tt, self._sample(tt.start_ms), self._sample(tt.start_ms + tt.dur_ms))
                 for tt in self.turns]
        n_frames = None if self.total_ms is None else self._sample(self.total_ms) // self.frame_len
        last_turn_end = max((b for _, _, b in spans), default=0)
        k = 0
        try:
            while n_frames is None or k < n_frames:
                t = self._t0 + k * frame_ns
                await self.clock.sleep_until_ns(t)
                a, b = k * self.frame_len, (k + 1) * self.frame_len
                pcm = np.zeros(self.frame_len, np.float64)
                speech, step, ov = False, None, None
                for tt, s0, s1 in spans:
                    lo, hi = max(a, s0), min(b, s1)
                    if lo < hi:
                        pcm[lo - a:hi - a] += tone(self.rate, hi - lo, self.hz, self.level_dbfs, lo)
                        speech, step = True, tt.step_id
                        if s1 <= b:                          # last speech frame of the turn
                            self._turn_q.put_nowait((tt.step_id, t))
                for oid, t_inj, clip in self._inj:
                    s0 = int(round((t_inj - self._t0) * self.rate / _NS_PER_S))
                    lo, hi = max(a, s0), min(b, s0 + len(clip))
                    if lo < hi:
                        seg = clip[lo - s0:hi - s0]
                        pcm[lo - a:hi - a] += seg
                        if np.any(seg):
                            speech, ov = True, oid
                if b >= last_turn_end and spans and not self._turn_done:
                    self._turn_done = True
                    self._turn_q.put_nowait(None)
                out = np.clip(np.rint(pcm), -32768, 32767).astype(np.int16)
                yield Frame(t, k, "caller", out, None, speech, step, ov)
                k += 1
        finally:
            self.exhausted = True
            if not self._turn_done:
                self._turn_done = True
                self._turn_q.put_nowait(None)

    _turn_done = False

    async def turn_ends(self) -> AsyncIterator[tuple[str, int]]:
        while True:
            item = await self.clock.idle(self._turn_q.get())
            if item is None:
                return
            yield item
