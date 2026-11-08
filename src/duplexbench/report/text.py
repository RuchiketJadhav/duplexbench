"""Early text report: one fixed-width table of the `observations` of a run (BUILD_PLAN M2-T11; spec §10 items 2, 10).

    metric               system   variant      n  n_all    mean     p50  CI          tags
    yield_rate           mock-a   budget=500   4      5  0.7500  1.0000  (CI in M7)  ambiguous_end:1
                                  budget=1000  ...
    caveat: ambiguous_end on 1 observation(s) (yield_rate) — excluded from n; ...

Rows: every `Metric` member present in the run, in enum order; one row per
(system_id, variant), the metric name printed once. `variant` is the
`@<v>` tags that `cli score` writes (e.g. `@1000` = B 1.0 s; legacy `budget=` /
`window=` also accepted), which must not be pooled. `n` counts valid rows with a
value; `n_all` every row. `mean` and `p50` are over the valid values (a rate's
mean is the rate). Confidence intervals arrive in M7. Any metric name outside
`Metric` raises: report/ never prints an unregistered name (BUILD_PLAN §2.11).
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from duplexbench.metrics.names import Metric

if TYPE_CHECKING:
    from duplexbench.storage import Store

CI_PLACEHOLDER = "(CI in M7)"
VARIANT_KEYS = ("budget", "window")
CAVEAT_TAGS = {
    "ambiguous_end": "control turn ended before offset + window (spec §3.4)",
    "not_injected": "agent utterance ended before the overlay offset (spec §3.2)",
    "unmatched_control": "control twin diverged by caller step (spec §3.4)",
    "contested": "contested bucket, never headline (spec §3.3)",
    "no_refasr": "no reference-ASR words for the ear audio; excluded from n (fix-23)",
    "step_not_reached": "entity's caller step never ended; excluded from n (fix-32)",
}


class UnknownMetricError(ValueError):
    pass


def _rows(store: "Store | Path | str", run_id: str) -> list[sqlite3.Row]:
    if hasattr(store, "flush_sync"):
        store.flush_sync()
        path = store.path
    else:
        path = Path(store)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT metric, system_id, value, valid, tags_json FROM observations "
                            "WHERE run_id = ?", (run_id,)).fetchall()
    finally:
        conn.close()


def _is_variant(t: str) -> bool:
    """`@<v>` (report/aggregate.py convention, cli score) or legacy `budget=` / `window=`."""
    return t.startswith("@") or ("=" in t and t.split("=", 1)[0] in VARIANT_KEYS)


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.4f}"


def render(store: "Store | Path | str", run_id: str) -> str:
    """Render the run's observations as fixed-width text. Raises UnknownMetricError on a foreign name."""
    rows = _rows(store, run_id)
    known = {m.value for m in Metric}
    bad = sorted({r["metric"] for r in rows} - known)
    if bad:
        raise UnknownMetricError(f"observations contain metric names outside Metric: {bad}")

    groups: dict[tuple[str, str, str], list[tuple[float | None, bool, list[str]]]] = defaultdict(list)
    caveats: dict[str, Counter] = {t: Counter() for t in CAVEAT_TAGS}
    for r in rows:
        tags = list(json.loads(r["tags_json"] or "[]"))
        variant = ",".join(sorted(t for t in tags if _is_variant(t)))
        groups[(r["metric"], r["system_id"] or "", variant)].append(
            (r["value"], bool(r["valid"]), [t for t in tags if not _is_variant(t)]))
        for t in tags:
            if t in caveats:
                caveats[t][r["metric"]] += 1

    order = {m.value: i for i, m in enumerate(Metric)}

    def _vkey(v: str) -> tuple:
        parts = [("@", "", p[1:]) if p.startswith("@") else p.partition("=")
                 for p in v.split(",") if p]
        return tuple((k, int(x) if x.lstrip("-").isdigit() else x) for k, _, x in parts)

    keys = sorted(groups, key=lambda k: (order[k[0]], k[1], _vkey(k[2])))
    table: list[list[str]] = []
    last_metric = None
    for metric, system, variant in keys:
        obs = groups[(metric, system, variant)]
        vals = [v for v, ok, _ in obs if ok and v is not None]
        tagc = Counter(t for _, _, ts in obs for t in ts)
        table.append([
            metric if metric != last_metric else "",
            system, variant or "-", str(len(vals)), str(len(obs)),
            _fmt(statistics.fmean(vals) if vals else None),
            _fmt(statistics.median(vals) if vals else None),
            CI_PLACEHOLDER,
            ",".join(f"{t}:{c}" for t, c in sorted(tagc.items())) or "-",
        ])
        last_metric = metric

    header = ["metric", "system", "variant", "n", "n_all", "mean", "p50", "CI", "tags"]
    widths = [max(len(header[i]), *(len(row[i]) for row in table)) if table else len(header[i])
              for i in range(len(header))]
    numeric = {3, 4, 5, 6}

    def line(cells: list[str]) -> str:
        return "  ".join(c.rjust(widths[i]) if i in numeric else c.ljust(widths[i])
                         for i, c in enumerate(cells)).rstrip()

    out = [f"run {run_id}: {len(rows)} observation(s), {len({k[0] for k in keys})} metric(s)",
           line(header), line(["-" * w for w in widths])]
    out += [line(r) for r in table]
    if not table:
        out.append("(no observations)")
    for tag, desc in CAVEAT_TAGS.items():
        c = caveats[tag]
        if c:
            per = ", ".join(f"{m}:{n}" for m, n in sorted(c.items(), key=lambda x: order[x[0]]))
            out.append(f"caveat: {tag} on {sum(c.values())} observation(s) ({per}) - {desc}")
    return "\n".join(out) + "\n"
