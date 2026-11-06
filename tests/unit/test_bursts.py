import numpy as np

from duplexbench.channel.bursts import Bursts

RATE = 8000


def _run(seed, minutes=60, chunk_s=1):
    b = Bursts(seed=seed, per_min=1.0, len_ms=[150, 400], level_db=-10, rate=RATE)
    z = np.zeros(RATE * chunk_s, np.int16)
    outs = [b.apply(z) for _ in range(minutes * 60 // chunk_s)]
    return b, outs


def test_poisson_count_and_lengths_over_60_minutes():
    b, _ = _run(seed=11)
    assert 40 <= len(b.events) <= 80
    for _, n in b.events:
        assert 150 <= n / RATE * 1e3 <= 400


def test_burst_audio_matches_events_and_level():
    b, outs = _run(seed=11, minutes=10)
    y = np.concatenate(outs).astype(float)
    active = np.abs(y) > 0
    for s, n in b.events:
        assert active[s:s + n].mean() > 0.99         # burst straddling chunks is continuous
    on = np.zeros(len(y), bool)
    for s, n in b.events:
        on[s:s + n] = True
    assert not active[~on].any()
    rms_db = 10 * np.log10(np.mean(y[on] ** 2) / 32768 ** 2)
    assert abs(rms_db - (-10)) < 0.5


def test_seeded_reproducible():
    a, oa = _run(seed=5, minutes=5)
    b, ob = _run(seed=5, minutes=5)
    assert a.events == b.events and all(np.array_equal(x, y) for x, y in zip(oa, ob))
    c, _ = _run(seed=6, minutes=5)
    assert c.events != a.events
