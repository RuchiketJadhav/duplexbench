"""fix-41: the 195 s default calibration clip, the reference-ASR diagnostics log and the
finish() timeout message, on the fake Deepgram server (127.0.0.1, no internet, $0).

* DEFAULT_REFASR_CLIP is the 3min clip and the segmenter finds its 42 sentences.
* A gate-failing refasr stage writes `<stem>.refasr_log.json` with every server message
  (each with `_arrival_ms`), stop and Finalize times, per-utterance TTFS with onset/end and
  the return time of the last final word, and the reason; the API key never lands in it.
* A passing stage writes no log unless keep_log; a transport error (finish timeout) is a
  failed result with the log written.
* The finish() timeout message names the last server message type and its age, or says
  that none arrived.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from fake_deepgram_server import KEY, FakeDeepgramServer  # noqa: E402

from duplexbench.calibrate import (DEFAULT_REFASR_CLIP, REFASR_LOG_SCHEMA,  # noqa: E402
                                   refasr_log_path, refasr_result, write_refasr_log)
from duplexbench.metrics.activity import frame_rms_dbfs, is_active, segment  # noqa: E402
from duplexbench.refasr.candidates import deepgram as dg  # noqa: E402
from duplexbench.thresholds import Thresholds  # noqa: E402

TH = Thresholds()
TH200 = Thresholds(inactivity_run_ms=200)          # test-only: pipecat's stop_secs 0.2
TH_STRICT = Thresholds(refasr_partial_p90_ms=300)  # test-only: Finalize-only TTFS ~520 fails
FRAME = np.zeros(160, np.int16)
UTTS = [(100, 700), (1500, 2100), (2900, 3500)]
CLIP_MS = 4_200
SCRIPT = [w for o, e in UTTS for w in (
    {"word": f"a{o}", "start": o / 1e3, "end": (o + 280) / 1e3, "confidence": 0.9},
    {"word": f"b{o}", "start": (o + 300) / 1e3, "end": e / 1e3, "confidence": 0.9})]


def _clip(tmp_path) -> Path:
    x = np.zeros(8 * CLIP_MS, np.int16)
    for o, e in UTTS:
        x[8 * o:8 * e] = 3000
    p = tmp_path / "clip.wav"
    sf.write(str(p), x, 8000, subtype="PCM_16")
    return p


def _run(tmp_path, th, *, keep_log=False, finish_timeout_s=3.0, **srv):
    clip = _clip(tmp_path)
    log_p = refasr_log_path(tmp_path / "calibration-X.json")
    with FakeDeepgramServer(script=SCRIPT, delay_ms=10, chunk_ms=None, **srv) as s:
        asr = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=finish_timeout_s)
        res = refasr_result(asr, clip, th, log_path=log_p, keep_log=keep_log)
    return res, log_p


# -- the default clip ------------------------------------------------------------------------
def test_default_clip_is_the_3min_one():
    assert DEFAULT_REFASR_CLIP.name == "refasr_calib_speech_3min.wav"
    info = sf.info(str(DEFAULT_REFASR_CLIP))
    assert info.samplerate == 8000 and info.channels == 1 and abs(info.duration - 195) < 1e-6
    meta = json.loads(DEFAULT_REFASR_CLIP.with_suffix(".json").read_text(encoding="utf-8"))
    assert len(meta["utterances"]) == 42
    x, _ = sf.read(str(DEFAULT_REFASR_CLIP), dtype="int16")
    act = np.array([is_active(frame_rms_dbfs(x[i:i + 160]), TH) for i in range(0, len(x), 160)])
    assert len(segment(act, TH)) == 42
    assert (DEFAULT_REFASR_CLIP.parent / "refasr_calib_speech_60s.wav").exists()   # kept


def test_log_path_is_beside_the_calibration_json():
    p = refasr_log_path(Path("results") / "calibration-20261108T000000Z.json")
    assert p == Path("results") / "calibration-20261108T000000Z.refasr_log.json"


# -- the log on a gate fail ------------------------------------------------------------------
def test_gate_fail_writes_log_with_messages_times_and_no_key(tmp_path):
    res, log_p = _run(tmp_path, TH_STRICT)
    assert res["status"] == "fail" and "TTFS p90" in res["reason"]
    assert res["log_path"] == str(log_p) and log_p.exists()
    text = log_p.read_text(encoding="utf-8")
    assert KEY not in text
    log = json.loads(text)
    assert log["schema"] == REFASR_LOG_SCHEMA and log["written_because"] == "gate_fail"
    assert log["reason"] == res["reason"] and log["transport_error"] is None
    assert log["paced"] is True and log["detector_hold_ms"] == TH.inactivity_run_ms
    assert isinstance(log["stream_t0_offset_ms"], float) and log["messages_note"] is None
    # every server message, in order, each with its arrival time
    msgs = log["messages"]
    assert msgs and all(isinstance(m["_arrival_ms"], float) for m in msgs)
    assert [m["_arrival_ms"] for m in msgs] == sorted(m["_arrival_ms"] for m in msgs)
    assert sum(bool(m.get("from_finalize")) for m in msgs) == 3 + 1     # 3 stops + finish
    assert msgs[-1]["type"] == "Metadata"
    # stops 500 ms after each speech end; Finalize right after each stop
    assert len(log["stop_ms"]) == len(log["finalize_sent_ms"]) == 3
    assert log["finish_called_ms"] >= CLIP_MS
    assert len(log["utterances"]) == 3
    for u, (o, e) in zip(log["utterances"], UTTS):
        assert (u["onset_ms"], u["speech_end_ms"]) == (o, e)
        assert e + 480 <= u["stop_ms"] <= u["finalize_sent_ms"] < e + 650
        assert u["n_final_words"] == 2 and u["last_final_word"]["text"] == f"b{o}"
        assert u["last_final_return_ms"] > u["finalize_sent_ms"]
        assert u["ttfs_ms"] == round(u["last_final_return_ms"] - e, 2)
    assert [u["ttfs_ms"] for u in log["utterances"]] == res["ttfs_ms"]


def test_log_scrubs_the_key(tmp_path):
    asr = dg.DeepgramASR(api_key=KEY, url="ws://127.0.0.1:1/v1/listen")
    p = write_refasr_log(tmp_path / "x.refasr_log.json",
                         {"reason": f"boom Token {KEY}", "messages": [{"note": KEY}]}, asr)
    text = p.read_text(encoding="utf-8")
    assert KEY not in text and text.count("***") == 2


# -- no log on a pass unless asked -----------------------------------------------------------
def test_pass_writes_no_log_unless_keep_log(tmp_path):
    res, log_p = _run(tmp_path, TH200)
    assert res["status"] == "pass", res
    assert res["log_path"] is None and not log_p.exists()
    res, log_p = _run(tmp_path, TH200, keep_log=True)
    assert res["status"] == "pass" and res["log_path"] == str(log_p)
    log = json.loads(log_p.read_text(encoding="utf-8"))
    assert log["written_because"] == "keep_refasr_log" and log["status"] == "pass"


# -- transport error: finish timeout ---------------------------------------------------------
def test_finish_timeout_in_stage_is_a_failed_result_with_log(tmp_path):
    # silent after 2 messages: the replies to the first two Finalize, then nothing
    res, log_p = _run(tmp_path, TH200, finish_timeout_s=0.5, silent_after=2)
    assert res["status"] == "fail" and res["transport_error"]
    assert res["reason"].startswith("transport error: deepgram finish timeout")
    assert "last message Results from_finalize" in res["reason"]
    log = json.loads(log_p.read_text(encoding="utf-8"))
    assert log["written_because"] == "transport_error"
    assert log["transport_error"] == res["transport_error"] and len(log["messages"]) == 2
    assert res["n_utterances"] == 3 and len(log["finalize_sent_ms"]) == 3


def _push(st, n):
    for _ in range(n):
        st.push(FRAME)


def test_finish_timeout_names_last_message_and_its_age():
    with FakeDeepgramServer(silent_after=1, final_at=10**9) as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=0.3).stream()
        _push(st, 20)                       # 6 400 bytes: the interim (at 3 200) is sent
        deadline = time.perf_counter() + 3.0
        while not st.messages and time.perf_counter() < deadline:
            time.sleep(0.02)
        assert len(st.messages) == 1
        with pytest.raises(dg.RefASRTransportError,
                           match=r"timeout: .* last message Results interim (\d+) ms ago "
                                 r"\(1 messages received\)") as e:
            st.finish()
    import re
    ago = int(re.search(r"interim (\d+) ms ago", str(e.value)).group(1))
    assert ago >= 300                       # at least the finish wait
    assert KEY not in str(e.value)


def test_finish_timeout_when_the_server_never_answered():
    with FakeDeepgramServer(silent_after=0) as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=0.3).stream()
        _push(st, 30)
        with pytest.raises(dg.RefASRTransportError,
                           match="no server message received on this stream"):
            st.finish()
        assert s.n_sent == [0]
