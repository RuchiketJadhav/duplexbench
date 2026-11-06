import numpy as np

from duplexbench.channel.resample import Resampler, band_limit


def _noise(n, seed=0):
    return np.random.default_rng(seed).normal(0, 3000, n).astype(np.int16)


def test_one_second_24k_to_8k_is_exactly_8000_samples():
    r = Resampler(24000, 8000)
    x = _noise(24000)
    out = [r.push(x[i:i + 480]) for i in range(0, 24000, 480)]
    total = sum(len(o) for o in out) + len(r.flush())
    assert total == 8000


def test_chunked_push_equals_single_push():
    x = _noise(999, seed=1)
    a = Resampler(24000, 8000)
    ya = np.concatenate([a.push(x[:333]), a.push(x[333:666]), a.push(x[666:]), a.flush()])
    b = Resampler(24000, 8000)
    yb = np.concatenate([b.push(x), b.flush()])
    assert abs(len(ya) - len(yb)) <= 1
    n = min(len(ya), len(yb))
    assert np.max(np.abs(ya[:n].astype(int) - yb[:n].astype(int))) <= 1


def test_delay_ms_stored_and_small():
    for src, dst in [(24000, 8000), (16000, 8000), (8000, 16000), (48000, 8000)]:
        r = Resampler(src, dst)
        assert isinstance(r.delay_ms, float)
        assert 0.0 <= r.delay_ms < 5.0, (src, dst, r.delay_ms)
    assert Resampler(8000, 8000).delay_ms == 0.0


def test_streaming_output_is_not_bursty():
    # every 20 ms chunk after the first yields a full 20 ms of output (no block hold-back)
    r = Resampler(16000, 8000)
    lens = [len(r.push(_noise(320, seed=i))) for i in range(20)]
    assert all(n == 160 for n in lens[1:]), lens


def test_output_is_time_aligned():
    # a 100 ms tone burst starting at 0.5 s stays at 0.5 s after resampling
    src = 24000
    t = np.arange(src) / src
    x = np.where((t >= 0.5) & (t < 0.6), 8000 * np.sin(2 * np.pi * 440 * t), 0).astype(np.int16)
    r = Resampler(src, 8000)
    y = np.concatenate([r.push(x), r.flush()]).astype(float)
    onset = np.argmax(np.abs(y) > 2000) / 8000
    assert abs(onset - 0.5) < 0.002


def test_to_frames_keeps_remainder():
    r = Resampler(8000, 8000)
    f1 = r.to_frames(np.arange(250, dtype=np.int16), 8000)
    assert len(f1) == 1 and len(f1[0]) == 160
    f2 = r.to_frames(np.arange(250, 320, dtype=np.int16), 8000)
    assert len(f2) == 1 and f2[0][0] == 160 and f2[0][-1] == 319
    assert len(r.to_frames(np.zeros(100, np.int16), 16000)) == 0   # 320-sample frames at 16 kHz


def _tone_db(f, rate=8000, n=8000):
    t = np.arange(n) / rate
    x = (8000 * np.sin(2 * np.pi * f * t)).astype(np.int16)
    y = band_limit(x, rate).astype(float)
    mid = slice(n // 4, 3 * n // 4)
    return 10 * np.log10((np.mean(y[mid] ** 2) + 1e-9) / np.mean(x[mid].astype(float) ** 2))


def test_band_limit_attenuates_3600hz_by_20db():
    assert _tone_db(3600) <= -20
    assert abs(_tone_db(1000)) < 0.5          # passband untouched
