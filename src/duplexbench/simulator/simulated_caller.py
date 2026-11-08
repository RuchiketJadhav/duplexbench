"""Simulated caller source for Track C (BUILD_PLAN M4-T10/M4-T09; spec §7).

    caller = SimulatedCaller(scenario, llm, voice, clock, th, store,
                             run_id=..., call_id=..., rng_seed=...,
                             tool_server=server, tool_token=token,
                             refasr=engine, asr_threaded=False)
    caller.attach(playout)           # agent onset/stop on the ear -> TurnPolicy;
                                     # with refasr: every played ear frame -> refasr stream
    caller.on_agent_text(text)       # what the caller hears (reference-ASR text) -> LLM context
    caller.on_provider_text(text)    # provider output_transcript -> provider_transcript only
    await caller.finish_ear()        # after the call: flush the reference-ASR stream
    async for f in caller.frames(): ...                   # CallerSource: paced 20 ms frames
    async for step_id, t_ns in caller.turn_ends(): ...    # ("sim0", t), ("sim1", t), ...

How it runs
-----------
frames() is the paced side: it sleeps until each frame's send time on the
clock and hands out the next queued speech frame, or a silence frame (pcm 0,
mu-law 0xff) so the line never starves. Overlays passed to inject() are mixed
on top (scaled to -20 dBFS + level_db, as in ScriptedCaller).

The first frames() pull spawns the decision loop with clock.task:

    next_action() -> Wait(t)    sleep until t or until an input event (agent onset/stop,
                                tool start/result, agent text), whichever comes first
    SPECULATING / FLOOR_WAIT    start generating the reply from the LLM input (the
                                transcript, plus the current agent turn's provisional
                                words when finals lag; fix-42) (key = (prompt_hash,
                                transcript_hash(input))); it is discarded if the agent
                                resumes or the input changes
    next_action() -> Speak      reply = speculation if it still matches (below), else a
                                fresh LLM call; END -> the caller hangs up: frames() ends
                                after the queued audio; otherwise TTS -> 8 kHz frames
                                queued, start_speaking at the send time of the first frame

The LLM call is a network wait and is awaited directly (clock rule); waits on
events completed by other accounted tasks go through clock.idle.

What the caller hears (spec §7 "Hears")
---------------------------------------
With `refasr` (a `refasr.engine.RefASR`), attach() also subscribes to every played
ear frame (`Playout.subscribe_frames`) and pushes it into `refasr.stream()`; the
words each push releases go to `on_agent_text`, the agent side of the LLM
transcript. The provider's own `output_transcript` goes to `on_provider_text`,
is kept in `provider_transcript` (for `mishearing_rate`) and never enters the LLM
context. Engines whose streamer releases no partials (`BufferedStreamer`) are
finished at every agent `stop` on the ear and a new stream is started, so each
agent utterance is heard once it ends. `refasr_words` keeps every heard word with
times in ms from the first ear frame.

Provisional words (fix-24): the ear stream is opened with
`refasr.engine.open_stream(refasr, provisional=True)`, so an engine such as Deepgram
also releases interim words with `final=False`. Only final words enter the LLM
transcript (`on_agent_text`), `refasr_words` and `refasr_text` (mishearing_rate), so
the transcript never carries text that is later corrected. Every provisional word is
kept, with ear times, in `refasr_provisional`. Turn-taking stays on the playout segmenter.

Speculation from provisional words (fix-42; spec §7 "Generation starts speculatively from
partial ASR"): finals can land 0.5-5.6 s after the agent's speech end (Deepgram, measured
2026-11-08), so the draft may start from provisional words. `_pending` holds the current
agent turn's provisional words not yet covered by finals: the latest release per word
slot (`refasr.engine.merge_provisional`: a correction replaces the word it overlaps in
time), only words starting at or after the end of the last final word (and after any held
reply boundary, below); a final release drops every pending word that starts before its
end. The LLM input is `self.transcript` with the pending text appended to the current agent
turn (a new agent entry when the agent has no final words yet). `speculative_source` is
"provisional" when pending text was in the input, else "final".

  At Speak the speculation is checked against the input as it is now:
    both "final"        key equality, as before fix-42;
    otherwise           normalized equality of the two inputs, per entry (role, norm(text)),
                        norm = casefold, delete every character that is neither a word
                        character (\\w) nor whitespace, collapse whitespace runs to one space,
                        strip.
  Match -> the speculation is used; the row is tagged `provisional_reply` when the input
  still carries pending provisional text (no final yet for those words). No match on a
  "provisional" speculation -> discarded (policy.discarded_speculations += 1,
  flags["provisional_mismatches"] += 1; its LLM task is not cancelled, so its usage is
  still logged) and a fresh reply is generated from the current input; a fresh reply from
  an input with pending text is also a `provisional_reply`.

  A provisional reply never writes provisional words into `transcript` or `refasr_words`.
  It records a hold (transcript position of that agent turn, ear ms handed to the ASR at
  that moment); a final word that later arrives with start_ms before the hold's boundary
  is written into that agent entry (inserted before the caller's line when the turn had no
  final words), so the transcript keeps the agent's final words in order.

  Counters (flags, only when > 0, beside `unscripted_overlap`): `speculation_from_provisional`
  (speculations started from a provisional input), `provisional_mismatches`,
  `provisional_replies`. Rows in `self.rows` carry `speculative_source` (speculative rows;
  None otherwise) and `provisional_reply`; the `sim_turns` table schema is unchanged, so
  these two keys are not written to the store.

Finalize on agent stop (fix-40, Dograh parity): `_on_playout(kind="stop")` (the
playout segmenter's stop event, published from `Playout` after the tick's frame was
pushed to the ear stream) calls `refasr.engine.finalize_if_supported` on the ear
streamer, inline (`asr_threaded=False`) or as a "finalize" item on the worker queue
behind the frames already queued (`asr_threaded=True`). Deepgram then flushes the
utterance's final at once (`{"type":"Finalize"}`, as pipecat does on its local VAD
stop); engines without `finalize()` are untouched. The segmenter's own rule sets the
delay (metrics/activity.py, unchanged): a stop is reported once the inactivity run has
lasted `inactivity_run_ms` (500 ms): on the tick that plays the run's 25th inactive
20 ms frame, i.e. 480 ms after the tick of the run's first frame, when 500 ms of
silence after the speech end has been pushed. Finalize therefore goes out 500 ms of
ear audio after the agent's speech end (pipecat: stop_secs 0.2). `finalize_count`
counts the finalize calls made. The transcript still takes final words only.

`asr_threaded=False` (VirtualClock, FakeASR): push/finish run inline in the
playout tick, deterministic. `asr_threaded=True` (a real model): the tick only
queues a copy of the frame; one accounted worker task takes frames from the queue
(clock.idle: an in-loop queue) and awaits `asyncio.to_thread(push)` directly (a
thread is completed from outside the loop; never through clock.idle).

sim_turns rows: one per spoken turn (discarded=0) and one per discarded
speculation (discarded=1, text = what had been generated, may be empty).
flags["unscripted_overlap"] counts agent onsets while the caller speaks
(TurnPolicy tags them; the caller finishes its utterance).
"""
from __future__ import annotations

import asyncio
import itertools
import math
import re
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from duplexbench.channel import mulaw
from duplexbench.clock import Clock
from duplexbench.refasr.engine import (Word, finalize_if_supported, merge_provisional,
                                       open_stream, text_of)
from duplexbench.stimuli.caller_source import Frame
from duplexbench.simulator.audio import CallerVoice, Utterance, to_frames, transcript_hash
from duplexbench.simulator.caller_llm import END, CallerLLM, SimContext
from duplexbench.simulator.turn_policy import (UNSCRIPTED_OVERLAP, Speak, TurnPolicy, TurnState,
                                               Wait)
from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000
_MS_PER_S = 1_000
_FULL_SCALE = 32768.0
REF_DBFS = -20.0
_END_Q = object()
FINAL, PROVISIONAL = "final", "provisional"
SPEC_FROM_PROVISIONAL = "speculation_from_provisional"
PROVISIONAL_MISMATCHES = "provisional_mismatches"
PROVISIONAL_REPLIES = "provisional_replies"
_ROW_ONLY_KEYS = ("speculative_source", "provisional_reply")   # self.rows only: no column
_PUNCT = re.compile(r"[^\w\s]")

Transcript = list[tuple[str, str]]


def norm_text(text: str) -> str:
    """fix-42 matching rule: casefold, delete every character that is neither a word
    character (\\w) nor whitespace, collapse whitespace runs to one space, strip."""
    return " ".join(_PUNCT.sub("", text.casefold()).split())


def norm_transcript(tr: Transcript) -> tuple[tuple[str, str], ...]:
    return tuple((role, norm_text(t)) for role, t in tr)


@dataclass
class _Spec:
    key: tuple[str, str]
    turn_idx: int
    task: asyncio.Task | None = None
    text: str | None = None
    source: str = "final"
    norm: tuple = ()


@dataclass
class _Hold:
    idx: int               # transcript index of the held agent entry (or where it goes)
    has_agent: bool        # an agent entry already sits at idx
    boundary_ms: float     # ear ms handed to the ASR when the caller took the floor


@dataclass
class _QFrame:
    pcm: np.ndarray
    step_id: str
    last: bool
    done: asyncio.Event | None


class SimulatedCaller:
    def __init__(self, scenario: Any, llm: CallerLLM, voice: CallerVoice, clock: Clock,
                 th: Thresholds, store: Any, *, run_id: str, call_id: str, rng_seed: int,
                 tool_server: Any = None, tool_token: str | None = None,
                 caller_first: bool = False, rate: int = 8000, refasr: Any = None,
                 asr_threaded: bool = False) -> None:
        self.scenario = scenario
        self.llm = llm
        self.voice = voice
        self.clock = clock
        self.th = th
        self.store = store
        self.run_id, self.call_id = run_id, call_id
        self.policy = TurnPolicy(clock, th, rng_seed)
        self.caller_first = caller_first
        self.rate = rate
        self.frame_ns = th.frame_ms * _NS_PER_MS
        self.spf = rate * th.frame_ms // _MS_PER_S
        self._silence = np.zeros(self.spf, np.int16)
        self._silence_mulaw = mulaw.encode(self._silence)
        self.transcript: list[tuple[str, str]] = []
        self.provider_transcript: list[str] = []
        self.refasr = refasr
        self.asr_threaded = asr_threaded
        self.refasr_words: list[Word] = []          # final words only (fix-24)
        self.refasr_provisional: list[Word] = []    # provisional words, never in the transcript
        self._streamer: Any = None
        self._stream_off_ms = 0.0          # ear ms at which the current stream started
        self._ear_ms = 0.0                 # ear audio pushed so far, ms
        self._asr_q: asyncio.Queue | None = None
        self._asr_task: asyncio.Task | None = None
        self._asr_done = False
        self.finalize_count = 0            # fix-40: finalize() calls on the ear stream
        self._pending: list[Word] = []     # fix-42: current turn's provisional words
        self._final_end_ms = -math.inf     # end of the last final word (ear ms)
        self._ear_in_ms = 0.0              # ear ms handed to the ASR (queued frames included)
        self._holds: list[_Hold] = []      # provisional replies whose finals may come late
        self._orphans: list[asyncio.Task] = []   # mismatched speculations left to finish
        self.flags: dict[str, int] = {}
        self.rows: list[dict] = []
        self._q: deque[_QFrame] = deque()
        self._inj: list[tuple[str, int, np.ndarray]] = []    # (id, start frame, float samples)
        self._ids = itertools.count()
        self._turns: asyncio.Queue = asyncio.Queue()
        self._wake = asyncio.Event()
        self._spec: _Spec | None = None
        self._loop_task: asyncio.Task | None = None
        self.start_ns: int | None = None
        self._next_k = 0
        self.hung_up = False
        self.error: BaseException | None = None
        if tool_server is not None and tool_token is not None:
            tool_server.subscribe(tool_token, self._on_tool)

    # -- inputs (hooks the run command wires) ---------------------------------------
    def attach(self, playout: Any) -> None:
        """Subscribe to the playout segmenter (onset/stop of agent speech on the ear) and,
        with a reference ASR, to every played ear frame."""
        playout.subscribe(self._on_playout)
        if self.refasr is not None:
            self._streamer = open_stream(self.refasr, provisional=True)
            playout.subscribe_frames(self._on_ear_frame)

    def on_agent_text(self, text: str) -> None:
        """What the caller heard (reference-ASR text of the ear); consecutive chunks merge
        into one agent turn of the LLM transcript."""
        text = (text or "").strip()
        if not text:
            return
        if self.transcript and self.transcript[-1][0] == "agent":
            self.transcript[-1] = ("agent", f"{self.transcript[-1][1]} {text}")
        else:
            self.transcript.append(("agent", text))
        self._wake.set()

    def on_provider_text(self, text: str) -> None:
        """Provider output_transcript text: kept for mishearing_rate, never in the LLM context."""
        text = (text or "").strip()
        if text:
            self.provider_transcript.append(text)

    @property
    def provider_text(self) -> str:
        return " ".join(self.provider_transcript)

    @property
    def refasr_text(self) -> str:
        return text_of(self.refasr_words)

    # -- the ear through the reference ASR --------------------------------------------
    def _buffered(self) -> bool:
        from duplexbench.refasr.candidates import BufferedStreamer
        return isinstance(self._streamer, BufferedStreamer)

    def _heard(self, words: list[Word], off_ms: float) -> None:
        if not words:
            return
        shift = int(round(off_ms))
        shifted = [Word(w.text, w.start_ms + shift, w.end_ms + shift, w.conf, w.final)
                   for w in words]
        prov = [w for w in shifted if not w.final]
        self.refasr_provisional += prov
        floor = max([self._final_end_ms] + [h.boundary_ms for h in self._holds])
        for w in prov:
            if w.start_ms >= floor:
                self._pending = merge_provisional(self._pending, w)
        final = [w for w in shifted if w.final]
        if not final:
            return
        self.refasr_words += final
        self._final_end_ms = max(self._final_end_ms, max(w.end_ms for w in final))
        self._pending = [p for p in self._pending if p.start_ms >= self._final_end_ms]
        normal: list[Word] = []
        for w in final:
            hold = next((h for h in self._holds if w.start_ms < h.boundary_ms), None)
            if hold is None:
                normal.append(w)
            else:
                self._held_word(hold, w)
        if normal:
            self.on_agent_text(text_of(normal))

    def _held_word(self, hold: _Hold, w: Word) -> None:
        """A late final of an agent turn the caller already answered (provisional reply,
        fix-42): written into that turn's agent entry, before the caller's line."""
        if hold.has_agent:
            role, t = self.transcript[hold.idx]
            self.transcript[hold.idx] = (role, f"{t} {w.text}".strip())
        else:
            self.transcript.insert(hold.idx, ("agent", w.text))
            hold.has_agent = True
            for h in self._holds:
                if h is not hold and h.idx >= hold.idx:
                    h.idx += 1
        self._wake.set()

    def _on_ear_frame(self, frame_idx: int, t_ns: int, pcm: np.ndarray) -> None:
        if self._asr_done or self._streamer is None:
            return
        self._ear_in_ms += len(pcm) * _MS_PER_S / self.rate
        if not self.asr_threaded:
            off = self._stream_off_ms
            self._ear_ms += len(pcm) * _MS_PER_S / self.rate
            self._heard(self._streamer.push(pcm), off)
            return
        if self._asr_q is None:
            self._asr_q = asyncio.Queue()
            self._asr_task = self.clock.task(self._asr_worker())
        self._asr_q.put_nowait(("push", np.array(pcm, dtype=np.int16, copy=True)))

    def _restart(self, words: list[Word]) -> None:
        off = self._stream_off_ms
        self._streamer = open_stream(self.refasr, provisional=True)
        self._stream_off_ms = self._ear_ms
        self._heard(words, off)

    async def _asr_worker(self) -> None:
        assert self._asr_q is not None
        try:
            while True:
                op, pcm = await self.clock.idle(self._asr_q.get())
                if op == "end":
                    return
                if op == "push":
                    off = self._stream_off_ms
                    self._ear_ms += len(pcm) * _MS_PER_S / self.rate
                    words = await asyncio.to_thread(self._streamer.push, pcm)   # direct await
                    self._heard(words, off)
                elif op == "restart":
                    self._restart(await asyncio.to_thread(self._streamer.finish))
                elif op == "finalize":
                    if await asyncio.to_thread(finalize_if_supported, self._streamer):
                        self.finalize_count += 1
        except asyncio.CancelledError:
            raise
        except BaseException as e:      # surfaced by frames()
            self.error = e
            self.hung_up = True
            self._wake.set()

    def _asr_stop(self) -> None:
        """Agent stop on the ear: a buffered (batch) engine transcribes the utterance now."""
        if self._streamer is None or self._asr_done or not self._buffered():
            return
        if not self.asr_threaded:
            self._restart(self._streamer.finish())
        elif self._asr_q is not None:
            self._asr_q.put_nowait(("restart", None))

    def _asr_finalize(self) -> None:
        """Agent stop on the ear (fix-40): ask a streaming engine with finalize() to settle
        the utterance now (Deepgram Finalize). Buffered engines restart in _asr_stop."""
        if self._streamer is None or self._asr_done or self._buffered():
            return
        if not self.asr_threaded:
            if finalize_if_supported(self._streamer):
                self.finalize_count += 1
        elif self._asr_q is not None:
            self._asr_q.put_nowait(("finalize", None))

    async def finish_ear(self) -> None:
        """After the call: drain the worker and release every word not yet heard. Idempotent."""
        if self._streamer is None or self._asr_done:
            return
        self._asr_done = True
        if self._asr_task is not None and self._asr_q is not None:
            if not self._asr_task.done():
                self._asr_q.put_nowait(("end", None))
                await self.clock.idle(self._asr_task)
            words = await asyncio.to_thread(self._streamer.finish)
        else:
            words = self._streamer.finish()
        self._heard(words, self._stream_off_ms)

    def _on_playout(self, kind: str, frame_idx: int, t_ns: int) -> None:
        if kind == "onset":
            if self.policy.on_agent_active(t_ns) == UNSCRIPTED_OVERLAP:
                self.flags[UNSCRIPTED_OVERLAP] = self.flags.get(UNSCRIPTED_OVERLAP, 0) + 1
            else:
                self._discard_spec()
        elif kind == "stop":
            self.policy.on_agent_inactive(t_ns)
            self._asr_stop()
            self._asr_finalize()
        self._wake.set()

    def _on_tool(self, kind: str, info: dict) -> None:
        t = info.get("t_ns")
        if kind == "start":
            self.policy.tool_pending(True, t)
        elif kind == "result":
            self.policy.tool_pending(False, t)
        self._wake.set()

    # -- CallerSource -----------------------------------------------------------------
    async def frames(self) -> AsyncIterator[Frame]:
        self.start_ns = self.clock.now_ns()
        self._loop_task = self.clock.task(self._run())
        k = 0
        try:
            while True:
                if self.error is not None:
                    raise self.error
                if self.hung_up and not self._q and not self._live_inj(k):
                    return
                t = self.start_ns + k * self.frame_ns
                await self.clock.sleep_until_ns(t)
                speech, step, ov = False, None, None
                qf = self._q.popleft() if self._q else None
                pcm = qf.pcm if qf is not None else self._silence
                if qf is not None:
                    speech, step = True, qf.step_id
                live = self._live_inj(k)
                if live:
                    y = pcm.astype(np.float64)
                    for oid, k0, x in live:
                        o = (k - k0) * self.spf
                        seg = x[o:o + self.spf]
                        y[:len(seg)] += seg
                        if np.any(seg):
                            speech, ov = True, oid
                    pcm = np.clip(np.rint(y), -32768, 32767).astype(np.int16)
                    self._inj = [e for e in self._inj if k < e[1] + len(e[2]) // self.spf - 1]
                self._next_k = k + 1
                enc = self._silence_mulaw if (qf is None and not live) else mulaw.encode(pcm)
                if qf is not None and qf.last:
                    self._turns.put_nowait((qf.step_id, t))
                    if qf.done is not None:
                        qf.done.set()
                yield Frame(t_ns=t, seq=k, src="caller", pcm=pcm.copy(), mulaw=enc,
                            speech=speech, step_id=step, overlay_id=ov)
                k += 1
        finally:
            self.hung_up = True
            if self._loop_task is not None and not self._loop_task.done():
                self._loop_task.cancel()
            self._discard_spec()
            for t in self._orphans:
                if not t.done():
                    t.cancel()
            self._turns.put_nowait(_END_Q)

    def inject(self, clip_pcm: np.ndarray, level_db: float, t_ns: int | None = None) -> str:
        """Mix `clip_pcm` (8 kHz int16) at `level_db` re -20 dBFS from the next frame not
        yet handed out (or the frame at `t_ns`, if later). Returns the overlay id."""
        oid = f"inj-{next(self._ids)}"
        x = np.asarray(clip_pcm, dtype=np.float64).ravel()
        k0 = self._next_k
        if t_ns is not None and self.start_ns is not None:
            k0 = max(k0, -(-(t_ns - self.start_ns) // self.frame_ns))
        rms = math.sqrt(float(np.mean(x * x))) if len(x) else 0.0
        if rms > 0:
            gain = _FULL_SCALE * 10 ** ((REF_DBFS + level_db) / 20) / rms
            n = -(-len(x) // self.spf) * self.spf
            buf = np.zeros(n)
            buf[:len(x)] = x * gain
            self._inj.append((oid, int(k0), buf))
        return oid

    async def turn_ends(self) -> AsyncIterator[tuple[str, int]]:
        while True:
            item = await self.clock.idle(self._turns.get())
            if item is _END_Q:
                return
            yield item

    def close(self) -> None:
        """Hang up: frames() ends after the queued audio."""
        self.hung_up = True
        self._wake.set()

    # -- internals --------------------------------------------------------------------
    def _live_inj(self, k: int) -> list:
        return [e for e in self._inj if e[1] <= k < e[1] + len(e[2]) // self.spf]

    def _input(self) -> tuple[Transcript, str]:
        """(LLM input, source): the transcript, plus the current agent turn's pending
        provisional text when there is any (fix-42)."""
        tr = list(self.transcript)
        prov = text_of(self._pending).strip()
        if not prov:
            return tr, FINAL
        if tr and tr[-1][0] == "agent":
            tr[-1] = ("agent", f"{tr[-1][1]} {prov}")
        else:
            tr.append(("agent", prov))
        return tr, PROVISIONAL

    def _key(self, tr: Transcript | None = None) -> tuple[str, str]:
        return (self.llm.prompt_hash,
                transcript_hash(self.transcript if tr is None else tr))

    def _ctx(self, tr: Transcript | None = None) -> SimContext:
        return SimContext.from_scenario(self.scenario,
                                        list(self.transcript if tr is None else tr),
                                        tool_pending=self.policy.tool_pending_now)

    def _count(self, flag: str) -> None:
        self.flags[flag] = self.flags.get(flag, 0) + 1

    async def _gen(self, spec: _Spec, ctx: SimContext) -> str:
        text = await self.llm.next_utterance(ctx)     # network wait: direct await
        spec.text = text
        if text != END and self._spec is spec:
            self.voice.prepare(spec.key, text)
        return text

    def _start_spec(self) -> None:
        tr, src = self._input()
        spec = _Spec(self._key(tr), len(self.policy.turns), source=src,
                     norm=norm_transcript(tr))
        self._spec = spec
        if src == PROVISIONAL:
            self._count(SPEC_FROM_PROVISIONAL)
        spec.task = self.clock.task(self._gen(spec, self._ctx(tr)))

    def _discard_spec(self, *, cancel: bool = True) -> None:
        spec, self._spec = self._spec, None
        if spec is None:
            return
        if spec.task is not None and not spec.task.done():
            if cancel:
                spec.task.cancel()
            else:
                self._orphans.append(spec.task)     # finishes: its LLM usage is logged
        self.voice.discard()
        text = spec.text if spec.text not in (None, END) else ""
        self._put_row(spec.turn_idx, text, "[]", None, None, None, True, True,
                      source=spec.source)

    def _put_row(self, turn_idx: int, text: str, words_json: str, send_ns: int | None,
                 think_ms: float | None, late_ms: float | None, speculative: bool,
                 discarded: bool, *, source: str | None = None,
                 provisional_reply: bool = False) -> None:
        row = {"run_id": self.run_id, "call_id": self.call_id, "turn_idx": turn_idx,
               "text": text, "tts_words_json": words_json, "send_ns": send_ns,
               "think_ms": think_ms, "late_ms": late_ms, "speculative": int(speculative),
               "discarded": int(discarded), "prompt_hash": self.llm.prompt_hash,
               "speculative_source": source if speculative else None,
               "provisional_reply": int(provisional_reply)}
        self.rows.append(row)
        if self.store is not None:     # sim_turns schema unchanged: fix-42 keys stay in rows
            self.store.put("sim_turns",
                           {k: v for k, v in row.items() if k not in _ROW_ONLY_KEYS})

    async def _wait(self, until_ns: int | None) -> None:
        if self._wake.is_set():
            self._wake.clear()
            return
        timer = None
        if until_ns is not None:
            async def _timer() -> None:
                await self.clock.sleep_until_ns(until_ns)
                self._wake.set()
            timer = self.clock.task(_timer())
        try:
            await self.clock.idle(self._wake.wait())
        finally:
            self._wake.clear()
            if timer is not None and not timer.done():
                timer.cancel()

    def _spec_matches(self, spec: _Spec, tr: Transcript, src: str) -> bool:
        if spec.source == FINAL and src == FINAL:
            return spec.key == self._key(tr)          # as before fix-42
        return spec.norm == norm_transcript(tr)       # fix-42 normalized match

    async def _reply(self) -> tuple[str, Utterance | None, bool, str | None, bool]:
        """(text, utterance or None for END, used_speculation, speculative_source,
        provisional_reply)."""
        spec = self._spec
        tr, src = self._input()
        prov = src == PROVISIONAL                     # input still carries provisional text
        if spec is not None and self._spec_matches(spec, tr, src):
            self._spec = None
            text = await self.clock.idle(spec.task)
            if text == END:
                return END, None, True, spec.source, prov
            utt = await self.voice.take(spec.key)
            if utt is None:
                utt = await self.voice.synth_utterance(text)
            return text, utt, True, spec.source, prov
        if spec is not None:
            if spec.source == PROVISIONAL:            # the finals (or a correction) differ
                self.policy.discarded_speculations += 1
                self._count(PROVISIONAL_MISMATCHES)
                self._discard_spec(cancel=False)
            else:
                self._discard_spec()                  # transcript moved on: stale
        text = await self.llm.next_utterance(self._ctx(tr))
        if text == END:
            return END, None, False, None, prov
        return text, await self.voice.synth_utterance(text), False, None, prov

    def _onset_ns(self) -> int:
        now = self.clock.now_ns()
        if self.start_ns is None:
            return now
        return max(now, self.start_ns + self._next_k * self.frame_ns)

    async def _speak(self, text: str, utt: Utterance, first: bool, used_spec: bool,
                     source: str | None = None, prov_reply: bool = False) -> bool:
        onset = self._onset_ns()
        if first:
            rec = self.policy.start_first_turn(onset)
        else:
            if not isinstance(self.policy.next_action(onset), Speak):
                return False                          # floor lost meanwhile
            rec = self.policy.start_speaking(onset)
        step = f"sim{rec.turn_idx}"
        if prov_reply:                                # fix-42: late finals go to this turn
            has_agent = bool(self.transcript) and self.transcript[-1][0] == "agent"
            self._holds.append(_Hold(len(self.transcript) - 1 if has_agent
                                     else len(self.transcript), has_agent, self._ear_in_ms))
            self._pending = []
            self._count(PROVISIONAL_REPLIES)
        fr = to_frames(utt.pcm, self.spf) or [self._silence.copy()]
        done = asyncio.Event()
        for i, f in enumerate(fr):
            last = i == len(fr) - 1
            self._q.append(_QFrame(f, step, last, done if last else None))
        self.transcript.append(("caller", text))
        self._put_row(rec.turn_idx, text, utt.words_json(), onset, rec.think_ms,
                      None if first else rec.late_ms, used_spec, False, source=source,
                      provisional_reply=prov_reply)
        await self.clock.idle(done.wait())
        self.policy.finish_speaking(self.clock.now_ns())
        return True

    async def _run(self) -> None:
        try:
            if self.caller_first:
                text, utt, _, _, _ = await self._reply()
                if text == END or utt is None:
                    self.hung_up = True
                    return
                await self._speak(text, utt, True, False)
            while not self.hung_up:
                a = self.policy.next_action()
                if self.policy.speculation_active() and self._spec is None:
                    self._start_spec()
                if isinstance(a, Speak):
                    text, utt, used, source, prov = await self._reply()
                    if text == END or utt is None:
                        self.hung_up = True
                        return
                    if not await self._speak(text, utt, False, used, source, prov):
                        self.voice.discard()
                        self._put_row(len(self.policy.turns), text, "[]", None, None, None,
                                      used, True, source=source, provisional_reply=prov)
                    continue
                assert isinstance(a, Wait)
                await self._wait(a.until_ns)
        except asyncio.CancelledError:
            raise
        except BaseException as e:      # surfaced by frames()
            self.error = e
            self.hung_up = True
