"""Scripted TTS caller source (BUILD_PLAN M2-T06; spec §3.2 geometry, §3.5 scripted callers).

    clips = {"s1": pcm_8k, "s1/resumption": pcm_8k, "s2": pcm_8k}   # int16, 8 kHz, prepared by the caller
    caller = ScriptedCaller(scenario, clips, clock, th)
    async for frame in caller.frames(): ...          # paced 20 ms frames, from clock.now_ns() at first pull
    async for step_id, t_ns in caller.turn_ends(): ...
    caller.inject(overlay_pcm, level_db=-6.0, t_ns=clock.now_ns())

Script geometry, per step in order:

    [speech clip] [pause_ms silence] [resumption clip]   <- pause/resumption only when pause_ms is set
    [post-turn silence]                                   <- E-END resumption_ms on this step, else th.eend_silence_ms

The post-turn silence follows EVERY step (the scripted caller waits for the agent's
reply there; spec §3.2 E-END "a complete caller turn followed by >= 6 s of scripted
silence"). Each segment is zero-padded to whole frames so step boundaries sit on
frame boundaries. Wait-class Y-CMD resumptions (3-6 s after an injected command)
depend on the live injection time and belong to the overlay engine (M2-T05).

`turn_ends` yields `(step_id, t_ns)` where t_ns is the end of the step's speech on
the send timeline: segment start + `speech_end_ms` when the step gives one (no
resumption), else the end of the last speech clip. It is queued when the frame that
carries that speech end is handed out (that frame's send time).

`inject` mixes an overlay into the outgoing stream starting at the next frame not
yet handed out (or the frame at `t_ns`, whichever is later). The clip is scaled so
its own RMS is -20 dBFS, then by `level_db` (spec §3.2 "at the stated level
offsets"); overlapping voices are summed and clipped to int16. It returns the
overlay id (the `CallerSource` contract; `overlay_id=` names it, else `inj-<n>`);
`injected_at[id]` is the source time of the first frame that carries it.

Frames are `stimuli.caller_source.Frame` (a `frames.Frame`) with the caller-side
ground truth the runner logs: `speech` (a scripted speech/resumption frame with
non-zero samples, or an injected clip sounding), `step_id` (the scripted step
speaking) and `overlay_id` (the injected clip sounding).

Lead-in (fix-34, WORKFLOW_SPEC gap G1): `ScriptedCaller(..., lead_in=LeadIn(th))`
holds step 1 until the agent's first end of turn (run config
`scripted: {lead_in: agent_first}`; the default `caller_first` passes no LeadIn and
plays step 1 from the first frame, as before). While it waits, the caller sends
silence frames on the same 20 ms grid (`speech=False`, no step); `start_ns`, the
origin of every script time (turn ends, injections, P-PAUSE geometry), is set to
the tick at which step 1's first frame goes out, so the script itself is unchanged.
See `LeadIn` for the end-of-turn rule and the no-greeting guard.

Implements the `CallerSource` protocol of `stimuli/caller_source.py` (M1) by
structure; `CallerSourceLike` below mirrors it so this module does not depend on
that file's creation order.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import numpy as np

from duplexbench.channel import mulaw
from duplexbench.clock import Clock
from duplexbench.stimuli.caller_source import Frame
from duplexbench.scenarios.schema import Scenario
from duplexbench.thresholds import Thresholds

_FULL_SCALE = 32768.0
_NS_PER_MS = 1_000_000
_MS_PER_S = 1000              # threshold-ok: unit conversion ms per s
REF_DBFS = -20.0          # overlay clips are normalised to this RMS before level_db is applied
_END = object()


@runtime_checkable
class CallerSourceLike(Protocol):
    """Structural mirror of `stimuli.caller_source.CallerSource` (M1)."""
    def frames(self) -> AsyncIterator[Frame]: ...
    def inject(self, clip_pcm: np.ndarray, level_db: float, t_ns: int | None = None) -> str: ...
    def turn_ends(self) -> AsyncIterator[tuple[str, int]]: ...


@dataclass(frozen=True)
class Segment:
    kind: Literal["speech", "pause", "resumption", "post_turn"]
    step_id: str
    start_frame: int
    n_frames: int


class LeadIn:
    """agent_first gate (fix-34): wait for the agent's first end of turn before step 1.

    Fed by `Playout.subscribe(lead_in.on_activity)` (the live ear segmenter: an
    utterance stop is the start of an `inactivity_run_ms` run). The agent's first end of
    turn is reached when at least one utterance has started and the ear has been
    inactive for `th.eend_silence_ms` since the last utterance's stop (the same
    silence the script leaves after every caller step; no new threshold). An agent
    that resumes inside that silence (a two-part greeting) restarts the wait.
    `greeting_end_ns` = that last stop (ear time).

    Guard: no agent onset within `th.stall_ms` of the lead-in start -> `no_greeting`
    and step 1 starts anyway. The harness has no first-audio timeout; `stall_ms` is
    the existing "neither channel active" call timeout (`call_timeout_ms`, 480 s,
    could never fire first, because `stall_ms`, 20 s, ends a silent call before it).
    The runner does not apply its stall rule while the caller is `waiting_lead_in`.
    Nothing is cut: the greeting plays to the ear like any agent audio."""

    mode = "agent_first"

    def __init__(self, th: Thresholds):
        self.wait_ns = th.eend_silence_ms * _NS_PER_MS
        self.guard_ns = th.stall_ms * _NS_PER_MS
        self.utts: list[list[int | None]] = []        # [onset_ns, stop_ns | None]
        self.start_ns: int | None = None              # first lead-in tick
        self.end_ns: int | None = None                # tick at which step 1 started
        self.greeting_end_ns: int | None = None
        self.no_greeting = False

    def on_activity(self, kind: str, frame_idx: int, t_ns: int) -> None:
        del frame_idx
        if self.end_ns is not None:
            return
        if kind == "onset":
            self.utts.append([t_ns, None])
        elif kind == "stop" and self.utts and self.utts[-1][1] is None:
            self.utts[-1][1] = t_ns

    def ready(self, now: int) -> bool:
        """True once step 1 may start at tick `now` (records the outcome)."""
        if self.start_ns is None:
            self.start_ns = now
        if not self.utts:
            if now - self.start_ns >= self.guard_ns:
                self.no_greeting, self.end_ns = True, now
                return True
            return False
        stop = self.utts[-1][1]
        if stop is not None and now - stop >= self.wait_ns:
            self.greeting_end_ns, self.end_ns = stop, now
            return True
        return False

    def flags(self, call_start_ns: int) -> dict:
        """Call flags (ms from the call start): lead_in, greeting_end_ms, step1_start_ms,
        greeting_utterances, no_greeting."""
        def ms(t: int | None) -> float | None:
            return None if t is None else (t - call_start_ns) / _NS_PER_MS
        return {"lead_in": self.mode, "greeting_end_ms": ms(self.greeting_end_ns),
                "step1_start_ms": ms(self.end_ns), "greeting_utterances": len(self.utts),
                "no_greeting": self.no_greeting}


def resumption_key(step_id: str) -> str:
    """Key of a step's resumption clip in the `clips` mapping."""
    return f"{step_id}/resumption"


def dbfs(pcm: np.ndarray) -> float:
    x = np.asarray(pcm, dtype=np.float64)
    r = math.sqrt(float(np.mean(x * x))) if len(x) else 0.0
    return 20 * math.log10(r / _FULL_SCALE) if r > 0 else -math.inf


class ScriptedCaller:
    def __init__(self, scenario: Scenario, clips: Mapping[str, np.ndarray], clock: Clock,
                 th: Thresholds, rate: int = 8000, lead_in: LeadIn | None = None):
        self.scenario = scenario
        self.lead_in = lead_in
        self.waiting_lead_in = False      # runner: no stall while the lead-in waits
        self.clock = clock
        self.th = th
        self.rate = rate
        self.frame_ns = th.frame_ms * _NS_PER_MS
        self.spf = rate * th.frame_ms // _MS_PER_S          # samples per frame (160 at 8 kHz)
        self.segments: list[Segment] = []
        self._turn_end_at: dict[int, list[tuple[str, int]]] = {}   # frame idx -> [(step, offset_ns)]
        self._script = self._build(clips)
        self.start_ns: int | None = None
        self._next_k = 0
        self._inj: list[tuple[int, np.ndarray, str]] = []     # (start frame, float samples, id)
        self.injected_at: dict[str, int] = {}                  # overlay id -> first frame t_ns
        self._inj_ids = 0
        self._seg_of: list[Segment] = [sg for sg in self.segments for _ in range(sg.n_frames)]
        self._turns: asyncio.Queue = asyncio.Queue()

    # -- script -------------------------------------------------------------
    def _frames_for(self, n_samples: int) -> int:
        return -(-n_samples // self.spf)

    def _build(self, clips: Mapping[str, np.ndarray]) -> np.ndarray:
        s = self.scenario
        eend = {o.anchor_step: o.resumption_ms for o in s.overlays
                if o.cls == "E-END" and o.resumption_ms is not None}
        parts: list[np.ndarray] = []
        k = 0

        def add(kind: str, step_id: str, pcm: np.ndarray) -> int:
            nonlocal k
            n = self._frames_for(len(pcm))
            buf = np.zeros(n * self.spf, np.int16)
            buf[:len(pcm)] = pcm
            parts.append(buf)
            self.segments.append(Segment(kind, step_id, k, n))
            k0, k = k, k + n
            return k0

        for st in s.steps:
            if st.id not in clips:
                raise ValueError(f"{s.id}: no clip for step {st.id!r}")
            speech = _int16(clips[st.id])
            k0 = add("speech", st.id, speech)
            end_ns = k0 * self.frame_ns + len(speech) * _NS_PER_MS * _MS_PER_S // self.rate
            if st.pause_ms is not None:
                rk = resumption_key(st.id)
                if rk not in clips:
                    raise ValueError(f"{s.id}: step {st.id!r} has pause_ms but no clip {rk!r}")
                add("pause", st.id, np.zeros(st.pause_ms * self.rate // _MS_PER_S, np.int16))
                res = _int16(clips[rk])
                r0 = add("resumption", st.id, res)
                end_ns = r0 * self.frame_ns + len(res) * _NS_PER_MS * _MS_PER_S // self.rate
            elif st.speech_end_ms is not None:
                end_ns = k0 * self.frame_ns + st.speech_end_ms * _NS_PER_MS
            last_k = max(k0, -(-end_ns // self.frame_ns) - 1)
            self._turn_end_at.setdefault(last_k, []).append((st.id, end_ns))
            gap_ms = eend.get(st.id, self.th.eend_silence_ms)
            add("post_turn", st.id, np.zeros(gap_ms * self.rate // _MS_PER_S, np.int16))
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    @property
    def n_frames(self) -> int:
        """Frames in the script alone (injections may extend the stream)."""
        return len(self._script) // self.spf

    # -- CallerSource -------------------------------------------------------
    async def frames(self) -> AsyncIterator[Frame]:
        t0 = self.clock.now_ns()
        j = 0                                   # lead-in frames sent (agent_first)
        if self.lead_in is not None:
            self.waiting_lead_in = True
            silence = np.zeros(self.spf, np.int16)
            while True:
                t = t0 + j * self.frame_ns
                await self.clock.sleep_until_ns(t)
                if self.lead_in.ready(self.clock.now_ns()):
                    break
                yield Frame(t_ns=t, seq=j, src="caller", pcm=silence.copy(),
                            mulaw=mulaw.encode(silence), speech=False, step_id=None,
                            overlay_id=None)
                j += 1
            self.waiting_lead_in = False
        self.start_ns = t0 + j * self.frame_ns
        k = 0
        try:
            while k < self.n_frames or any(k0 + len(x) // self.spf > k for k0, x, _ in self._inj):
                t = self.start_ns + k * self.frame_ns
                await self.clock.sleep_until_ns(t)
                pcm, ov = self._mix(k)
                self._next_k = k + 1
                for step_id, off in self._turn_end_at.get(k, ()):
                    self._turns.put_nowait((step_id, self.start_ns + off))
                step = None
                if k < len(self._seg_of):
                    sg = self._seg_of[k]
                    if sg.kind in ("speech", "resumption") and \
                            np.any(self._script[k * self.spf:(k + 1) * self.spf]):
                        step = sg.step_id
                yield Frame(t_ns=t, seq=j + k, src="caller", pcm=pcm, mulaw=mulaw.encode(pcm),
                            speech=step is not None or ov is not None, step_id=step,
                            overlay_id=ov)
                k += 1
        finally:
            self._turns.put_nowait(_END)

    def inject(self, clip_pcm: np.ndarray, level_db: float, t_ns: int | None = None, *,
               overlay_id: str | None = None) -> str:
        """Mix `clip_pcm` (8 kHz int16) from the next frame at `level_db` re -20 dBFS.
        Returns the overlay id; `injected_at[id]` is the source time of its first frame."""
        x = np.asarray(clip_pcm, dtype=np.float64).ravel()
        k0 = self._next_k
        if t_ns is not None and self.start_ns is not None:
            k0 = max(k0, -(-(t_ns - self.start_ns) // self.frame_ns))
        rms = math.sqrt(float(np.mean(x * x))) if len(x) else 0.0
        if rms > 0:
            gain = _FULL_SCALE * 10 ** ((REF_DBFS + level_db) / 20) / rms
            n = self._frames_for(len(x)) * self.spf
            buf = np.zeros(n)
            buf[:len(x)] = x * gain
        if overlay_id is None:
            overlay_id = f"inj-{self._inj_ids}"
            self._inj_ids += 1
        if rms > 0:
            self._inj.append((k0, buf, overlay_id))
        self.injected_at[overlay_id] = \
            (self.start_ns if self.start_ns is not None else self.clock.now_ns()) + k0 * self.frame_ns
        return overlay_id

    async def turn_ends(self) -> AsyncIterator[tuple[str, int]]:
        while True:
            item = await self.clock.idle(self._turns.get())
            if item is _END:
                return
            yield item

    # -- internals ----------------------------------------------------------
    def _mix(self, k: int) -> tuple[np.ndarray, str | None]:
        """Frame k of the script plus every injection sounding in it, and the id of the
        first injection with non-zero samples in the frame (None if none)."""
        a, b = k * self.spf, (k + 1) * self.spf
        base = self._script[a:b]
        if len(base) < self.spf:
            base = np.concatenate([base, np.zeros(self.spf - len(base), np.int16)])
        live = [(k0, x, oid) for k0, x, oid in self._inj if k0 <= k < k0 + len(x) // self.spf]
        if not live:
            return base.copy(), None
        y = base.astype(np.float64)
        sounding = None
        for k0, x, oid in live:
            o = (k - k0) * self.spf
            seg = x[o:o + self.spf]
            y += seg
            if sounding is None and np.any(np.rint(seg)):
                sounding = oid
        self._inj = [(k0, x, oid) for k0, x, oid in self._inj
                     if k < k0 + len(x) // self.spf - 1 or k < k0]
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16), sounding


def _int16(pcm: np.ndarray) -> np.ndarray:
    a = np.asarray(pcm).ravel()
    if a.dtype != np.int16:
        raise ValueError(f"clips must be int16 8 kHz PCM, got {a.dtype}")
    return a
