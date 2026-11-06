"""Reference-ASR calibration stage (BUILD_PLAN M4-T01; spec §1.4 D4)."""
import asyncio
from pathlib import Path

import soundfile as sf

from duplexbench.calibrate import CalibConfig, calibrate, format_table, refasr_result, refasr_stage
from duplexbench.refasr.engine import FakeASR, Word
from duplexbench.thresholds import Thresholds

TH = Thresholds()
CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "refasr_calib_60s.wav"
NS = 1_000_000
SCRIPT = [Word(f"w{i}", i * 1000 + 200, i * 1000 + 600) for i in range(59)]


class FakeClock:
    def __init__(self):
        self.t = 0

    def now_ns(self):
        return self.t


class TimedASR:
    """FakeASR whose push costs `per_frame_ms`, plus `per_word_ms` when it releases words."""
    name, version = "timed-fake", "0"

    def __init__(self, clock, per_frame_ms=0.0, per_word_ms=0.0):
        self.inner, self.clock = FakeASR(SCRIPT), clock
        self.per_frame_ms, self.per_word_ms = per_frame_ms, per_word_ms

    def transcribe(self, pcm):
        return self.inner.transcribe(pcm)

    def stream(self):
        st, outer = self.inner.stream(), self

        class S:
            def push(self, frame):
                out = st.push(frame)
                outer.clock.t += int((outer.per_frame_ms + outer.per_word_ms * bool(out)) * NS)
                return out

            def finish(self):
                return st.finish()
        return S()


def _clip():
    pcm, sr = sf.read(str(CLIP), dtype="int16")
    return pcm


def test_fixture_shape():
    info = sf.info(str(CLIP))
    assert info.samplerate == 8000 and info.channels == 1 and info.subtype == "PCM_16"
    assert abs(info.duration - 60) < 1e-6 and CLIP.stat().st_size < 1_000_000


def test_fast_engine_passes():
    c = FakeClock()
    r = refasr_stage(TimedASR(c, per_frame_ms=1.0), _clip(), TH, c)
    assert r["passed"], r
    assert r["n_words"] == len(SCRIPT)
    assert r["rtf"] == 0.05                     # 1 ms per 20 ms frame
    assert r["partial_p90_ms"] < TH.refasr_partial_p90_ms


def test_slow_partials_refused():
    c = FakeClock()
    r = refasr_stage(TimedASR(c, per_frame_ms=0.5, per_word_ms=350), _clip(), TH, c)
    assert not r["passed"] and "partial p90" in r["reason"]
    assert r["partial_p90_ms"] >= 350
    assert r["rtf"] < TH.refasr_rtf_max          # refused on latency alone


def test_slow_rtf_refused():
    c = FakeClock()
    r = refasr_stage(TimedASR(c, per_frame_ms=15.0), _clip(), TH, c)
    assert not r["passed"] and "RTF" in r["reason"]
    assert abs(r["rtf"] - 0.75) < 1e-3
    # RTF < 1 means no backlog: each word is only one push (15 ms) late
    assert r["partial_p90_ms"] == 15.0


def test_backlog_accumulates_beyond_real_time():
    c = FakeClock()
    r = refasr_stage(TimedASR(c, per_frame_ms=30.0), _clip(), TH, c)   # RTF 1.5
    assert not r["passed"] and r["partial_p90_ms"] > 20_000           # ~half of 60 s behind


def test_result_block_skipped_without_asr():
    r = refasr_result(None, None, TH)
    assert r["status"] == "skipped" and "M4-T01" in r["reason"]


def test_result_block_with_asr():
    c = FakeClock()
    r = refasr_result(TimedASR(c, per_frame_ms=1.0), CLIP, TH, clock=c)
    assert r["status"] == "pass" and r["version"] == "timed-fake:0"


def test_calibrate_wires_stage(tmp_path):
    cfg = CalibConfig.load(calls=2)
    res = asyncio.run(calibrate(cfg, virtual=True, th=TH, out_dir=tmp_path, asr=FakeASR(SCRIPT)))
    assert res["refasr"]["status"] == "pass"
    assert "refasr partial p90" in format_table(res)
