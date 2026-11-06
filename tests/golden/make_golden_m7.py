"""Golden M7 (BUILD_PLAN M7-T06, §4; CLAUDE.md M7 "report byte-identical on re-render, verify 0").

The deterministic two-system fixture store of tests/unit/_report_fixture.py (sysA,
sysB; C0 + C1; scenarios s1-s4; every exclusion tag; a classifier metric; a
self-hosted system without a cancel event; a phone-path disagreement) is aggregated,
its run card built and its caveats listed; the HTML report is rendered without clips
(clip cutting is report/clips.py's own test). Frozen in expected_m7.json:

    aggregate_sha256   sha256 of report_dir/aggregate.json (the exact text)
    aggregate          the same JSON, parsed (for a readable diff on failure)
    run_card           report/run_card.build(...)
    caveats            report/caveats.lines(...)
    index_html_sha256  sha256 of report_dir/index.html
    verify             report/verify.verify(...) on the rendered dir (must be [])

    uv run python tests/golden/make_golden_m7.py     # rewrites expected_m7.json

Regenerate only with an intentional change to stats, aggregation or the report
(BUILD_PLAN §4), and say why in docs/worklog.md.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "unit"))
import _report_fixture as fx  # noqa: E402

sys.path.pop(0)

from duplexbench.report import caveats as cav  # noqa: E402
from duplexbench.report.aggregate import aggregate, to_json  # noqa: E402
from duplexbench.report.html import render  # noqa: E402
from duplexbench.report.run_card import build as build_card  # noqa: E402
from duplexbench.report.verify import verify  # noqa: E402
from duplexbench.thresholds import Thresholds  # noqa: E402

EXPECTED = HERE / "expected_m7.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def generate(out_dir: Path) -> dict:
    """Build the fixture store under out_dir, render the report to out_dir/report."""
    th = Thresholds()           # spec defaults, independent of configs/thresholds.yaml edits
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db = fx.build(out_dir / "events.sqlite")
    agg = aggregate(db, fx.RUN, th)
    card = build_card(db, fx.RUN, th)
    report_dir = out_dir / "report"
    index = render(agg, card, report_dir, th, public=False, clips=None)
    agg_path = report_dir / "aggregate.json"
    assert agg_path.read_text(encoding="utf-8") == to_json(agg)
    return {
        "aggregate_sha256": _sha(agg_path),
        "aggregate": json.loads(agg_path.read_text(encoding="utf-8")),
        "run_card": json.loads(json.dumps(card)),
        "caveats": cav.lines(agg, th),
        "index_html_sha256": _sha(index),
        "verify": verify(db, fx.RUN, report_dir, th),
        "html_path": str(index),
    }


def frozen(g: dict) -> dict:
    return {k: v for k, v in g.items() if k != "html_path"}


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = frozen(generate(Path(d) / "a"))
        b = frozen(generate(Path(d) / "b"))
    if a != b:
        raise SystemExit("two regenerations differ; not writing expected_m7.json")
    if a["verify"]:
        raise SystemExit(f"verify is not clean: {a['verify']}")
    EXPECTED.write_text(json.dumps(a, sort_keys=True, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
    print(f"wrote {EXPECTED} (aggregate {a['aggregate_sha256'][:12]}, "
          f"index.html {a['index_html_sha256'][:12]}, {len(a['caveats'])} caveats)")


if __name__ == "__main__":
    main()
