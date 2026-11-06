"""M1-T07: channel conditions and the two-direction chain (spec §5)."""
from __future__ import annotations

import numpy as np
import pytest

from duplexbench.channel import mulaw
from duplexbench.channel.chain import ChannelChain, Condition, load_condition
from duplexbench.metrics.activity import frame_rms_dbfs
from duplexbench.thresholds import Thresholds

TH = Thresholds()


def _tone(n: int, rate: int = 8000, dbfs: float = -20.0, hz: float = 660.0) -> np.ndarray:
    amp = 32768 * 10 ** (dbfs / 20) * np.sqrt(2)
    return np.rint(amp * np.sin(2 * np.pi * hz * np.arange(n) / rate)).astype(np.int16)


class _ForcedLoss:
    def __init__(self, lost: set[int]):
        self.lost, self.i = lost, -1

    def next(self) -> bool:
        self.i += 1
        return self.i in self.lost


def test_conditions_take_every_value_from_thresholds():
    c0, c1, w = (load_condition(n, TH) for n in ("C0", "C1", "W"))
    assert (c0.rate, c0.codec, c0.noise, c0.loss, c0.muffle, c0.bursts) == (8000, True, False, False, False, False)
    assert (w.rate, w.codec, w.noise, w.loss) == (16000, False, False, False)
    assert c1.noise_snr_db == TH.noise_snr_db and c1.noise_drift_db == TH.noise_drift_db
    assert (c1.ge_p, c1.ge_r, c1.ge_loss_in_bad) == (TH.ge_p, TH.ge_r, TH.ge_loss_in_bad)
    assert c1.burst_len_ms == tuple(TH.burst_len_ms) and c1.muffle_share == TH.muffle_share
    other = load_condition("C1", Thresholds(noise_snr_db=10, muffle_share=0.5))
    assert other.noise_snr_db == 10 and other.muffle_share == 0.5
    assert str(c1) == "C1" and isinstance(c1, Condition)


def test_c0_is_mulaw_round_trip_only():
    ch = ChannelChain(load_condition("C0", TH), 11, 12)
    assert ch.delay_frames == 0 and ch.delivery(True) == "mulaw" and ch.delivery(False) == "pcm16@8000"
    x = _tone(160 * 20)
    out = [f for k in range(20) for f in ch.forward(x[k * 160:(k + 1) * 160], 8000, [True])]
    assert len(out) == 20
    for k, f in enumerate(out):
        assert f == mulaw.encode(x[k * 160:(k + 1) * 160])


def test_w_yields_16k_pcm_frames_without_codec():
    ch = ChannelChain(load_condition("W", TH), 11, 12)
    assert ch.delivery(True) == "pcm16@16000" and ch.frame_len == 320
    x = _tone(320 * 5, rate=16000)
    out = [f for k in range(5) for f in ch.forward(x[k * 320:(k + 1) * 320], 16000, [True])]
    assert [len(f) for f in out] == [640] * 5
    assert np.array_equal(np.frombuffer(b"".join(out), np.int16), x)
    back = list(ch.back(x, 16000))
    assert all(len(b) == 320 for b in back) and np.array_equal(np.concatenate(back), x)


def test_c1_loss_repeats_previous_frame_and_noise_is_before_loss():
    ch = ChannelChain(load_condition("C1", TH), 11, 12)
    ch.fwd_ge = _ForcedLoss({10, 11, 12})
    assert ch.delay_frames == 2
    speech, silence = _tone(160), np.zeros(160, np.int16)
    out = []
    for k in range(30):
        x, m = (speech, True) if k < 8 else (silence, False)
        out += list(ch.forward(x, 8000, [m]))
    assert out[10] == out[9] and out[11] == out[9] and out[12] == out[9]   # concealment = repeat
    assert ch.fwd_lost == [10, 11, 12]
    for k in range(13, len(out)):                                         # input silence + noise bed
        assert out[k] != out[9]
        assert frame_rms_dbfs(mulaw.decode(out[k])) > -60


def test_c1_forward_is_sample_aligned_and_held_back_whole_frames():
    ch = ChannelChain(load_condition("C1", Thresholds(muffle_share=0.0)), 11, 12)
    ch.fwd_ge = _ForcedLoss(set())
    ch.noise = ch.bursts = None
    imp = np.zeros(160, np.int16)
    imp[80] = 20000
    outs = []
    for k in range(6):
        outs += list(ch.forward(imp if k == 0 else np.zeros(160, np.int16), 8000, [k == 0]))
    assert ch.delay_frames == 2 and len(outs) == 6 - ch.delay_frames    # held back 2 frames
    peaks = [int(np.abs(mulaw.decode(f).astype(int)).max()) for f in outs]
    assert int(np.argmax(peaks)) == 0                                   # sample-aligned:
    assert int(np.argmax(np.abs(mulaw.decode(outs[0]).astype(int)))) == 80   # same sample index


def test_muffle_decisions_follow_utterances():
    ch = ChannelChain(load_condition("C1", Thresholds(muffle_share=1.0)), 11, 12)
    ch.fwd_ge = _ForcedLoss(set())
    for k in range(20):
        list(ch.forward(_tone(160), 8000, [k % 5 < 3]))                  # 4 utterances
    assert ch.muffled_utterances == [True] * 4


def test_forward_and_back_use_independent_seeds():
    c1 = load_condition("C1", TH)
    ch = ChannelChain(c1, 11, 12)
    fwd = [ch.fwd_ge.next() for _ in range(30_000)]
    back = [ch.back_ge.next() for _ in range(30_000)]
    assert fwd != back and sum(fwd) > 0 and sum(back) > 0
    same = ChannelChain(c1, 11, 11)
    assert [same.fwd_ge.next() for _ in range(30_000)] != [same.back_ge.next() for _ in range(30_000)]


def test_back_resamples_to_line_and_decodes():
    ch = ChannelChain(load_condition("C0", TH), 11, 12)
    x = _tone(24_000, rate=24_000)                                          # 1 s at 24 kHz
    frames = list(ch.back(x, 24_000))
    assert all(len(f) == 160 and f.dtype == np.int16 for f in frames)
    assert 48 <= len(frames) <= 50                                         # hold-back < 1 frame
    assert ch.back_delay_ms(24_000) > 0 and ch.back_delay_ms(8000) == 0.0
    mid = np.concatenate(frames[10:40])
    assert abs(frame_rms_dbfs(mid) - (-20.0)) < 1.0


def test_back_loss_conceals_with_previous_decoded_frame():
    ch = ChannelChain(load_condition("C1", TH), 11, 12)
    ch.back_ge = _ForcedLoss({3})
    frames = [_tone(160, hz=440 + 100 * k) for k in range(6)]
    out = [f for fr in frames for f in ch.back(fr, 8000)]
    assert np.array_equal(out[3], out[2]) and ch.back_lost == [3]


@pytest.mark.parametrize("name", ["C0", "C1", "W"])
def test_condition_yaml_exists(name):
    assert load_condition(name, TH).name == name
