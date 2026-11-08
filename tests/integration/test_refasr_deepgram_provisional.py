"""fix-24: calibrate.refasr_stage through DeepgramASR on the fake server's chunked script mode
(127.0.0.1, no internet). With interim Results every 100 ms the partial p90 is well under the
300 ms gate; without interims the first words of each chunk wait for the chunk final and
the p90 is chunk-length, as measured live on 2026-11-07 (3990 ms).

fix-40: partial_p90_ms is now disclosure only; the gate is TTFS (test_refasr_finalize.py).
This clip is silence, so the speech-end detector finds no utterance and the stage fails
on "no speech end detected"; the partial p90 is still measured and reported."""
import numpy as np
from fake_deepgram_server import KEY, FakeDeepgramServer  # tests/integration is on sys.path

from duplexbench.calibrate import refasr_stage
from duplexbench.clock import RealClock
from duplexbench.refasr.candidates.deepgram import DeepgramASR
from duplexbench.thresholds import Thresholds

TH = Thresholds()
# 10 words, one every 300 ms from 0.1 s: ends 0.35, 0.65, ... 2.75 s
SCRIPT = [{"word": f"w{k}", "start": 0.1 + 0.3 * k, "end": 0.35 + 0.3 * k, "confidence": 0.9}
          for k in range(10)]
CLIP = np.zeros(8_000 * 3, np.int16)                 # 3 s, paced at real time


def _stage(**kw):
    with FakeDeepgramServer(script=SCRIPT, chunk_ms=1_500, **kw) as s:
        res = refasr_stage(DeepgramASR(api_key=KEY, url=s.url), CLIP, TH, RealClock())
        return res, s.sent[0]


def test_partial_p90_with_interims_under_the_gate():
    res, sent = _stage(interim_ms=100)
    assert any(not m["is_final"] for m in sent)
    assert res["n_words"] == len(SCRIPT)             # final words, each counted once
    assert res["partial_p90_ms"] is not None
    assert res["partial_p90_ms"] < 300 < TH.refasr_partial_p90_ms    # interims every 100 ms
    assert res["n_utterances"] == 0 and res["ttfs_p90_ms"] is None      # silent clip
    assert not res["passed"] and "no speech end detected" in res["reason"]
    assert "partial p90" not in res["reason"]                           # not gated


def test_partial_p90_without_interims_is_chunk_length():
    res, sent = _stage()
    assert all(m["is_final"] for m in sent)
    assert res["n_words"] == len(SCRIPT)
    # words ending at 0.35 s wait for the 1.5 s chunk final: p90 >= ~1 s
    assert res["partial_p90_ms"] > 1_000 > TH.refasr_partial_p90_ms
    assert not res["passed"] and "partial p90" not in res["reason"]     # not gated
