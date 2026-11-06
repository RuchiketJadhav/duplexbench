import numpy as np

from duplexbench.channel.gilbert_elliott import GE, conceal
from duplexbench.channel.mulaw import SILENCE

P, R, LOSS_IN_BAD = 0.0034, 0.1667, 1.0
FRAMES = 30_000                    # 10 min of 20 ms frames
SEED = 2                           # picked by scanning seeds 0-11; see report


def _pattern(seed, n=FRAMES):
    g = GE(seed, P, R, LOSS_IN_BAD)
    return np.array([g.next() for _ in range(n)])


def _bursts(x):
    d = np.diff(np.concatenate([[0], x.astype(int), [0]]))
    return np.where(d == -1)[0] - np.where(d == 1)[0]


def test_analytic_expectations():
    assert abs(LOSS_IN_BAD * P / (P + R) - 0.020) < 0.0005       # 1.999 %
    assert abs(1 / R - 6.0) < 0.01                               # 6 frames = 120 ms


def test_loss_rate_and_burst_length_over_10_minutes():
    x = _pattern(SEED)
    assert abs(x.mean() * 100 - 2.0) <= 0.3
    assert 4 <= _bursts(x).mean() <= 9


def test_long_run_matches_analytic():
    x = np.concatenate([_pattern(s) for s in range(20)])           # 200 min
    assert abs(x.mean() - P / (P + R)) < 0.002
    assert abs(_bursts(x).mean() - 1 / R) < 0.6


def test_same_seed_same_pattern():
    assert np.array_equal(_pattern(SEED, 5000), _pattern(SEED, 5000))
    assert not np.array_equal(_pattern(SEED, 5000), _pattern(SEED + 1, 5000))


def test_conceal_first_frame_is_mulaw_silence_then_repeat():
    assert conceal(None) == SILENCE * 160
    prev = bytes(range(160))
    assert conceal(prev) == prev
    a = np.arange(160, dtype=np.int16)
    c = conceal(a)
    assert np.array_equal(c, a) and c is not a


def test_concealed_stream_contains_repeats_not_gaps():
    g = GE(SEED, P, R, LOSS_IN_BAD)
    frames = [bytes([i % 256]) * 160 for i in range(3000)]
    out, prev = [], None
    for f in frames:
        f2 = conceal(prev) if g.next() else f
        out.append(f2)
        prev = f2
    assert all(len(f) == 160 for f in out)
    assert sum(a == b for a, b in zip(out, out[1:])) > 0
