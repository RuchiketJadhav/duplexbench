"""fix-40: Deepgram Finalize mid-stream (Dograh parity) and the TTFS calibration gate.

* DeepgramStreamer.finalize() against the fake server on 127.0.0.1 (no internet): a
  from_finalize final comes back through the normal final release, the stream stays
  open, finalize_count counts; finalize_if_supported leaves engines without it alone.
* calibrate.ttfs_per_utterance on hand-computed values.
* calibrate.refasr_stage measures TTFS on the fake server's script mode: Finalize at
  the detector's stop gives TTFS = hold + network delay; without Finalize the 1.5 s
  chunk finals show up as TTFS; with the default 500 ms hold the Finalize-only TTFS is
  >= 500 ms (the hold is inside TTFS, as pipecat's stop_secs is).
* SimulatedCaller calls finalize() once per agent stop on the ear.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from fake_deepgram_server import (FINAL_WORDS, FINALIZE_WORDS, KEY,  # noqa: E402
                                  FakeDeepgramServer)

from duplexbench.calibrate import MemStore, refasr_stage, ttfs_per_utterance  # noqa: E402
from duplexbench.clock import RealClock, VirtualClock  # noqa: E402
from duplexbench.playout import Playout  # noqa: E402
from duplexbench.refasr.candidates import deepgram as dg  # noqa: E402
from duplexbench.refasr.engine import FakeASR, Word, finalize_if_supported  # noqa: E402
from duplexbench.thresholds import Thresholds  # noqa: E402

FRAME = np.zeros(160, np.int16)
TH = Thresholds()
MS = 1_000_000


def _w(d):
    return Word(d["word"], round(d["start"] * 1_000), round(d["end"] * 1_000), d["confidence"])


def _push_until(st, n_frames, want, wait_s=3.0):
    got = []
    for _ in range(n_frames):
        got += st.push(FRAME)
    deadline = time.perf_counter() + wait_s
    while len(got) < want and time.perf_counter() < deadline:
        time.sleep(0.02)
        got += st.push(FRAME)
    return got


# -- DeepgramStreamer.finalize ------------------------------------------------------------
def test_endpointing_default_is_dograh_value():
    assert dg.DeepgramASR(api_key="k").endpointing == 100
    assert dg.DeepgramASR(api_key="k").query()["endpointing"] == "100"


def test_finalize_mid_stream_releases_from_finalize_final_and_stream_stays_open():
    with FakeDeepgramServer() as s:          # default replies: Finalize -> FINALIZE_WORDS
        st = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0).stream()
        got = _push_until(st, 5, 0)
        assert st.finalize() is None and st.finalize_count == 1
        got += _push_until(st, 1, len(FINALIZE_WORDS))
        assert got == [_w(d) for d in FINALIZE_WORDS] and all(w.final for w in got)
        assert any(m.get("from_finalize") for m in st.messages)
        # still open: audio keeps flowing, the chunk final arrives, a 2nd finalize works
        got = _push_until(st, 60, len(FINAL_WORDS))
        assert got == [_w(d) for d in FINAL_WORDS]
        st.finalize()
        assert st.finalize_count == 2
        got = _push_until(st, 1, len(FINALIZE_WORDS))
        assert got == [_w(d) for d in FINALIZE_WORDS]
        got = st.finish()                    # Finalize + CloseStream as before
        assert [m["type"] for m in s.texts[0]] == ["Finalize", "Finalize", "Finalize",
                                                    "CloseStream"]
    assert st.finalize_count == 2            # finish()'s own Finalize is not counted
    assert got == [_w(d) for d in FINALIZE_WORDS]
    with pytest.raises(RuntimeError, match="after finish"):
        st.finalize()


def test_finalize_before_any_push_sends_nothing():
    with FakeDeepgramServer() as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url).stream()
        st.finalize()
        assert st.finalize_count == 0 and st.finish() == []
        assert s.texts == []                 # no connection was opened


def test_script_mode_finalize_flushes_words_heard_so_far():
    script = [{"word": "a", "start": 0.1, "end": 0.3, "confidence": 0.9},
              {"word": "b", "start": 1.1, "end": 1.4, "confidence": 0.9}]
    with FakeDeepgramServer(script=script, chunk_ms=None) as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0).stream()
        got = _push_until(st, 25, 0)         # 500 ms: "a" is buffered, no chunk final
        assert got == []
        st.finalize()
        got = _push_until(st, 1, 1)
        assert [w.text for w in got] == ["a"] and got[0].final
        got = _push_until(st, 50, 0) + st.finish()     # "b" comes with finish()
        assert [w.text for w in got] == ["b"]
        fin = [m for m in s.sent[0] if m["from_finalize"]]
        assert len(fin) == 2 and all(m["is_final"] for m in fin)


def test_finalize_if_supported():
    fake = FakeASR([Word("x", 0, 10)]).stream()
    assert finalize_if_supported(fake) is False and finalize_if_supported(None) is False

    class S:
        n = 0

        def finalize(self):
            self.n += 1
    s = S()
    assert finalize_if_supported(s) is True and s.n == 1


# -- TTFS: hand-computed -------------------------------------------------------------------
def test_ttfs_per_utterance_hand_computed():
    onsets, ends = [100, 1500, 2900], [700, 2100, 3500]
    rel = [(400.0, Word("a", 100, 380, 1.0, False)),   # provisional: ignored
           (900.0, Word("a", 100, 380)),               # utt 0
           (950.0, Word("b", 400, 710)),               # utt 0, ends 10 ms past the RMS end
           (2050.0, Word("c", 1500, 1800)),            # utt 1, early chunk final
           (2400.0, Word("d", 1800, 2100)),            # utt 1 completes here
           (50.0, Word("pre", 0, 60))]                 # before the first onset: utt 0
    assert ttfs_per_utterance(rel, onsets, ends) == [950.0 - 700, 2400.0 - 2100, None]
    assert ttfs_per_utterance(rel, [], []) == []


# -- TTFS in calibrate on the fake server ---------------------------------------------------
UTTS = [(100, 700), (1500, 2100), (2900, 3500)]          # speech spans, ms
CLIP_MS = 4_200


def _clip():
    x = np.zeros(8 * CLIP_MS, np.int16)
    for o, e in UTTS:
        x[8 * o:8 * e] = 3000                             # -20.8 dBFS: active
    return x


SCRIPT = [w for o, e in UTTS for w in (
    {"word": f"a{o}", "start": o / 1e3, "end": (o + 280) / 1e3, "confidence": 0.9},
    {"word": f"b{o}", "start": (o + 300) / 1e3, "end": e / 1e3, "confidence": 0.9})]


class _NoFinalize:
    """The same Deepgram engine with finalize() hidden: an engine without Finalize."""

    def __init__(self, asr):
        self.asr, self.name, self.version, self.realtime_input = asr, "nofin", "0", True

    def stream(self, provisional=False):
        st = self.asr.stream(provisional=provisional)

        class S:
            push, finish = st.push, st.finish
        return S()


def _stage(th, *, finalize=True, **srv):
    with FakeDeepgramServer(script=SCRIPT, delay_ms=10, **srv) as s:
        asr = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=3.0)
        res = refasr_stage(asr if finalize else _NoFinalize(asr), _clip(), th, RealClock())
        n_fin = sum(m.get("type") == "Finalize" for m in s.texts[0])
    return res, n_fin


TH200 = Thresholds(inactivity_run_ms=200)    # test-only: pipecat's stop_secs 0.2


def test_ttfs_passes_when_finals_come_only_on_finalize():
    res, n_fin = _stage(TH200, chunk_ms=None)
    assert res["n_utterances"] == 3 and res["n_ttfs"] == 3 and res["n_words"] == 6
    assert res["finalize_supported"] and res["finalize_count"] == 3
    assert n_fin == 3 + 1                                   # 3 at the stops + finish()
    assert res["detector_hold_ms"] == 200
    # Finalize goes out 200 ms after each speech end; final = hold + 10 ms delay + grid
    assert all(200 <= v < TH.refasr_partial_p90_ms for v in res["ttfs_ms"]), res
    assert res["ttfs_p90_ms"] <= TH.refasr_partial_p90_ms == 700     # B22: sim_eot_ms
    assert res["passed"], res


def test_ttfs_without_finalize_shows_the_chunk_finals():
    with_fin, _ = _stage(TH200, chunk_ms=1_500)
    without, n_fin = _stage(TH200, finalize=False, chunk_ms=1_500)
    assert with_fin["passed"] and with_fin["ttfs_p90_ms"] < 300          # Finalize reply, not the chunk
    assert not without["finalize_supported"] and without["finalize_count"] == 0
    assert n_fin == 1                                       # finish() only
    assert without["n_ttfs"] == 3
    # utt 0 ends 700 -> chunk final at 1500; utt 1 ends 2100 -> 3000; utt 2 -> finish
    assert 780 <= without["ttfs_ms"][0] < 900 and 880 <= without["ttfs_ms"][1] < 1000
    assert without["ttfs_p90_ms"] > TH.refasr_partial_p90_ms and not without["passed"]
    assert "TTFS p90" in without["reason"]


def test_default_hold_is_inside_ttfs():
    res, _ = _stage(TH, chunk_ms=None)                     # inactivity_run_ms = 500
    assert res["detector_hold_ms"] == 500 and res["finalize_count"] == 3
    assert all(500 <= v < 600 for v in res["ttfs_ms"]), res   # hold + 10 ms delay + grid
    assert res["passed"], res                                 # B22: 700 leaves room for the hold


# -- the simulated caller ------------------------------------------------------------------
class _FinASR:
    """FakeASR whose streamer records finalize() calls (ear ms pushed at each call)."""
    name, version = "fin-fake", "0"

    def __init__(self, script):
        self.inner, self.calls = FakeASR(script), []

    def transcribe(self, pcm):
        return self.inner.transcribe(pcm)

    def stream(self, provisional=False):
        st, outer = self.inner.stream(), self

        class S:
            def push(self, frame):
                return st.push(frame)

            def finish(self):
                return st.finish()

            def finalize(self):
                outer.calls.append(st.elapsed_ms)
        return S()


def _caller_two_stops(engine, threaded):
    from test_sim_hears_refasr import SPF, CallerVoice, CtxLLM, FakeTTS, SimulatedCaller, \
        _scenario
    clock, store = VirtualClock(), MemStore()
    caller = SimulatedCaller(_scenario(), CtxLLM(), CallerVoice(FakeTTS(), "v1", 1.0,
                             threaded=False), clock, TH, store, run_id="r", call_id="c",
                             rng_seed=7, refasr=engine, asr_threaded=threaded)
    po = Playout(clock, store, None, TH, run_id="r", call_id="c")
    caller.attach(po)
    stops = []
    po.subscribe(lambda kind, idx, t: stops.append(idx) if kind == "stop" else None)
    for amp in (3000, 0, 3000):                  # 1 s speech, 1 s silence, 1 s speech
        for _ in range(50):
            po.enqueue(np.full(SPF, amp, np.int16), 0)

    async def main():
        play = clock.task(po.run())
        await clock.sleep_until_ns(5_000 * MS)
        caller.close()
        po.stop()
        play.cancel()
        await clock.idle(asyncio.gather(play, return_exceptions=True))
        await caller.finish_ear()

    asyncio.run(clock.run(main()))
    return caller, stops


@pytest.mark.parametrize("threaded", [False, True])
def test_caller_finalizes_once_per_agent_stop(threaded):
    eng = _FinASR([Word("hello", 0, 300), Word("again", 2100, 2500)])
    caller, stops = _caller_two_stops(eng, threaded)
    assert len(stops) == 2
    assert caller.finalize_count == 2 and len(eng.calls) == 2
    # each Finalize follows the frame on which the stop fired: speech end + 500 ms of ear
    ends_ms = [i * TH.frame_ms for i in stops]
    assert eng.calls == [e + TH.inactivity_run_ms for e in ends_ms]
    assert caller.refasr_text == "hello again"             # transcript: final words only


def test_caller_without_finalize_is_unchanged():
    caller, stops = _caller_two_stops(FakeASR([Word("hello", 0, 300)]), False)
    assert len(stops) == 2 and caller.finalize_count == 0
    assert caller.refasr_text == "hello"
