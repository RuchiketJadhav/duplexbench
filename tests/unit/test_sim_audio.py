"""M4-T09: simulated-caller voice (TTS -> 8 kHz, frames, speculative cache)."""
import asyncio

import numpy as np

from duplexbench.simulator.audio import (FRAME_SAMPLES, CallerVoice, resample_to_line, to_frames,
                                         transcript_hash)
from duplexbench.stimuli.fake_tts import FakeTTS


def _voice(**kw):
    return CallerVoice(FakeTTS(), "v1", 1.0, threaded=False, **kw)


def test_synth_is_8k_int16_and_length_matches():
    tts = FakeTTS()
    src, sr, _ = tts.synthesize("hello there", "v1", 1.0)
    pcm = asyncio.run(_voice().synth("hello there"))
    assert pcm.dtype == np.int16
    assert sr == 24000
    # length = source duration at 8 kHz, minus trimmed digital silence at the edges
    assert abs(len(pcm) - len(src) // 3) <= 24
    assert pcm[0] != 0 and pcm[-1] != 0


def test_resample_passthrough_and_delay_compensated():
    x = (np.sin(np.arange(2400) / 10) * 8000).astype(np.int16)
    assert np.array_equal(resample_to_line(x, 8000), x)
    y = resample_to_line(np.concatenate([x, x]), 16000)
    assert len(y) == 2400


def test_synth_utterance_keeps_word_times():
    utt = asyncio.run(_voice().synth_utterance("one two three"))
    assert [w.text for w in utt.words] == ["one", "two", "three"]
    assert utt.words[0].start_ms == 0
    assert utt.words[-1].end_ms <= len(utt.pcm) * 1000 // 8000 + 1
    assert '"text":"two"' in utt.words_json()


def test_to_frames_pads_tail():
    fr = to_frames(np.ones(161, np.int16))
    assert len(fr) == 2 and all(len(f) == FRAME_SAMPLES for f in fr)
    assert fr[1][0] == 1 and not fr[1][1:].any()
    assert to_frames(np.zeros(0, np.int16)) == []
    assert len(to_frames(np.ones(320, np.int16))) == 2


def test_speculative_cache_hit_and_miss():
    async def main():
        v = _voice()
        k1 = ("ph", transcript_hash([("agent", "hi")]))
        k2 = ("ph", transcript_hash([("agent", "hi"), ("agent", "more")]))
        v.prepare(k1, "yes please")
        hit = await v.take(k1)
        v.prepare(k1, "stale reply")
        miss = await v.take(k2)
        return v, hit, miss

    v, hit, miss = asyncio.run(main())
    assert hit is not None and hit.text == "yes please" and len(hit.pcm) > 0
    assert miss is None
    assert v.spec_hits == 1 and v.spec_discards == 1
    assert not v.has(("ph", "x"))


def test_transcript_hash_stable_and_order_sensitive():
    a = transcript_hash([("agent", "a"), ("caller", "b")])
    assert a == transcript_hash([("agent", "a"), ("caller", "b")])
    assert a != transcript_hash([("caller", "b"), ("agent", "a")])


def test_disk_cache_reused(tmp_path):
    tts = FakeTTS()
    v = CallerVoice(tts, "v1", 1.0, cache_dir=tmp_path, threaded=False)
    a = asyncio.run(v.synth("cache me"))
    b = asyncio.run(v.synth("cache me"))
    assert tts.calls == 1 and np.array_equal(a, b)


def test_threaded_synth_matches_inline():
    a = asyncio.run(CallerVoice(FakeTTS(), "v1", 1.0).synth("same text"))
    b = asyncio.run(_voice().synth("same text"))
    assert np.array_equal(a, b)
