"""Golden M1 (BUILD_PLAN M1-T16): the VirtualClock mock replay reproduces expected_m1.json exactly."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_golden_m1 as golden  # noqa: E402

sys.path.pop(0)


def test_golden_m1_reproduces_exactly(tmp_path):
    expected = json.loads(golden.EXPECTED.read_text(encoding="utf-8"))
    got = json.loads(json.dumps(golden.generate(tmp_path)))     # same JSON round trip
    for key in ("wav_sha256", "events_sha256", "playout_sha256", "playout_rows", "audio_events",
                "outcome", "overlays", "config"):
        assert got[key] == expected[key], key
    assert got["events"] == expected["events"]


def test_two_regenerations_identical(tmp_path):
    a = golden.generate(tmp_path / "a")
    b = golden.generate(tmp_path / "b")
    assert a == b


def test_golden_covers_the_m1_loop():
    exp = json.loads(golden.EXPECTED.read_text(encoding="utf-8"))
    kinds = {e[2] for e in exp["events"]}
    assert {"session_open", "first_audio_byte", "caller_turn_end", "overlay_onset",
            "cancel_or_truncate", "playout_flush", "agent_onset", "agent_stop", "tool_call",
            "closed", "call_end"} <= kinds
    assert exp["config"]["condition"] == "C1" and exp["outcome"] == "completed"
    assert exp["playout_rows"] == 250
