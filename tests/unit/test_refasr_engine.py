"""M4a-T01: reference-ASR engine interface and FakeASR."""
import numpy as np
import pytest

from duplexbench.refasr.engine import FakeASR, RefASR, Streamer, Word, text_of

SCRIPT = [Word("your", 100, 250), Word("slot", 260, 500), Word("is", 520, 600),
          Word("three", 620, 900), Word("pm", 910, 1180)]
FRAME = np.zeros(160, dtype=np.int16)          # 20 ms at 8 kHz


def test_fake_batch_returns_script():
    asr = FakeASR(SCRIPT)
    assert isinstance(asr, RefASR)
    assert asr.transcribe(np.zeros(8000, dtype=np.int16)) == SCRIPT
    assert text_of(asr.transcribe(FRAME)) == "your slot is three pm"


def test_streamer_releases_words_in_order_by_time():
    st = FakeASR(SCRIPT).stream()
    assert isinstance(st, Streamer)
    released: list[tuple[int, Word]] = []
    for i in range(1, 51):                     # 1.0 s of audio
        for w in st.push(FRAME):
            released.append((i * 20, w))
    assert [w for _, w in released] == SCRIPT[:4]      # "pm" ends at 1180 ms > 1000
    for t_ms, w in released:
        assert w.end_ms <= t_ms < w.end_ms + 20        # released on the covering frame
    assert st.finish() == [SCRIPT[4]]


def test_streamer_accepts_mulaw_bytes_and_empty_partials():
    st = FakeASR(SCRIPT).stream()
    assert st.push(b"\xff" * 160) == []                # 20 ms: nothing ends that early
    for _ in range(4):
        st.push(b"\xff" * 160)
    assert st.elapsed_ms == pytest.approx(100.0)


def test_push_after_finish_raises():
    st = FakeASR(SCRIPT).stream()
    st.finish()
    with pytest.raises(RuntimeError):
        st.push(FRAME)
