"""Reference playout model: jitter buffer, flush on cancel, ear recording (spec §1.3; BUILD_PLAN M1-T09).

    enqueue(frame, t_recv) --> deque --> run(): one frame every frame_ms on the clock
                                           |-> right channel of the WAV (the ear)
                                           |-> `playout` row: rms, active, depth, backlog
                                           |-> Segmenter -> onset / stop -> subscribers

Rules
-----
* The loop ticks from the moment `run()` starts, on an absolute grid
  t_k = t_start + k * frame_ms, and writes exactly one row per tick, so the ear
  recording is continuous (silence when nothing plays).
* Jitter buffer: while *buffering* (at start and after every underrun) a frame is
  played only once `jitter_buffer_ms / frame_ms` frames are queued, or once the
  oldest queued frame has waited `jitter_buffer_ms` (so a short tail is never
  stranded). Spec §1.3: 60 ms = three frames.
* Underrun (queue empty while playing): a silence frame is played, one
  `playout_underrun` event is logged per episode, and buffering resumes.
* `queue_depth` is the number of queued frames at the tick, *including* the frame
  being played; `backlog_ms = queue_depth * frame_ms` = how long audio enqueued
  now waits before it is heard. A frame-synchronous system holds the jitter buffer
  (3 frames = 60 ms); a faster-than-real-time one builds a backlog.
* `flush(t, reason)` empties the queue immediately (provider cancel/truncate, spec
  §1.3; the harness invents no flush). Idempotent: a second flush at the same
  `t_ns` logs nothing. The next played row carries `flushed=1`.
* Same-time ordering (BUILD_PLAN §2.1): a flush whose clock wait was registered
  before the tick's wait runs first, so the frame is not played.

Subscribers: `subscribe(cb)` with `cb(kind, frame_idx, t_ns)`, kind "onset" or
"stop"; `t_ns` is the tick time of `frame_idx` (for a stop, the start of the
inactivity run, reported when the run completes).

Frame subscribers: `subscribe_frames(cb)` with `cb(frame_idx, t_ns, pcm_int16)`,
called once per tick for every played frame, silence included, after the frame's
row is logged and before onset/stop subscribers (spec §7 "Hears": the simulated
caller streams the ear into the reference ASR). `pcm_int16` is the exact array
written to the ear channel; callbacks must not modify it.
"""
from __future__ import annotations

import bisect
import itertools
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from duplexbench.metrics.activity import Segmenter, frame_rms_dbfs, is_active
from duplexbench.storage import canonical_json

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.recorder import Recorder
    from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000
_FRAMES_PER_S = 50

Subscriber = Callable[[str, int, int], None]
FrameSubscriber = Callable[[int, int, np.ndarray], None]


class Playout:
    def __init__(self, clock: "Clock", store: Any, wav: "Recorder | None", th: "Thresholds",
                 segmenter: Segmenter | None = None, *, run_id: str = "", call_id: str = "",
                 rate: int = 8000, next_seq: Callable[[], int] | None = None):
        self.clock, self.store, self.wav, self.th = clock, store, wav, th
        self.segmenter = segmenter if segmenter is not None else Segmenter(th)
        self.run_id, self.call_id, self.rate = run_id, call_id, int(rate)
        self.frame_len = self.rate // _FRAMES_PER_S
        self.frame_ns = th.frame_ms * _NS_PER_MS
        self.prebuffer = max(1, th.jitter_buffer_ms // th.frame_ms)
        self.prebuffer_ns = th.jitter_buffer_ms * _NS_PER_MS
        self.next_seq: Callable[[], int] = next_seq or itertools.count().__next__
        self._q: deque[tuple[np.ndarray, int]] = deque()
        self._buffering = True
        self._underrun_logged = True       # no underrun before anything played
        self._flushed_pending = False
        self._last_flush_t: int | None = None
        self._subs: list[Subscriber] = []
        self._frame_subs: list[FrameSubscriber] = []
        self._stop = False
        self.t_start: int | None = None
        self.frames_played = 0             # ticks so far
        self.last_active_ns: int | None = None
        self.enqueued = 0
        self._row_t: list[int] = []        # logged rows, for backlog_ms_at
        self._row_backlog: list[float] = []
        self._silence = np.zeros(self.frame_len, np.int16)

    # -- producer side --------------------------------------------------------
    def enqueue(self, frame_pcm: np.ndarray, t_recv_ns: int) -> None:
        """Queue one frame (`rate`/50 samples, int16) received at `t_recv_ns`."""
        self._q.append((np.asarray(frame_pcm, dtype=np.int16).ravel(), int(t_recv_ns)))
        self.enqueued += 1

    def flush(self, t_ns: int, reason: str) -> None:
        """Empty the queue now (spec §1.3). Idempotent at one `t_ns`."""
        if self._last_flush_t == t_ns:
            return
        self._last_flush_t = t_ns
        dropped = len(self._q)
        self._q.clear()
        self._flushed_pending = True
        self._event(t_ns, "playout_flush", {"reason": reason, "dropped_frames": dropped})

    def subscribe(self, cb: Subscriber) -> None:
        """`cb(kind, frame_idx, t_ns)` on every utterance onset / stop at the ear."""
        self._subs.append(cb)

    def subscribe_frames(self, cb: FrameSubscriber) -> None:
        """`cb(frame_idx, t_ns, pcm_int16)` for every played frame (silence included)."""
        self._frame_subs.append(cb)

    def stop(self) -> None:
        """Ask `run()` to return before its next tick."""
        self._stop = True

    @property
    def depth(self) -> int:
        return len(self._q)

    # -- the loop ---------------------------------------------------------------
    def tick_ns(self, frame_idx: int) -> int:
        return (self.t_start or 0) + frame_idx * self.frame_ns

    async def run(self) -> None:
        """Accounted task: play one frame per `frame_ms` until `stop()` or cancel."""
        self.t_start = self.clock.now_ns()
        k = 0
        while not self._stop:
            t = self.t_start + k * self.frame_ns
            await self.clock.sleep_until_ns(t)
            if self._stop:
                break
            self._tick(k, t)
            k += 1

    def _tick(self, k: int, t: int) -> None:
        depth = len(self._q)
        frame = None
        if self._buffering and depth:
            oldest = self._q[0][1]
            if depth >= self.prebuffer or t - oldest >= self.prebuffer_ns:
                self._buffering = False
        if not self._buffering:
            if depth:
                frame = self._q.popleft()[0]
                self._underrun_logged = False
            else:
                self._buffering = True
                if not self._underrun_logged:
                    self._underrun_logged = True
                    self._event(t, "playout_underrun", {"frame_idx": k})
        pcm = frame if frame is not None else self._silence
        if len(pcm) != self.frame_len:
            pcm = np.pad(pcm, (0, max(0, self.frame_len - len(pcm))))[:self.frame_len]
        rms = frame_rms_dbfs(pcm)
        active = is_active(rms, self.th)
        backlog = float(depth * self.th.frame_ms)
        flushed = self._flushed_pending
        self._flushed_pending = False
        if self.wav is not None:
            self.wav.write_right(pcm, t)
        self.store.put("playout", {
            "run_id": self.run_id, "call_id": self.call_id, "t_ns": t, "frame_idx": k,
            "rms_dbfs": rms if np.isfinite(rms) else None, "active": int(active),
            "queue_depth": depth, "backlog_ms": backlog, "flushed": int(flushed)})
        self._row_t.append(t)
        self._row_backlog.append(backlog)
        self.frames_played = k + 1
        if active:
            self.last_active_ns = t
        for fcb in list(self._frame_subs):
            fcb(k, t, pcm)
        for ev in self.segmenter.push(k, active):
            self._publish(ev.kind, ev.frame_idx)

    def _publish(self, kind: str, idx: int) -> None:
        t = self.tick_ns(idx)
        self._event(t, "agent_onset" if kind == "onset" else "agent_stop", {"frame_idx": idx})
        for cb in list(self._subs):
            cb(kind, idx, t)

    def _event(self, t_ns: int, kind: str, payload: dict) -> None:
        self.store.put("events", {
            "run_id": self.run_id, "call_id": self.call_id, "t_ns": int(t_ns),
            "seq": self.next_seq(), "kind": kind, "src": "playout",
            "payload_json": canonical_json(payload)})

    # -- queries ------------------------------------------------------------------
    def backlog_ms_at(self, t_ns: int) -> float:
        """backlog_ms of the last row logged at or before `t_ns` (binary search; 0 before the first)."""
        i = bisect.bisect_right(self._row_t, t_ns) - 1
        return self._row_backlog[i] if i >= 0 else 0.0
