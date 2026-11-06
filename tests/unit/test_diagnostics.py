"""M2-T09: TOR with FDB v1's exact rule (eval_pause_handling.py:7-8, 42-60)."""
from pathlib import Path

import numpy as np

from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, OverlayRow
from duplexbench.metrics.diagnostics import Word, observe, takeover, takeover_backchannel
from duplexbench.scenarios.loader import load_pack
from duplexbench.thresholds import Thresholds

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"


def _words(n, dur_ms, start=100.0):
    step = dur_ms / n
    return [Word(f"w{i}", start + i * step, start + (i + 1) * step) for i in range(n)]


def test_hand_cases():
    assert takeover([]) is False                       # silence
    assert takeover(_words(1, 800)) is False           # 0.8 s, one word
    assert takeover(_words(3, 1500)) is True           # 1.5 s, three words
    assert takeover(_words(4, 800)) is True            # 0.8 s, four words (> 3)
    assert takeover(_words(3, 800)) is False           # <= 3 words under 1 s
    assert takeover(_words(2, 1000)) is True           # exactly 1 s is not "< 1 s"


def test_end_none_uses_last_start():
    assert takeover([Word("a", 0, 100), Word("b", 900, None)]) is False   # duration 0.9 s


def test_backchannel_rule():
    assert takeover_backchannel([]) is False
    assert takeover_backchannel([(0, 500, ["mm", "hm"])]) is False
    assert takeover_backchannel([(0, 500, ["a", "b", "c"])]) is True
    assert takeover_backchannel([(0, 3500, [])]) is True
    assert takeover_backchannel([(0, 1500, ["yeah"])]) is True


def test_observe_tags_and_refasr_gap():
    sc = next(x for x in load_pack(FIX).scenarios if x.id == "sched_001")
    rows = [OverlayRow("o1", "Y-INT", 1, 1, 1, 2), OverlayRow("o2", "H-BC", 1, 1, 1, 2),
            OverlayRow("o3", "P-PAUSE", 1, 1, 1, 2),
            OverlayRow("o4", "E-END", 1, 1, 1, 2, ("not_injected",))]
    rec = CallRecord(events=[], playout=np.zeros(0, PLAYOUT_DTYPE), overlays=rows, tool_calls=[],
                     sim_turns=[], wav_path=Path("x.wav"), scenario=sc, twin=None, call_id="c1",
                     system_id="m", condition="C0")
    by = {o.event_id: o for o in observe(rec, Thresholds(), {"o1": _words(5, 2000), "o3": []})}
    assert set(by) == {"o1", "o3", "o4"}               # H-BC is the backchannel rule's, not here
    assert by["o1"].value == 1.0 and by["o1"].valid
    assert by["o3"].value == 0.0 and by["o3"].valid
    assert not by["o4"].valid and "not_injected" in by["o4"].tags
    assert all(o.metric == "tor" and "diagnostic" in o.tags for o in by.values())
    assert "cls:Y-INT" in by["o1"].tags and "cls:P-PAUSE" in by["o3"].tags   # shared cls:<class>
    assert not any(t.startswith("cls=") for o in by.values() for t in o.tags)
    assert all(not o.valid for o in observe(rec, Thresholds()))
