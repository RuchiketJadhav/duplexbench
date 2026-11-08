"""fix-24: provisional words from Deepgram interim Results, settled at the final.

* Word.final defaults to True (positional order unchanged); open_stream / settled.
* DeepgramStreamer(provisional=True) against the fake server on 127.0.0.1 (no internet):
  interim release, final settle, no duplicate finals, a text correction (interim -> final
  and interim -> interim), stream() / transcribe() unchanged (finals only).
* calibrate.first_release_latencies on hand-computed values.
* SimulatedCaller: the LLM transcript, refasr_words and refasr_text carry final words only.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from fake_deepgram_server import (FINAL_WORDS, FINALIZE_WORDS, INTERIM_WORD, KEY,  # noqa: E402
                                  FakeDeepgramServer)

from duplexbench.calibrate import first_release_latencies  # noqa: E402
from duplexbench.refasr.candidates import deepgram as dg  # noqa: E402
from duplexbench.refasr.engine import FakeASR, Word, open_stream, settled  # noqa: E402

FRAME = np.zeros(160, np.int16)
SCRIPT = [{"word": "hello", "start": 0.1, "end": 0.3, "confidence": 0.9},
          {"word": "there", "start": 0.4, "end": 0.6, "confidence": 0.9}]


def _w(d, final=True):
    return Word(d["word"], round(d["start"] * 1_000), round(d["end"] * 1_000),
                d["confidence"], final)


def _push_until(st, n_frames, done, wait_s=3.0):
    got = []
    for _ in range(n_frames):
        got += st.push(FRAME)
    deadline = time.perf_counter() + wait_s
    while not done(got) and time.perf_counter() < deadline:
        time.sleep(0.02)
        got += st.push(FRAME)
    return got


# -- Word / helpers ---------------------------------------------------------------------
def test_word_final_defaults_true_and_keeps_positional_order():
    w = Word("a", 1, 2, 0.5)
    assert w.final is True and w == Word("a", 1, 2, 0.5, True)
    assert Word("a", 1, 2, 0.5, False) != w
    assert settled([w, Word("b", 3, 4, 1.0, False)]) == [w]


def test_open_stream_passes_provisional_only_when_supported():
    fake = FakeASR([Word("x", 0, 10)])
    assert open_stream(fake, provisional=True).push(np.zeros(160, np.int16)) == [Word("x", 0, 10)]
    a = dg.DeepgramASR(api_key="k")
    assert open_stream(a, provisional=True).provisional is True
    assert open_stream(a).provisional is False and a.stream().provisional is False


# -- Deepgram streamer -------------------------------------------------------------------
def test_interim_released_as_provisional_then_settled():
    with FakeDeepgramServer() as s:                # default replies: interim "helo", then final
        st = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0).stream(provisional=True)
        got = _push_until(st, 15, lambda g: len(g) >= 1)          # 4800 bytes: interim only
        assert got == [_w(INTERIM_WORD, final=False)]              # "helo", provisional
        got = _push_until(st, 15, lambda g: len(g) >= 2)          # past 9600 bytes: final
        assert got == [_w(d) for d in FINAL_WORDS]                 # corrected "hello", final
        rest = st.finish()
    assert rest == [_w(d) for d in FINALIZE_WORDS]
    finals = [w.text for w in settled(got + rest)]
    assert finals == ["hello", "there", "order"]                  # each final exactly once


def test_interims_cumulative_correction_and_no_duplicate_finals():
    with FakeDeepgramServer(script=SCRIPT, chunk_ms=1_000, interim_ms=100,
                            misheard={"hello": "helo"}) as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0).stream(provisional=True)
        got = _push_until(st, 75, lambda g: any(w.final for w in g))   # 1.5 s of audio
        got += st.finish()
        sent = s.sent[0]
    n_interims = sum(1 for m in sent if not m["is_final"])
    assert n_interims >= 5                         # "hello"/"there" repeated in later interims
    prov = [w.text for w in got if not w.final]
    # "helo" first time; "hello" again only because its text changed; "there" once;
    # later interims repeating unchanged words release nothing
    assert prov == ["helo", "hello", "there"]
    assert [w for w in got if w.final] == [_w(d) for d in SCRIPT]
    i_final = next(i for i, w in enumerate(got) if w.final)
    assert all(not w.final for w in got[:i_final])                 # provisional came first


def test_stream_and_transcribe_without_provisional_are_finals_only():
    with FakeDeepgramServer(script=SCRIPT, chunk_ms=1_000, interim_ms=100) as s:
        a = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0)
        words = a.transcribe(np.zeros(8_000 * 3 // 2, np.int16))
        assert any(not m["is_final"] for m in s.sent[0])           # interims were sent
    assert words == [_w(d) for d in SCRIPT] and all(w.final for w in words)


def test_script_mode_without_interims_sends_none():
    with FakeDeepgramServer(script=SCRIPT, chunk_ms=1_000) as s:
        a = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0)
        st = a.stream(provisional=True)
        got = [w for _ in range(75) for w in st.push(FRAME)]
        got += st.finish()
        assert all(m["is_final"] for m in s.sent[0])
    assert all(w.final for w in got) and [w.text for w in got] == ["hello", "there"]


# -- calibrate latency over provisional + final ------------------------------------------
def test_first_release_latencies_hand_computed():
    rel = [(150.0, Word("helo", 100, 300, 0.4, False)),     # first release of word 1
           (420.0, Word("hello", 100, 310, 0.5, False)),    # correction: not counted again
           (700.0, Word("there", 400, 620, 0.9, False)),
           (3000.0, Word("hello", 100, 350)),               # finals at the chunk end
           (3000.0, Word("there", 400, 620)),
           (3000.0, Word("gone", 900, 1000, 0.3, False)),   # overlaps no final: ignored
           (3500.0, Word("order", 1200, 1500))]             # final with no provisional
    assert first_release_latencies(rel) == [150.0 - 350, 700.0 - 620, 3500.0 - 1500]
    # adjacent (touching) words do not borrow each other's release time
    rel = [(10.0, Word("a", 0, 100, 1.0, False)), (900.0, Word("b", 100, 200))]
    assert first_release_latencies(rel) == [700.0]
    # finals only: identical to the pre-fix-24 rule (return time - end_ms)
    rel = [(500.0, Word("a", 0, 100)), (800.0, Word("b", 100, 200))]
    assert first_release_latencies(rel) == [400.0, 600.0]


# -- the simulated caller ----------------------------------------------------------------
class _ProvASR:
    """FakeASR whose script carries provisional words; records the stream() keyword."""
    name, version = "prov-fake", "0"

    def __init__(self, script):
        self.inner, self.kw = FakeASR(script), []

    def transcribe(self, pcm):
        return settled(self.inner.transcribe(pcm))

    def stream(self, provisional=False):
        self.kw.append(provisional)
        return self.inner.stream()


@pytest.mark.parametrize("threaded", [False, True])
def test_simulated_caller_transcript_uses_final_words_only(threaded):
    from test_sim_hears_refasr import _agent_texts, _call
    eng = _ProvASR([Word("helo", 0, 250, 0.4, False), Word("hello", 0, 300),
                    Word("thar", 400, 700, 0.4, False), Word("there", 400, 800)])
    caller, llm = _call(eng, threaded=threaded)
    assert eng.kw and all(eng.kw)                  # the ear stream asked for provisional words
    assert _agent_texts(caller.transcript) == ["hello there"]
    assert caller.refasr_text == "hello there"
    assert all(w.final for w in caller.refasr_words)
    assert [(w.text, w.final) for w in caller.refasr_provisional] == \
        [("helo", False), ("thar", False)]
    for ctx in llm.contexts:                       # provisional text never reaches the LLM
        assert all("helo" not in t and "thar" not in t for _w, t in ctx.transcript)
