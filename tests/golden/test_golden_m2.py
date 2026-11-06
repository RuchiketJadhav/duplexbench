"""Golden M2 (BUILD_PLAN M2-T12, §4; D3): the overlay + control-twin mock run reproduces
its stored Track A table exactly. Regenerate with `uv run python tests/golden/make_golden_m2.py`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import make_golden_m2 as g  # noqa: E402


def test_golden_m2_track_a_table_exact(tmp_path):
    expected = json.loads(g.EXPECTED.read_text(encoding="utf-8"))
    got = json.loads(json.dumps(g.generate(tmp_path)))     # tuples -> lists, as stored
    assert got["config"] == expected["config"]
    assert got["outcomes"] == expected["outcomes"]
    assert got["overlays"] == expected["overlays"]
    assert got["track_a"] == expected["track_a"]


def test_golden_m2_table_covers_track_a():
    expected = json.loads(g.EXPECTED.read_text(encoding="utf-8"))
    metrics = {row[1] for row in expected["track_a"]}
    assert {"yield_rate", "t_stop_ear", "hold_speaking_rate", "hold_listening_rate",
            "response_rate", "wait_compliance", "talk_over_share", "t_turn_gap_ear"} <= metrics
    tags = {t for row in expected["track_a"] for t in row[6]}
    assert {"not_injected", "ambiguous_end"} <= tags
    assert {row[3] for row in expected["track_a"] if row[1] == "yield_rate"} == {"@500", "@1000", "@2000"}
