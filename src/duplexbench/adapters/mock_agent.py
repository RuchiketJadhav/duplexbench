"""Mock agent adapter with programmed behaviour (spec §1.4; BUILD_PLAN M1-T12).

Driven only by the clock: every wait is `clock.sleep_until_ns`. It speaks a
440 Hz tone at -20 dBFS (or, with `voice="babble"`, a harmonic formant babble
that Silero VAD recognises as speech, used by the calibration's Silero stage:
Silero scores a pure tone as non-speech, p < 0.15).

Timing program (all relative to harness clock times, so calibration can check
the harness against exact values):

* caller turn end: the runner's `call.turn_ended(t)` (spec §1.2 anchor). The mock
  polls `call.turn_end_ns` on its 20 ms tick; its first audio chunk is emitted at
  exactly `t + first_audio_ms`, then one `frame_ms` chunk per `frame_ms` (real-time
  pace, like a frame-synchronous model) for `reply_len_ms`.
* greeting (fix-34): with `greets_ms` set, the mock speaks first: one utterance of
  `greets_ms` starting `first_audio_ms` after `open()`, like Dograh's start-node
  greeting. `greets_ms=None` (default): silent until the first caller turn end.
* tool call: `tool_call` at exactly `t + tool_at_ms` after each turn end
  (`tool_at_ms=None` disables).
* interruption: the mock "hears" caller frames through an energy detector
  (frame RMS above `th.rms_active_dbfs`, and FLOOR_MARGIN_DB above a minimum-tracking
  noise floor, so the C1 noise bed alone is not speech; a C1 burst is heard, like a
  real agent would). A caller frame heard while the mock
  is speaking, sent at `t_i`, schedules the programmed reaction at
  `t_i + stop_after_interrupt_ms`: a `cancel_or_truncate` event and no more audio.

Behaviours (M1 implements the floor-control ones; Track C ones are TODO(M4)):

    stops               stop on any heard interruption                         (M2 (a))
    never_stops         ignore interruptions                                   (M2 (b))
    stops_on_everything stop on any heard caller sound (same as stops in M1;
                        TODO(M2): `stops` should ignore H-* overlays)          (M2 (c))
    short_reply         replies last SHORT_REPLY_MS                            (M2 (d))
    cancel_restart      cancel, then restart the same reply RESTART_GAP_MS later (M2 (e))
    silent_in_tool      after the tool call: silent until the tool result, then the reply
    filler_then_result  after the tool call: filler until the tool result, then the reply
    perfect, stale_writer, claims_success
                        behave as `stops` (TODO(M4): tool-argument and claim semantics)
"""
from __future__ import annotations

import functools
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import numpy as np

from duplexbench.adapters.base import Adapter, AdapterCaps, AdapterEvent
from duplexbench.channel import mulaw
from duplexbench.metrics.activity import frame_rms_dbfs
from duplexbench.thresholds import Thresholds

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

Behaviour = Literal["stops", "never_stops", "stops_on_everything", "short_reply", "cancel_restart",
                    "perfect", "stale_writer", "claims_success", "silent_in_tool",
                    "filler_then_result"]

_NS_PER_MS = 1_000_000
_FULL_SCALE = 32768.0
SHORT_REPLY_MS = 1200          # BUILD_PLAN M2 acceptance (d): "short replies that end at 1.2 s"
RESTART_GAP_MS = 20            # M2 acceptance (e): restart 20 ms after the cancel
FLOOR_MARGIN_DB = 10.0         # mock hearing: speech must exceed the running floor by this
FLOOR_RISE_DB = 0.1            # noise-floor tracker: max rise per frame (minimum statistics)
TONE_HZ = 440.0
LEVEL_DBFS = -20.0
_BABBLE_S = 8                  # length of the cached babble loop
_VOWELS = ((730, 1090, 2440), (270, 2290, 3010), (300, 870, 2240), (530, 1840, 2480),  # threshold-ok: formant Hz, not a spec number
           (640, 1190, 2390))  # Peterson-Barney formants (Hz) of five English vowels
_SYL_S = 0.2                   # one vowel per 200 ms
_F0_HZ = 120.0
_FORMANT_BW_HZ = 80.0
_AM_HZ = 4.0                   # syllabic amplitude modulation
_AM_DEPTH = 0.45               # syllabic envelope 0.1..1.0; Silero needs the modulation


@dataclass(frozen=True)
class MockProgram:
    first_audio_ms: int = 400
    stop_after_interrupt_ms: int = 250
    tool_at_ms: int | None = 1200
    reply_len_ms: int = 3000
    behaviour: Behaviour = "stops"
    rate: int = 24000                      # provider-side audio rate; the runner resamples
    voice: Literal["tone", "babble"] = "tone"
    level_dbfs: float = LEVEL_DBFS
    tool_name: str = "mock_tool"           # name/arguments of the programmed tool_call (a Track C
    tool_args: dict | None = None          # mock targets a scenario tool, e.g. find_slots)
    greets_ms: int | None = None           # fix-34: speak first, a greeting this long


def tone_chunk(rate: int, start: int, n: int, hz: float = TONE_HZ,
               level_dbfs: float = LEVEL_DBFS) -> np.ndarray:
    amp = _FULL_SCALE * 10 ** (level_dbfs / 20) * np.sqrt(2)
    t = (np.arange(n) + start) / rate
    return np.clip(np.rint(amp * np.sin(2 * np.pi * hz * t)), -32768, 32767).astype(np.int16)


@functools.lru_cache(maxsize=4)
def _babble_loop(rate: int, level_dbfs: float) -> np.ndarray:
    n = rate * _BABBLE_S
    t = np.arange(n) / rate
    f0 = _F0_HZ * (1 + 0.08 * np.sin(2 * np.pi * 2.5 * t))
    ph = 2 * np.pi * np.cumsum(f0) / rate
    syl = np.floor(t / _SYL_S).astype(int) % len(_VOWELS)
    y = np.zeros(n)
    for k in range(1, int(min(rate, 8000) / 2 / f0.max())):
        fk = k * f0
        a = np.zeros(n)
        for vi, formants in enumerate(_VOWELS):
            m = syl == vi
            a[m] = sum(1 / (1 + ((fk[m] - f) / _FORMANT_BW_HZ) ** 2) for f in formants)
        y += a * np.sin(k * ph) / np.sqrt(k)
    y *= 1 - _AM_DEPTH + _AM_DEPTH * np.sin(2 * np.pi * _AM_HZ * t - np.pi / 2)
    y *= _FULL_SCALE * 10 ** (level_dbfs / 20) / np.sqrt(np.mean(y ** 2))
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)


def babble_chunk(rate: int, start: int, n: int, level_dbfs: float = LEVEL_DBFS) -> np.ndarray:
    """Speech-like harmonic formant babble (samples start .. start+n-1 of a loop)."""
    loop = _babble_loop(rate, level_dbfs)
    idx = (np.arange(n) + start) % len(loop)
    return loop[idx]


class MockAgent(Adapter):
    name = "mock_agent"
    caps = AdapterCaps(cancel_event=True, native_mulaw=True, output_transcript=False,
                       usage_fields=False)

    def __init__(self, clock: "Clock", program: MockProgram | None = None,
                 th: Thresholds | None = None):
        self.clock = clock
        self.program = program or MockProgram()
        self.th = th or Thresholds()
        self.config = {"adapter": self.name, **self.program.__dict__}
        self.frame_ns = self.th.frame_ms * _NS_PER_MS
        self.chunk = self.program.rate * self.th.frame_ms // 1_000
        self.call: "CallContext | None" = None
        self._closed = False
        self._ids = itertools.count()
        self._floor_db = float("-inf")
        self._turns_seen = 0
        # speaking state
        self._utt_start: int | None = None     # clock time of the current reply's first chunk
        self._utt_end: int | None = None       # reply end (exclusive), None = until tool result
        self._utt_pos = 0                      # samples emitted in this reply
        self._next_chunk: int | None = None
        self._pending_stop: int | None = None
        self._pending_restart: int | None = None
        self._pending_reply: int | None = None
        self._pending_tool: int | None = None
        self._pending_greeting: int | None = None
        self._waiting_tool: str | None = None  # tool_call id awaiting a result
        self._tool_results: list[str] = []
        if self.program.voice == "babble":            # synthesize now, not inside the call
            _babble_loop(self.program.rate, self.program.level_dbfs)
        self.received_frames = 0
        self.heard: list[int] = []             # send times of frames heard as caller speech

    # -- Adapter API --------------------------------------------------------------
    async def open(self, call: "CallContext") -> None:
        self.call = call
        self._t_open = self.clock.now_ns()
        if self.program.greets_ms is not None:
            self._pending_greeting = self._t_open + self.program.first_audio_ms * _NS_PER_MS

    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        self.received_frames += 1
        cond = getattr(self.call, "condition", None) if self.call else None
        if getattr(cond, "codec", True):
            pcm = mulaw.decode(mulaw_bytes)
        else:
            pcm = np.frombuffer(mulaw_bytes, dtype=np.int16)
        rms = frame_rms_dbfs(pcm)
        loud = rms > self.th.rms_active_dbfs and rms > self._floor_db + FLOOR_MARGIN_DB
        if not (np.isfinite(rms) and np.isfinite(self._floor_db)):
            self._floor_db = rms        # digital silence resets the floor; first sound sets it
        else:                           # minimum tracking with a slow rise
            self._floor_db = min(rms, self._floor_db + FLOOR_RISE_DB)
        if not loud:
            return
        self.heard.append(t_ns)
        if self._speaking() and self._pending_stop is None and self._reacts():
            self._pending_stop = t_ns + self.program.stop_after_interrupt_ms * _NS_PER_MS

    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        self._tool_results.append(tool_call_id)
        if self._waiting_tool == tool_call_id:
            self._waiting_tool = None
            now = self.clock.now_ns()
            if self.program.behaviour == "silent_in_tool":
                self._start_reply(now)
            elif self.program.behaviour == "filler_then_result":
                self._start_reply(now)          # filler ends; the result reply starts now

    async def close(self) -> None:
        self._closed = True

    def events(self) -> AsyncIterator[AdapterEvent]:
        return self._events()

    # -- behaviour ----------------------------------------------------------------
    def _reacts(self) -> bool:
        return self.program.behaviour != "never_stops"

    def _speaking(self) -> bool:
        return self._utt_start is not None

    def _reply_len_ns(self) -> int:
        ms = SHORT_REPLY_MS if self.program.behaviour == "short_reply" else self.program.reply_len_ms
        return ms * _NS_PER_MS

    def _start_reply(self, t: int, until_tool: bool = False) -> None:
        self._utt_start = t
        self._utt_pos = 0
        self._utt_end = None if until_tool else t + self._reply_len_ns()
        self._next_chunk = t

    def _end_reply(self) -> None:
        self._utt_start = self._utt_end = self._next_chunk = None

    def _pcm(self, start: int, n: int) -> np.ndarray:
        p = self.program
        if p.voice == "babble":
            return babble_chunk(p.rate, start, n, p.level_dbfs)
        return tone_chunk(p.rate, start, n, TONE_HZ, p.level_dbfs)

    def _next_wake(self, now: int) -> int:
        cands = [now + self.frame_ns]
        for t in (self._next_chunk, self._pending_stop, self._pending_restart,
                  self._pending_reply, self._pending_tool, self._pending_greeting):
            if t is not None:
                cands.append(t)
        return max(now, min(cands))

    async def _events(self) -> AsyncIterator[AdapterEvent]:
        clock = self.clock
        yield AdapterEvent("session_open", clock.now_ns(), "mock.session_open", {})
        while not self._closed:
            now = clock.now_ns()
            # caller turn ends announced by the runner
            if self.call is not None and len(self.call.turn_end_ns) > self._turns_seen:
                for te in self.call.turn_end_ns[self._turns_seen:]:
                    self._pending_reply = te + self.program.first_audio_ms * _NS_PER_MS
                    if self.program.tool_at_ms is not None:
                        self._pending_tool = te + self.program.tool_at_ms * _NS_PER_MS
                self._turns_seen = len(self.call.turn_end_ns)
            await clock.sleep_until_ns(self._next_wake(now))
            now = clock.now_ns()
            # same-time order: stop, restart, reply start, tool, audio chunk
            if self._pending_stop is not None and now >= self._pending_stop:
                self._pending_stop = None
                if self._speaking():
                    self._end_reply()
                    yield AdapterEvent("cancel_or_truncate", now, "mock.cancel", {})
                    if self.program.behaviour == "cancel_restart":
                        self._pending_restart = now + RESTART_GAP_MS * _NS_PER_MS
            if self._pending_restart is not None and now >= self._pending_restart:
                self._pending_restart = None
                self._start_reply(now)
            if self._pending_greeting is not None and now >= self._pending_greeting:
                self._pending_greeting = None
                self._start_reply(now)
                self._utt_end = now + self.program.greets_ms * _NS_PER_MS
            if self._pending_reply is not None and now >= self._pending_reply:
                self._pending_reply = None
                self._start_reply(now)
            if self._pending_tool is not None and now >= self._pending_tool:
                self._pending_tool = None
                tid = f"call_{next(self._ids)}"
                b = self.program.behaviour
                if b in ("silent_in_tool", "filler_then_result"):
                    self._waiting_tool = tid
                    if b == "silent_in_tool":
                        self._end_reply()
                    else:
                        self._start_reply(now, until_tool=True)
                yield AdapterEvent("tool_call", now, "mock.tool_call",
                                   {"id": tid, "name": self.program.tool_name,
                                    "arguments": dict(self.program.tool_args or {})})
            if self._next_chunk is not None and now >= self._next_chunk:
                if self._utt_end is not None and now >= self._utt_end:
                    self._end_reply()
                else:
                    pcm = self._pcm(self._utt_pos, self.chunk)
                    self._utt_pos += self.chunk
                    self._next_chunk = now + self.frame_ns
                    yield AdapterEvent("audio", now, "mock.audio",
                                       {"pcm": pcm, "rate": self.program.rate})
        yield AdapterEvent("closed", clock.now_ns(), "mock.closed", {})
