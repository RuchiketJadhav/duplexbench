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
    SPECULATING / FLOOR_WAIT    start generating the reply from the current transcript
                                (key = (prompt_hash, transcript_hash)); it is discarded
                                if the agent resumes or the transcript changes
    next_action() -> Speak      reply = speculation if its key still matches, else a fresh
                                LLM call; END -> the caller hangs up: frames() ends after
                                the queued audio; otherwise TTS -> 8 kHz frames queued,
                                start_speaking at the send time of the first frame

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
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from duplexbench.channel import mulaw
from duplexbench.clock import Clock
from duplexbench.refasr.engine import Word, text_of
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


@dataclass
class _Spec:
    key: tuple[str, str]
    turn_idx: int
    task: asyncio.Task | None = None
    text: str | None = None


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
        self.refasr_words: list[Word] = []
        self._streamer: Any = None
        self._stream_off_ms = 0.0          # ear ms at which the current stream started
        self._ear_ms = 0.0                 # ear audio pushed so far, ms
        self._asr_q: asyncio.Queue | None = None
        self._asr_task: asyncio.Task | None = None
        self._asr_done = False
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
            self._streamer = self.refasr.stream()
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
        self.refasr_words += [Word(w.text, w.start_ms + shift, w.end_ms + shift, w.conf)
                              for w in words]
        self.on_agent_text(text_of(words))

    def _on_ear_frame(self, frame_idx: int, t_ns: int, pcm: np.ndarray) -> None:
        if self._asr_done or self._streamer is None:
            return
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
        self._streamer = self.refasr.stream()
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

    def _key(self) -> tuple[str, str]:
        return (self.llm.prompt_hash, transcript_hash(self.transcript))

    def _ctx(self) -> SimContext:
        return SimContext.from_scenario(self.scenario, list(self.transcript),
                                        tool_pending=self.policy.tool_pending_now)

    async def _gen(self, spec: _Spec, ctx: SimContext) -> str:
        text = await self.llm.next_utterance(ctx)     # network wait: direct await
        spec.text = text
        if text != END and self._spec is spec:
            self.voice.prepare(spec.key, text)
        return text

    def _start_spec(self) -> None:
        spec = _Spec(self._key(), len(self.policy.turns))
        self._spec = spec
        spec.task = self.clock.task(self._gen(spec, self._ctx()))

    def _discard_spec(self) -> None:
        spec, self._spec = self._spec, None
        if spec is None:
            return
        if spec.task is not None and not spec.task.done():
            spec.task.cancel()
        self.voice.discard()
        text = spec.text if spec.text not in (None, END) else ""
        self._put_row(spec.turn_idx, text, "[]", None, None, None, True, True)

    def _put_row(self, turn_idx: int, text: str, words_json: str, send_ns: int | None,
                 think_ms: float | None, late_ms: float | None, speculative: bool,
                 discarded: bool) -> None:
        row = {"run_id": self.run_id, "call_id": self.call_id, "turn_idx": turn_idx,
               "text": text, "tts_words_json": words_json, "send_ns": send_ns,
               "think_ms": think_ms, "late_ms": late_ms, "speculative": int(speculative),
               "discarded": int(discarded), "prompt_hash": self.llm.prompt_hash}
        self.rows.append(row)
        if self.store is not None:
            self.store.put("sim_turns", row)

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

    async def _reply(self) -> tuple[str, Utterance | None, bool]:
        """(text, utterance or None for END, used_speculation)."""
        spec = self._spec
        if spec is not None and spec.key == self._key():
            self._spec = None
            text = await self.clock.idle(spec.task)
            if text == END:
                return END, None, True
            utt = await self.voice.take(spec.key)
            if utt is None:
                utt = await self.voice.synth_utterance(text)
            return text, utt, True
        if spec is not None:
            self._discard_spec()                      # transcript moved on: stale
        text = await self.llm.next_utterance(self._ctx())
        if text == END:
            return END, None, False
        return text, await self.voice.synth_utterance(text), False

    def _onset_ns(self) -> int:
        now = self.clock.now_ns()
        if self.start_ns is None:
            return now
        return max(now, self.start_ns + self._next_k * self.frame_ns)

    async def _speak(self, text: str, utt: Utterance, first: bool, used_spec: bool) -> bool:
        onset = self._onset_ns()
        if first:
            rec = self.policy.start_first_turn(onset)
        else:
            if not isinstance(self.policy.next_action(onset), Speak):
                return False                          # floor lost meanwhile
            rec = self.policy.start_speaking(onset)
        step = f"sim{rec.turn_idx}"
        fr = to_frames(utt.pcm, self.spf) or [self._silence.copy()]
        done = asyncio.Event()
        for i, f in enumerate(fr):
            last = i == len(fr) - 1
            self._q.append(_QFrame(f, step, last, done if last else None))
        self.transcript.append(("caller", text))
        self._put_row(rec.turn_idx, text, utt.words_json(), onset, rec.think_ms,
                      None if first else rec.late_ms, used_spec, False)
        await self.clock.idle(done.wait())
        self.policy.finish_speaking(self.clock.now_ns())
        return True

    async def _run(self) -> None:
        try:
            if self.caller_first:
                text, utt, _ = await self._reply()
                if text == END or utt is None:
                    self.hung_up = True
                    return
                await self._speak(text, utt, True, False)
            while not self.hung_up:
                a = self.policy.next_action()
                if self.policy.speculation_active() and self._spec is None:
                    self._start_spec()
                if isinstance(a, Speak):
                    text, utt, used = await self._reply()
                    if text == END or utt is None:
                        self.hung_up = True
                        return
                    if not await self._speak(text, utt, False, used):
                        self.voice.discard()
                        self._put_row(len(self.policy.turns), text, "[]", None, None, None,
                                      used, True)
                    continue
                assert isinstance(a, Wait)
                await self._wait(a.until_ns)
        except asyncio.CancelledError:
            raise
        except BaseException as e:      # surfaced by frames()
            self.error = e
            self.hung_up = True
