"""M2-T03: stimulus importer (trim at -50 dBFS, 50 ms padding, 24 kHz mono WAV)."""
import hashlib

import numpy as np
import pytest
import soundfile as sf

from duplexbench.stimuli.importer import ClipMeta, ImportParams, import_clip

RATE = 16000


def _tone(ms: int, rate: int = RATE, dbfs: float = -20.0) -> np.ndarray:
    t = np.arange(int(rate * ms / 1000)) / rate
    return (10 ** (dbfs / 20) * 32767 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)


def _write(path, lead_ms, speech_ms, tail_ms, rate=RATE):
    z = lambda ms: np.zeros(int(rate * ms / 1000), np.int16)
    sf.write(str(path), np.concatenate([z(lead_ms), _tone(speech_ms, rate), z(tail_ms)]), rate,
             subtype="PCM_16")


def test_leading_silence_trimmed_to_padding(tmp_path):
    src = tmp_path / "in.wav"
    _write(src, 300, 800, 400)
    m = import_clip(src, tmp_path / "out")
    assert isinstance(m, ClipMeta)
    assert abs(m.onset_ms - 50) <= 5
    assert abs(m.duration_ms - (800 + 100)) <= 5
    assert abs((m.end_ms - m.onset_ms) - 800) <= 5
    assert m.rate == 24000
    data, rate = sf.read(str(m.path), dtype="int16")
    assert rate == 24000 and data.ndim == 1
    assert m.sha256 == hashlib.sha256(m.path.read_bytes()).hexdigest()


def test_stereo_and_short_lead(tmp_path):
    src = tmp_path / "st.wav"
    mono = np.concatenate([np.zeros(int(RATE * 0.02), np.int16), _tone(500)])
    sf.write(str(src), np.stack([mono, mono], axis=1), RATE, subtype="PCM_16")
    m = import_clip(src, tmp_path / "out")
    assert abs(m.onset_ms - 20) <= 5            # less lead than the padding: keep what exists
    assert abs(m.duration_ms - (20 + 500)) <= 5


def test_quiet_audio_below_trim_is_silence(tmp_path):
    src = tmp_path / "q.wav"
    sf.write(str(src), _tone(300, dbfs=-60.0), RATE, subtype="PCM_16")
    with pytest.raises(ValueError, match="no audio above"):
        import_clip(src, tmp_path / "out")


def test_params_defaults():
    p = ImportParams()
    assert (p.trim_dbfs, p.pad_ms, p.out_rate) == (-50.0, 50, 24000)
