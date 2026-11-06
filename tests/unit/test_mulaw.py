import numpy as np

from duplexbench.channel import mulaw
from duplexbench.channel.mulaw import SILENCE, decode, encode


def _segment(x: int) -> int:
    mag = min(abs(x), 32635) + 0x84
    return max(0, min(7, int(np.floor(np.log2(mag))) - 7))


def test_zero_is_silence_byte():
    assert encode(np.zeros(1, np.int16)) == b"\xff" == SILENCE
    assert decode(b"\xff")[0] == 0


def test_round_trip_within_segment_step():
    xs = np.arange(-32124, 32125, 97, dtype=np.int16)
    ys = decode(encode(xs))
    for x, y in zip(xs.tolist(), ys.tolist()):
        assert abs(int(y) - x) <= 2 ** (_segment(x) + 3), (x, y)


def test_encode_monotonic_non_increasing_on_positive_side():
    xs = np.arange(0, 32768, dtype=np.int32).astype(np.int16)
    codes = np.frombuffer(encode(xs), dtype=np.uint8).astype(int)
    assert np.all(np.diff(codes) <= 0)


def test_sine_round_trip_snr():
    rate = 8000
    t = np.arange(rate) / rate
    amp = 32768 * 10 ** (-20 / 20) * np.sqrt(2)          # -20 dBFS RMS
    x = np.rint(amp * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
    y = decode(encode(x)).astype(np.float64)
    err = y - x
    snr = 10 * np.log10(np.sum(x.astype(np.float64) ** 2) / np.sum(err ** 2))
    assert snr >= 35, snr


def test_lengths_and_types():
    x = np.zeros(160, np.int16)
    b = encode(x)
    assert isinstance(b, bytes) and len(b) == 160
    assert decode(b).dtype == np.int16 and len(decode(b)) == 160


def test_no_audioop():
    import inspect
    import re
    assert not re.search(r"^\s*(import|from)\s+audioop", inspect.getsource(mulaw), re.M)
