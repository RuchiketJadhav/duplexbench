"""Reference-ASR floor per entity type (BUILD_PLAN M5-T04; spec §4.3)."""
import numpy as np
import pytest

from duplexbench.channel.chain import ChannelChain, load_condition
from duplexbench.refasr.engine import FakeASR, Word
from duplexbench.refasr.floor_calibration import MINI_SET, floor_mini, floors, through_channel
from duplexbench.stimuli.fake_tts import FakeTTS
from duplexbench.thresholds import Thresholds

TH = Thresholds()


class ScriptedASR:
    """One FakeASR per clip, in order; records the audio it was given."""
    name, version = "scripted", "0"

    def __init__(self, texts):
        self.engines = [FakeASR([Word(w, i * 300, i * 300 + 250) for i, w in enumerate(t.split())])
                        for t in texts]
        self.seen = []

    def transcribe(self, pcm8k):
        self.seen.append(pcm8k)
        return self.engines[len(self.seen) - 1].transcribe(pcm8k)

    def stream(self):
        raise NotImplementedError


@pytest.fixture(scope="module")
def items():
    return floor_mini(FakeTTS())


def test_floor_mini_shape(items):
    assert len(items) == 20
    assert sorted({t for t, _, _ in items}) == ["amount", "dob", "email", "phone", "zip"]
    assert all(sum(t == k for t, _, _ in items) == 4 for k in ("phone", "email", "zip", "dob", "amount"))
    assert all(p.dtype == np.int16 and len(p) > 0 for _, _, p in items)


def test_floors_18_of_20(items):
    texts = [spoken for _, _, spoken in MINI_SET]
    texts[0] = "four one five five five five zero one zero nine"      # phone wrong
    texts[16] = "forty two dollars"                                      # amount wrong
    asr = ScriptedASR(texts)
    f = floors(asr, items, load_condition("C0", TH), TH)
    assert f == {"phone": 0.75, "email": 1.0, "zip": 1.0, "dob": 1.0, "amount": 0.75}
    assert len(asr.seen) == 20
    assert sum(f.values()) / len(f) == pytest.approx(18 / 20)


def test_audio_went_through_the_channel(items):
    asr = ScriptedASR([s for _, _, s in MINI_SET])
    floors(asr, items[:1], load_condition("C0", TH), TH)
    heard, src = asr.seen[0], items[0][2]
    assert len(heard) >= len(src) and not np.array_equal(heard[:len(src)], src)   # codec + band limit


def test_callable_chain_factory(items):
    cond = load_condition("C0", TH)
    asr = ScriptedASR([s for _, _, s in MINI_SET[:4]])
    f = floors(asr, items[:4], lambda: ChannelChain(cond, 3, 4), TH)
    assert f == {"phone": 1.0}


def test_through_channel_length():
    ch = ChannelChain(load_condition("C0", TH), 0, 1)
    out = through_channel(ch, np.zeros(8000, np.int16))
    assert len(out) % ch.frame_len == 0 and len(out) >= 8000
