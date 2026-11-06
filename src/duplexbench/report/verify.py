"""Verify a rendered report against the store (BUILD_PLAN M7-T05; asr-bench report/verify.py pattern).

Recomputes `aggregate(store, run_id, th)` from the store (applying the `--public`
redaction when the rendered JSON says `public: true`) and diffs it against
`report_dir/aggregate.json`, field by field. `[]` = every number in the report
is reproduced from the store; otherwise one line per differing path:

    metrics.pass_at_1.sysA.C0.value: report=0.75 recomputed=0.5
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from duplexbench.report.aggregate import aggregate, redact_public, to_json
from duplexbench.thresholds import Thresholds

REL_TOL = 1e-9


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=REL_TOL)
    return a == b


def diff(report: Any, recomputed: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(report, dict) and isinstance(recomputed, dict):
        for k in sorted(set(report) | set(recomputed), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in report:
                out.append(f"{p}: missing in report (recomputed={json.dumps(recomputed[k])[:80]})")
            elif k not in recomputed:
                out.append(f"{p}: not in recomputed (report={json.dumps(report[k])[:80]})")
            else:
                out += diff(report[k], recomputed[k], p)
        return out
    if isinstance(report, list) and isinstance(recomputed, list):
        if len(report) != len(recomputed):
            return [f"{path}: length report={len(report)} recomputed={len(recomputed)}"]
        for i, (a, b) in enumerate(zip(report, recomputed)):
            out += diff(a, b, f"{path}[{i}]")
        return out
    if not _same(report, recomputed):
        out.append(f"{path}: report={json.dumps(report)} recomputed={json.dumps(recomputed)}")
    return out


def verify(store: Any, run_id: str, report_dir: Path | str, th: Thresholds) -> list[str]:
    path = Path(report_dir) / "aggregate.json"
    if not path.exists():
        return [f"{path}: not found"]
    rendered = json.loads(path.read_text(encoding="utf-8"))
    agg = aggregate(store, run_id, th)
    if rendered.get("public"):
        agg = redact_public(agg)
    recomputed = json.loads(to_json(agg))     # same serialisation as the rendered file
    return diff(rendered, recomputed)
