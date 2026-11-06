"""fix-20: the simulated caller hears the ear through the reference ASR (spec §7 "Hears").

* Playout.subscribe_frames: one callback per played frame (silence included), matching
  the playout rows, without changing the onset/stop subscribers.
* SimulatedCaller(refasr=FakeASR): words released by the ear stream reach the agent side
  of the LLM transcript; provider output_transcript text goes to provider_transcript only.
  Inline (VirtualClock) and threaded paths hear the same words; a buffered (batch) engine
  is finished at each agent stop.
* metrics.task: mishearing_rate = WER(provider text vs reference-ASR text), diagnostic.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
import yaml

from duplexbench.calibrate import MemStore
from duplexbench.clock import VirtualClock
from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, Event
from duplexbench.metrics.task import edit_distance, mishearing, score_track_c
from duplexbench.playout import Playout
from duplexbench.refasr.candidates import BufferedStreamer
from duplexbench.refasr.engine import FakeASR, Word
from duplexbench.scenarios.schema import Scenario
from duplexbench.simulator.audio import CallerVoice
from duplexbench.simulator.caller_llm import END
from duplexbench.simulator.simulated_caller import SimulatedCaller
from duplexbench.stimuli.fake_tts import FakeTTS
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()
SPF = 8000 * TH.frame_ms // 1000
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev" / "track_c_001.yaml"
EAR_WORDS = [Word("hello", 0, 300), Word("there", 400, 800)]


# -- 1. the per-frame hook ------------------------------------------------------------------
def test_subscribe_frames_every_tick_including_silence():
    clock = VirtualClock()
    store = MemStore()
    po = Playout(clock, store, None, TH, run_id="r", call_id="c")
    seen, kinds = [], []
    po.subscribe_frames(lambda k, t, pcm: seen.append((k, t, pcm.copy())))
    po.subscribe(lambda kind, k, t: kinds.append(kind))
    for _ in range(5):
        po.enqueue(np.full(SPF, 3000, np.int16), 0)
    n_ticks = 12

    async def main():
        t = clock.task(po.run())
        await clock.sleep_until_ns((n_ticks * TH.frame_ms - 1) * MS)
        po.stop()
        t.cancel()
        await clock.idle(asyncio.gather(t, return_exceptions=True))

    asyncio.run(clock.run(main()))
    rows = store.rows["playout"]
    assert len(seen) == len(rows) == n_ticks
    assert [k for k, _, _ in seen] == [r["frame_idx"] for r in rows]
    assert [t for _, t, _ in seen] == [r["t_ns"] for r in rows]
    for (_k, _t, pcm), r in zip(seen, rows):
        assert pcm.dtype == np.int16 and len(pcm) == SPF
        assert bool(np.any(pcm)) == bool(r["active"])          # silence frames included
    assert sum(1 for _, _, p in seen if np.any(p)) == 5
    assert kinds == ["onset"]                                    # onset/stop API unchanged


# -- 2. the caller hears the ear ----------------------------------------------------------------
class CtxLLM:
    prompt_hash = "ph-test"

    def __init__(self, utts=("hi I need help",)):
        self.utts, self.contexts = list(utts), []

    async def next_utterance(self, ctx):
        self.contexts.append(ctx)
        i = ctx.caller_turns
        return self.utts[i] if i < len(self.utts) else END


def _scenario():
    return Scenario.model_validate({
        "id": "c1", "version": "1", "split": "public-dev", "domain": "retail", "track": "C",
        "source": "simulated", "caller_voice": "v1", "persona": "p", "goal": "g",
        "steps": [{"id": "s1", "text": "order late"}]})


def _call(engine, *, threaded=False, agent_frames=50, until_ms=4000):
    """Real Playout + SimulatedCaller on a VirtualClock; the agent plays `agent_frames`
    loud frames from t=0 and emits a provider transcript that differs from the ear."""
    clock = VirtualClock()
    store = MemStore()
    llm = CtxLLM()
    voice = CallerVoice(FakeTTS(), "v1", 1.0, threaded=False)
    caller = SimulatedCaller(_scenario(), llm, voice, clock, TH, store, run_id="r",
                             call_id="c", rng_seed=7, refasr=engine, asr_threaded=threaded)
    po = Playout(clock, store, None, TH, run_id="r", call_id="c")
    caller.attach(po)
    for _ in range(agent_frames):
        po.enqueue(np.full(SPF, 3000, np.int16), 0)

    async def main():
        caller.on_provider_text("PROVIDER ONLY words")
        play = clock.task(po.run())

        async def send():
            async for _f in caller.frames():
                pass

        snd = clock.task(send())
        await clock.sleep_until_ns(until_ms * MS)
        caller.close()
        po.stop()
        play.cancel()
        snd.cancel()
        await clock.idle(asyncio.gather(play, snd, return_exceptions=True))
        await caller.finish_ear()

    asyncio.run(clock.run(main()))
    return caller, llm


def _agent_texts(transcript):
    return [t for who, t in transcript if who == "agent"]


def test_caller_hears_refasr_words_not_provider_text():
    caller, llm = _call(FakeASR(EAR_WORDS))
    assert _agent_texts(caller.transcript) == ["hello there"]
    assert caller.provider_transcript == ["PROVIDER ONLY words"]
    assert caller.refasr_text == "hello there" and caller.provider_text == "PROVIDER ONLY words"
    assert [(w.text, w.start_ms, w.end_ms) for w in caller.refasr_words] == \
        [("hello", 0, 300), ("there", 400, 800)]
    assert llm.contexts, "the caller never took the floor"
    for ctx in llm.contexts:                     # the provider text never reaches the LLM
        assert all("PROVIDER" not in t for _w, t in ctx.transcript)
    spoken = [ctx for ctx in llm.contexts if ctx.caller_turns == 0]
    assert any(("agent", "hello there") in ctx.transcript for ctx in spoken)


def test_threaded_push_hears_the_same_words():
    inline, _ = _call(FakeASR(EAR_WORDS))
    threaded, _ = _call(FakeASR(EAR_WORDS), threaded=True)
    assert threaded.refasr_words == inline.refasr_words
    assert _agent_texts(threaded.transcript) == _agent_texts(inline.transcript)


class _BatchASR:
    """Batch-only engine: BufferedStreamer, one word per transcribed buffer."""
    name, version = "batch", "0"

    def __init__(self):
        self.calls = []

    def transcribe(self, pcm):
        self.calls.append(len(pcm))
        return [Word("utterance", 0, len(pcm) * 1000 // 8000)] if np.any(pcm) else []

    def stream(self):
        return BufferedStreamer(self.transcribe)


def test_buffered_engine_is_finished_at_agent_stop():
    eng = _BatchASR()
    caller, llm = _call(eng)
    assert _agent_texts(caller.transcript)[:1] == ["utterance"]
    # heard at the agent stop (before the caller's first turn), not only at finish_ear
    assert any(("agent", "utterance") in ctx.transcript for ctx in llm.contexts)
    assert len(eng.calls) >= 2                   # stop restart + final finish


def test_no_refasr_keeps_old_behaviour():
    class P:
        def __init__(self):
            self.subs = []

        def subscribe(self, cb):
            self.subs.append(cb)

    voice = CallerVoice(FakeTTS(), "v1", 1.0, threaded=False)
    c = SimulatedCaller(_scenario(), CtxLLM(), voice, VirtualClock(), TH, None, run_id="r",
                        call_id="c", rng_seed=1)
    c.attach(P())                                # no subscribe_frames needed without refasr
    c.on_agent_text("direct")
    c.on_provider_text("prov")
    assert c.transcript == [("agent", "direct")] and c.provider_transcript == ["prov"]
    asyncio.run(c.finish_ear())                  # no-op


# -- 3. the mishearing observation ---------------------------------------------------------------
def _rec(events):
    sc = Scenario.model_validate(yaml.safe_load(FIX.read_text(encoding="utf-8")))
    return CallRecord(events=events, playout=np.zeros(0, PLAYOUT_DTYPE), overlays=[],
                      tool_calls=[], sim_turns=[], wav_path=Path("x.wav"), scenario=sc,
                      twin=None, call_id="c1", trial=0, condition="C0", system_id="mock"), sc


def _mh(ref, prov):
    return Event(10, 1, "mishearing", "harness",
                 {"refasr": "fake:0", "refasr_text": ref, "provider_text": prov})


def test_edit_distance_and_wer():
    assert edit_distance("the cat sat down".split(), "the cap sat".split()) == 2
    assert mishearing("the cat sat down", "the cap sat") == pytest.approx(0.5)
    assert mishearing("", "anything") is None


def test_mishearing_rate_observation():
    rec, sc = _rec([_mh("your booking is moved to friday", "your booking is moved to monday")])
    obs = [o for o in score_track_c(rec, sc, [], [], TH) if o.metric == "mishearing_rate"]
    assert len(obs) == 1
    o = obs[0]
    assert o.valid and o.value == pytest.approx(1 / 6)       # one substitution in six words
    assert "diagnostic" in o.tags and "domain:scheduling" in o.tags


def test_mishearing_rate_absent_without_provider_text_invalid_without_ref():
    rec, sc = _rec([_mh("hello there", "")])
    assert not [o for o in score_track_c(rec, sc, [], [], TH) if o.metric == "mishearing_rate"]
    rec, sc = _rec([])
    assert not [o for o in score_track_c(rec, sc, [], [], TH) if o.metric == "mishearing_rate"]
    rec, sc = _rec([_mh("", "the agent said this")])
    o = [o for o in score_track_c(rec, sc, [], [], TH) if o.metric == "mishearing_rate"]
    assert len(o) == 1 and not o[0].valid and "no_refasr_words" in o[0].tags
