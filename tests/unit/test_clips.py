"""report/clips.py: stereo clip cutting with an event timeline."""
import json

import numpy as np
import pytest
import soundfile as sf

from duplexbench.metrics.base import Event
from duplexbench.report.clips import cut

RATE = 8000
MS = 1_000_000


@pytest.fixture
def wav(tmp_path):
    n = RATE * 3                                     # 3 s
    left = np.arange(n, dtype=np.int64) % 32000
    right = -left
    p = tmp_path / "call1.wav"
    sf.write(str(p), np.stack([left, right], axis=1).astype(np.int16), RATE, subtype="PCM_16")
    return p


def test_cut_samples_channels_and_timeline(wav, tmp_path):
    evs = [Event(500 * MS, 0, "onset", "playout"), Event(1200 * MS, 1, "stop", "playout"),
           {"t_ns": 1100 * MS, "kind": "overlay", "src": "harness", "payload": {"id": "o1"}},
           Event(2500 * MS, 2, "onset", "playout")]
    clip, tl = cut(wav, 1000, 1500, evs, out_dir=tmp_path / "clips")
    data, rate = sf.read(str(clip), dtype="int16", always_2d=True)
    assert rate == RATE and data.shape == (4000, 2)
    assert data[0, 0] == 8000 and data[0, 1] == -8000
    t = json.loads(tl)
    assert t["t0_ms"] == 1000 and t["t1_ms"] == 1500 and t["channels"] == 2
    assert t["channel_names"] == ["caller", "ear"]
    assert [(e["kind"], e["t_ms"]) for e in t["events"]] == [("overlay", 100.0), ("stop", 200.0)]
    assert json.loads(clip.with_suffix(".json").read_text()) == t


def test_cut_clamps_and_origin(wav, tmp_path):
    clip, tl = cut(wav, 2500, 9000, [Event(12_700 * MS, 0, "x", "h")], out_dir=tmp_path,
                   origin_ns=10_000 * MS)
    assert sf.info(str(clip)).frames == RATE // 2
    assert json.loads(tl)["events"][0]["t_ms"] == 200.0
    with pytest.raises(ValueError):
        cut(wav, 4000, 5000, [], out_dir=tmp_path)


def test_default_out_dir(wav):
    clip, _ = cut(wav, 0, 100)
    assert clip.parent == wav.parent / "clips"
