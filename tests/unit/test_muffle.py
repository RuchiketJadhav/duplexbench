import numpy as np

from duplexbench.channel.muffle import Muffle

RATE = 8000


def _utterances(n=200, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.normal(0, 3000, RATE // 2).astype(np.int16) for _ in range(n)]


def _energy_above(x, hz):
    spec = np.abs(np.fft.rfft(x.astype(float))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / RATE)
    return spec[f > hz].sum()


def test_share_of_200_utterances():
    m = Muffle(seed=4, share=0.20, cutoff_hz=1200, rate=RATE)
    m.apply(_utterances())
    assert 30 <= sum(m.muffled) <= 50


def test_muffled_loses_20db_above_2khz():
    m = Muffle(seed=4, share=0.20, cutoff_hz=1200, rate=RATE)
    utts = _utterances()
    out = m.apply(utts)
    checked = 0
    for u, o, flag in zip(utts, out, m.muffled):
        ratio_db = 10 * np.log10(_energy_above(o, 2000) / _energy_above(u, 2000))
        if flag:
            assert ratio_db <= -20
            checked += 1
        else:
            assert np.array_equal(u, o)
    assert checked > 0


def test_seeded_reproducible():
    a = Muffle(seed=9, share=0.2, cutoff_hz=1200, rate=RATE)
    b = Muffle(seed=9, share=0.2, cutoff_hz=1200, rate=RATE)
    a.apply(_utterances(50))
    b.apply(_utterances(50))
    assert a.muffled == b.muffled
