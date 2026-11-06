"""`duplexbench score <run_id> [--db PATH]` (default db results/<run_id>/events.sqlite) (BUILD_PLAN M2-T12; spec §1.1 "metrics are computed after the run").

    calls of the run (ORDER BY call_id) --CallRecord.load--> observers --> observations rows
                                                                       --> report/text.render

Observers, in this order per call: metrics.latency, metrics.floor_control,
metrics.diagnostics, then metrics.task.score_track_c and metrics.entities.score when
those modules exist (imported lazily; a missing module is reported as a note and
skipped). Control-twin calls are loaded as the `twin` of their overlay call and are
not scored themselves (aggregate never puts them in main rows, spec §3.4).
`t_stop_ear` comes from floor_control only (it carries the control-twin validity
tags); latency's copy is dropped. Likewise on Track C calls `t_tool_offset` comes from
metrics.task (ToolDef.anchor_step, domain tag); latency's copy is dropped. Re-scoring a run replaces its observations.
Ordering is deterministic: calls by call_id, observations by (metric, event_id,
tags) within a call.
"""
from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duplexbench.storage import canonical_json

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord, Observation
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.thresholds import Thresholds

RESULTS = Path("results")


def default_db(run_id: str) -> Path:
    """`duplexbench run` writes results/<run_id>/events.sqlite (same default as report/gate)."""
    return RESULTS / run_id / "events.sqlite"
FLOOR_OWNED = frozenset({"t_stop_ear"})
OPTIONAL = (("duplexbench.metrics.task", "score_track_c"), ("duplexbench.metrics.entities", "score"))

Observer = Callable[["CallRecord", "Thresholds"], list["Observation"]]


def observers(notes: list[str] | None = None) -> list[tuple[str, Observer]]:
    from duplexbench.metrics import diagnostics, floor_control, latency

    def lat(rec, th):
        drop = set(FLOOR_OWNED)
        if getattr(rec.scenario, "track", None) == "C":
            drop.add("t_tool_offset")       # Track C: task.score_track_c owns it (anchor_step)
        return [o for o in latency.observe(rec, th) if o.metric not in drop]

    out: list[tuple[str, Observer]] = [("latency", lat), ("floor_control", floor_control.observe),
                                       ("diagnostics", diagnostics.observe)]
    for mod, fn in OPTIONAL:
        try:
            m = importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name != mod:
                raise
            if notes is not None:
                notes.append(f"note: {mod} not built yet; skipped")
            continue
        f = getattr(m, fn, None)
        if f is None:
            if notes is not None:
                notes.append(f"note: {mod}.{fn} missing; skipped")
            continue
        out.append((f"{mod.rsplit('.', 1)[1]}.{fn}", _ADAPT[fn](f)))
    if notes is not None and len(out) > 3:
        notes.append("note: no reference-ASR words yet (M4): Track C / D observers get an "
                     "empty transcript")
    return out


def _task_adapter(f: Callable) -> Observer:
    """metrics.task.score_track_c(rec, scenario, tool_rows, refasr_words, th): Track C calls only."""
    def run(rec, th):
        if getattr(rec.scenario, "track", None) != "C":
            return []
        return f(rec, rec.scenario, list(rec.tool_calls), [], th)
    return run


def _entities_adapter(f: Callable) -> Observer:
    """metrics.entities.score(rec, scenario, refasr_words, floors, th): scenarios with entities."""
    def run(rec, th):
        if not getattr(rec.scenario, "entities", None):
            return []
        return f(rec, rec.scenario, [], None, th)
    return run


_ADAPT: dict[str, Callable[[Callable], Observer]] = {"score_track_c": _task_adapter,
                                                      "score": _entities_adapter}


def _sort_key(o: "Observation") -> tuple:
    return (o.metric, o.event_id or "", o.tags, -1 if o.value is None else o.value)


def score_run(db: Path | str, run_id: str, th: "Thresholds", *,
              scenarios: "Mapping[str, Scenario] | None" = None,
              notes: list[str] | None = None) -> list["Observation"]:
    """Score every overlay/clean call of `run_id`; replace its `observations` rows; return them."""
    from duplexbench.metrics.base import CallRecord
    db = Path(db)
    conn = sqlite3.connect(str(db))
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT call_id FROM calls WHERE run_id = ? AND COALESCE(is_control, 0) = 0 "
            "ORDER BY call_id", (run_id,))]
    finally:
        conn.close()
    obs_fns = observers(notes)
    out: list[Observation] = []
    for cid in ids:
        rec = CallRecord.load(db, cid, scenarios=scenarios)
        per: list[Observation] = []
        for _name, fn in obs_fns:
            per += fn(rec, th)
        out += sorted(per, key=_sort_key)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM observations WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO observations (run_id, call_id, metric, scenario_id, trial, condition, "
            "system_id, event_id, value, valid, tags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, o.call_id, o.metric, o.scenario_id, o.trial, o.condition, o.system_id,
              o.event_id, o.value, int(o.valid), canonical_json(list(o.tags))) for o in out])
        conn.commit()
    finally:
        conn.close()
    return out


def register(app: Any) -> None:
    import typer

    @app.command()
    def score(run_id: str = typer.Argument(..., help="run id in the event store"),
              db: Path = typer.Option(None, "--db", help="SQLite event store "
                                                         "(default results/<run_id>/events.sqlite)")) -> None:
        """Score a run: load every call, compute observations, print the text report (spec §4, §10)."""
        from duplexbench.report.text import render
        from duplexbench.storage import connect
        from duplexbench.thresholds import Thresholds

        if db is None:
            db = default_db(run_id)
        if not Path(db).exists():
            typer.echo(f"no event store at {db}", err=True)
            raise typer.Exit(2)
        connect(Path(db)).close()                # ensure the schema (observations table) exists
        notes: list[str] = []
        obs = score_run(db, run_id, Thresholds.load(), notes=notes)
        for n in notes:
            typer.echo(n)
        typer.echo(f"scored {len({o.call_id for o in obs})} call(s), {len(obs)} observation(s)")
        typer.echo(render(db, run_id), nl=False)
