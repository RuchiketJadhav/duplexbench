"""Reference-ASR calibration stage (BUILD_PLAN M4-T01; spec §1.4 D4).

fix-40: the gate is TTFS p90 (pipecat stt-benchmark). The scripts below put one word on
each utterance the speech-end detector (the playout segmenter's rule) finds in the clip,
ending at the utterance's RMS end, so a word released by the push that covers its end
has TTFS = that push's processing time."""
import asyncio
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from duplexbench.calibrate import (DEFAULT_REFASR_CLIP, CalibConfig, calibrate, format_table,
                                   refasr_result, refasr_stage)
from duplexbench.metrics.activity import frame_rms_dbfs, is_active, segment
from duplexbench.refasr.engine import FakeASR, Word
from duplexbench.thresholds import Thresholds

TH = Thresholds()
CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "refasr_calib_60s.wav"
NS = 1_000_000


def _utterances(path):
    """(onset_ms, end_ms) of every utterance the segmenter's rule finds in the clip."""
    x, _sr = sf.read(str(path), dtype="int16")
    act = np.array([is_active(frame_rms_dbfs(x[i:i + 160]), TH) for i in range(0, len(x), 160)])
    return [(o * TH.frame_ms, e * TH.frame_ms) for o, e in segment(act, TH)]


UTTS = _utterances(CLIP)
SCRIPT = [Word(f"w{i}", o, e) for i, (o, e) in enumerate(UTTS)]
SPEECH_WORDS = [Word(w["text"], w["start_ms"], w["end_ms"]) for w in json.loads(
    DEFAULT_REFASR_CLIP.with_suffix(".json").read_text(encoding="utf-8"))["words"]]


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
    assert r["n_words"] == len(SCRIPT) == r["n_utterances"] == r["n_ttfs"] == len(UTTS) > 10
    assert r["rtf"] == 0.05                     # 1 ms per 20 ms frame
    assert r["ttfs_ms"] == [1.0] * len(UTTS)    # released by the push covering the end
    assert r["ttfs_p50_ms"] == r["ttfs_p90_ms"] == 1.0 < TH.refasr_partial_p90_ms
    assert r["partial_p90_ms"] == 1.0           # disclosed, not gated
    assert r["finalize_supported"] is False and r["finalize_count"] == 0
    assert r["detector_hold_ms"] == TH.inactivity_run_ms


def test_slow_partials_refused():
    c = FakeClock()
    r = refasr_stage(TimedASR(c, per_frame_ms=0.5, per_word_ms=750), _clip(), TH, c)   # > 700 (B22)
    assert not r["passed"] and "TTFS p90" in r["reason"]
    assert r["ttfs_p90_ms"] >= 750 and r["partial_p90_ms"] >= 750
    assert r["rtf"] < TH.refasr_rtf_max          # refused on latency alone


def test_slow_rtf_refused():
    c = FakeClock()
    r = refasr_stage(TimedASR(c, per_frame_ms=15.0), _clip(), TH, c)
    assert not r["passed"] and "RTF" in r["reason"]
    assert abs(r["rtf"] - 0.75) < 1e-3
    # RTF < 1 means no backlog: each word is only one push (15 ms) late
    assert r["ttfs_p90_ms"] == 15.0 and r["partial_p90_ms"] == 15.0


def test_backlog_accumulates_beyond_real_time():
    c = FakeClock()
    r = refasr_stage(TimedASR(c, per_frame_ms=30.0), _clip(), TH, c)   # RTF 1.5
    assert not r["passed"] and r["ttfs_p90_ms"] > 20_000              # ~half of 60 s behind


def test_result_block_skipped_without_asr():
    r = refasr_result(None, None, TH)
    assert r["status"] == "skipped" and "M4-T01" in r["reason"]


def test_result_block_with_asr():
    c = FakeClock()
    r = refasr_result(TimedASR(c, per_frame_ms=1.0), CLIP, TH, clock=c)
    assert r["status"] == "pass" and r["version"] == "timed-fake:0"


def test_calibrate_wires_stage(tmp_path):
    # fix-41: the default clip is the 195 s, 42-sentence one; FakeASR over its word json
    assert DEFAULT_REFASR_CLIP.name == "refasr_calib_speech_3min.wav"
    cfg = CalibConfig.load(calls=2)
    res = asyncio.run(calibrate(cfg, virtual=True, th=TH, out_dir=tmp_path,
                                asr=FakeASR(SPEECH_WORDS)))      # default clip: the speech one
    ra = res["refasr"]
    assert ra["status"] == "pass", ra
    assert ra["n_utterances"] == ra["n_ttfs"] == 42           # one per sentence (800 ms pauses)
    assert ra["n_words"] == len(SPEECH_WORDS)
    assert ra["log_path"] is None and not list(tmp_path.glob("*.refasr_log.json"))   # pass: no log
    table = format_table(res)
    assert "refasr TTFS p90" in table and "partial p90 (not gated)" in table
    assert "refasr log" not in table


class LateASR:
    """Generic engine (no finalize, no messages) that releases every word only at finish."""
    name, version = "late-fake", "0"

    def transcribe(self, pcm):
        return list(SCRIPT)

    def stream(self):
        class S:
            def push(self, frame):
                return []

            def finish(self):
                return list(SCRIPT)
        return S()


def test_calibrate_fail_writes_refasr_log_beside_json(tmp_path):
    cfg = CalibConfig.load(calls=1)
    res = asyncio.run(calibrate(cfg, virtual=True, th=TH, out_dir=tmp_path, asr=LateASR(),
                                refasr_clip=CLIP))
    ra = res["refasr"]
    assert ra["status"] == "fail" and "TTFS p90" in ra["reason"]
    dest = Path(res["path"])
    log_p = Path(ra["log_path"])
    assert log_p == dest.with_name(dest.stem + ".refasr_log.json") and log_p.exists()
    assert json.loads(dest.read_text(encoding="utf-8"))["refasr"]["log_path"] == str(log_p)
    log = json.loads(log_p.read_text(encoding="utf-8"))
    assert log["written_because"] == "gate_fail" and log["reason"] == ra["reason"]
    assert log["messages"] == [] and "no stream.messages" in log["messages_note"]
    assert len(log["utterances"]) == ra["n_utterances"] == len(UTTS)
    u0 = log["utterances"][0]
    assert (u0["onset_ms"], u0["speech_end_ms"]) == UTTS[0]
    assert 60_000.0 <= u0["last_final_return_ms"] < 60_100.0      # released by finish()
    assert u0["ttfs_ms"] == round(u0["last_final_return_ms"] - UTTS[0][1], 2)
    assert u0["finalize_sent_ms"] is None and log["finalize_sent_ms"] == []
    assert [u["ttfs_ms"] for u in log["utterances"]] == ra["ttfs_ms"]
    assert f"refasr log: {log_p}" in format_table(res)


def test_calibrate_keep_refasr_log_on_pass(tmp_path):
    cfg = CalibConfig.load(calls=1)
    c = FakeClock()
    res = asyncio.run(calibrate(cfg, virtual=True, th=TH, out_dir=tmp_path,
                                asr=TimedASR(c, per_frame_ms=1.0), refasr_clip=CLIP,
                                keep_refasr_log=True))
    ra = res["refasr"]
    assert ra["status"] == "pass" and ra["log_path"]
    log = json.loads(Path(ra["log_path"]).read_text(encoding="utf-8"))
    assert log["written_because"] == "keep_refasr_log" and log["status"] == "pass"
