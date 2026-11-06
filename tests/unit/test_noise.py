import numpy as np

from duplexbench.channel.noise import NoiseBed

RATE = 8000
FRAME = 160


def _speech_fixture(seconds=10, seed=0):
    """Alternating 1 s 'speech' (modulated tones) and 0.5 s silence, plus the frame mask."""
    rng = np.random.default_rng(seed)
    t = np.arange(RATE) / RATE
    parts, mask = [], []
    for _ in range(int(seconds / 1.5) + 1):
        f0 = rng.uniform(120, 220)
        s = 6000 * np.sin(2 * np.pi * f0 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
        parts += [s, np.zeros(RATE // 2)]
        mask += [True] * (RATE // FRAME) + [False] * (RATE // 2 // FRAME)
    x = np.concatenate(parts)[: seconds * RATE]
    return x.astype(np.int16), np.array(mask[: seconds * RATE // FRAME])


def _snr_on_active(clean, noisy, mask):
    m = np.repeat(mask, FRAME)
    c = clean.astype(float)[m]
    n = noisy.astype(float)[m] - c
    return 10 * np.log10(np.mean(c ** 2) / np.mean(n ** 2))


def test_snr_on_speech_active_frames_is_15db():
    x, mask = _speech_fixture()
    y = NoiseBed(seed=7, snr_db=15, drift_db=0, rate=RATE).apply(x, mask)
    assert abs(_snr_on_active(x, y, mask) - 15) <= 1


def test_snr_with_drift_stays_within_drift_bound():
    x, mask = _speech_fixture()
    y = NoiseBed(seed=7, snr_db=15, drift_db=3, rate=RATE).apply(x, mask)
    assert abs(_snr_on_active(x, y, mask) - 15) <= 3 + 1


def test_drift_is_clipped_and_moves():
    nb = NoiseBed(seed=3, snr_db=15, drift_db=3, rate=RATE)
    track = nb._drift_track(RATE * 120)
    per_s = track[::RATE]
    assert np.all(np.abs(per_s) <= 3) and len(set(per_s.round(6))) > 5


def test_noise_present_in_silence():
    x, mask = _speech_fixture()
    y = NoiseBed(seed=7, snr_db=15, drift_db=0, rate=RATE).apply(x, mask)
    silent = ~np.repeat(mask, FRAME)
    assert np.std(y[silent].astype(float)) > 100


def test_same_seed_identical_different_seed_differs():
    x, mask = _speech_fixture()
    a = NoiseBed(seed=1, snr_db=15, drift_db=3, rate=RATE).apply(x, mask)
    b = NoiseBed(seed=1, snr_db=15, drift_db=3, rate=RATE).apply(x, mask)
    c = NoiseBed(seed=2, snr_db=15, drift_db=3, rate=RATE).apply(x, mask)
    assert np.array_equal(a, b) and not np.array_equal(a, c)


def test_per_sample_mask_accepted():
    x, mask = _speech_fixture(seconds=3)
    y = NoiseBed(seed=1, snr_db=15, drift_db=0, rate=RATE).apply(x, np.repeat(mask, FRAME))
    assert abs(_snr_on_active(x, y, mask) - 15) <= 1
