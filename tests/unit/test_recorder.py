"""M1-T10: stereo recorder (spec §1.1)."""
from __future__ import annotations

import hashlib

import numpy as np
import soundfile as sf

from duplexbench.recorder import Recorder

MS = 1_000_000


def _f(v: int) -> np.ndarray:
    return np.full(160, v, np.int16)


def test_100_left_100_right(tmp_path):
    p = tmp_path / "a.wav"
    r = Recorder(p, 8000)
    for i in range(100):
        r.write_left(_f(1), i * 20 * MS)
        r.write_right(_f(2), i * 20 * MS)
    r.close()
    data, rate = sf.read(str(p), dtype="int16", always_2d=True)
    assert rate == 8000 and data.shape == (16_000, 2)
    assert (data[:, 0] == 1).all() and (data[:, 1] == 2).all()


def test_left_only_right_is_zeros(tmp_path):
    p = tmp_path / "b.wav"
    r = Recorder(p, 8000)
    for i in range(50):
        r.write_left(_f(3), i * 20 * MS)
    r.close()
    data, _ = sf.read(str(p), dtype="int16", always_2d=True)
    assert data.shape == (8_000, 2)
    assert (data[:, 0] == 3).all() and (data[:, 1] == 0).all()


def test_alignment_by_time_pads_gaps(tmp_path):
    p = tmp_path / "c.wav"
    r = Recorder(p, 8000)
    r.write_right(_f(5), 0)
    r.write_right(_f(6), 100 * MS)           # 80 ms gap -> zeros
    r.write_left(_f(7), 60 * MS)
    r.close()
    data, _ = sf.read(str(p), dtype="int16", always_2d=True)
    assert data.shape == (960, 2)
    assert (data[:160, 1] == 5).all() and (data[160:800, 1] == 0).all() and (data[800:, 1] == 6).all()
    assert (data[:480, 0] == 0).all() and (data[480:640, 0] == 7).all() and (data[640:, 0] == 0).all()


def test_origin_shifts_sample_zero(tmp_path):
    p = tmp_path / "o.wav"
    r = Recorder(p, 8000, origin_ns=4_000 * MS)
    r.write_right(_f(9), 4_000 * MS)
    r.close()
    data, _ = sf.read(str(p), dtype="int16", always_2d=True)
    assert data.shape == (160, 2) and (data[:, 1] == 9).all()


def test_close_twice_and_hash(tmp_path):
    p = tmp_path / "d.wav"
    r = Recorder(p, 16000)
    r.write_left(np.arange(320, dtype=np.int16), 0)
    h1 = r.close()
    h2 = r.close()
    assert h1 == h2 == hashlib.sha256(p.read_bytes()).hexdigest()
    data, rate = sf.read(str(p), dtype="int16", always_2d=True)
    assert rate == 16000 and data.shape == (320, 2)
