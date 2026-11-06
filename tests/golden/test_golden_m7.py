"""Golden M7 (BUILD_PLAN M7-T06): aggregate text, run card, caveats and HTML reproduce exactly."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_golden_m7 as golden  # noqa: E402

sys.path.pop(0)

from duplexbench.report.aggregate import to_json  # noqa: E402

EXPECTED = json.loads(golden.EXPECTED.read_text(encoding="utf-8"))

# the caveat classes CLAUDE.md M7 requires to fire on this fixture (one anchor each)
CAVEAT_ANCHORS = (
    "Low n: t_turn_gap_ear [sysA, C0] n=10",
    "tagged ambiguous_end", "tagged not_injected", "tagged unmatched_control", "tagged contested",
    "tagged slow_caller", "tagged unscripted_overlap",
    "simulator mishearing rate", "simulator regeneration rate",
    "Phone path disagrees with live stream on the sign of the paired difference for yield_rate.",
    "unsupported_claim_rate: classifier not validated",
    "sysB: no provider cancel event",
)
HTML_ANCHORS = ("1. Run card", "2. Profile table", "3. Floor-control charts", "4. Latency tails",
                "5. Task table", "6. Entity table", "7. Frontier charts", "8. Condition deltas",
                "9. Failure clip browser", "10. Caveats (auto-generated)",
                "Pre-registered comparisons (paired, Holm)", "Exclusions per system",
                "playout penalty", "backlog_ms", "anchor_offset_in_utt_ms", "<svg")


def test_golden_m7_reproduces_exactly(tmp_path):
    got = golden.generate(tmp_path)
    assert got["aggregate_sha256"] == EXPECTED["aggregate_sha256"]
    # the exact aggregate.json text
    text = (tmp_path / "report" / "aggregate.json").read_text(encoding="utf-8")
    assert text == to_json(EXPECTED["aggregate"])
    assert got["run_card"] == EXPECTED["run_card"] and got["run_card"]["missing"] == []
    assert got["caveats"] == EXPECTED["caveats"]
    assert got["index_html_sha256"] == EXPECTED["index_html_sha256"]
    assert got["verify"] == [] == EXPECTED["verify"]


def test_caveat_lines_fire():
    lines = EXPECTED["caveats"]
    for anchor in CAVEAT_ANCHORS:
        assert any(anchor in ln for ln in lines), anchor


def test_html_anchors_no_composite_and_every_table_shows_n(tmp_path):
    html = Path(golden.generate(tmp_path)["html_path"]).read_text(encoding="utf-8")
    for a in HTML_ANCHORS:
        assert a in html, a
    low = html.lower()
    for w in ("composite", "overall score", "total score"):
        # the template's comment says "No composite score anywhere"; rendered text must not
        assert w not in re.sub(r"<!--.*?-->", "", low, flags=re.S), w
    tables = re.findall(r"<table>(.*?)</table>", html, flags=re.S)
    assert len(tables) >= 8
    for t in tables:
        heads = [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th>(.*?)</th>", t, re.S)]
        if heads[:1] in (["Field"], ["name"]):        # run card and thresholds: facts, not numbers
            continue
        assert any(h == "n" or h.startswith("n ") or "(n)" in h for h in heads), heads


def test_two_regenerations_identical(tmp_path):
    a = golden.frozen(golden.generate(tmp_path / "a"))
    b = golden.frozen(golden.generate(tmp_path / "b"))
    assert a == b
