"""Metric observation and the read-only call record metrics run on (BUILD_PLAN §2.9; spec §4, §10).

Every metric module exposes

    def observe(rec: CallRecord, th: Thresholds) -> list[Observation]

Values are `None` with `valid=False` and a tag when undefined: `not_stopped`,
`ambiguous_end`, `not_injected`, `unmatched_control`, `contested`,
`slow_caller`, `unscripted_overlap`, `classifier`, `diagnostic` (spec §3.3,
§3.4, §4.5, §7, §8).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.storage import Store

TAGS = ("not_stopped", "ambiguous_end", "not_injected", "unmatched_control", "contested",
        "slow_caller", "unscripted_overlap", "classifier", "diagnostic")

# One row per played frame (storage `playout` table, BUILD_PLAN §2.4).
PLAYOUT_DTYPE = np.dtype([("t_ns", "i8"), ("frame_idx", "i8"), ("rms_dbfs", "f8"),
                          ("active", "?"), ("queue_depth", "i4"), ("backlog_ms", "f8"),
                          ("flushed", "?")])


@dataclass(frozen=True)
class Observation:
    metric: str            # a duplexbench.metrics.names.Metric member
    call_id: str
    scenario_id: str
    trial: int
    condition: str
    system_id: str
    event_id: str | None
    value: float | None
    valid: bool
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Event:
    """`events` table row (BUILD_PLAN §2.4)."""
    t_ns: int
    seq: int
    kind: str
    src: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OverlayRow:
    overlay_id: str
    cls: str
    scheduled_ns: int | None
    actual_ns: int | None
    onset_ns: int | None
    end_ns: int | None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolRow:
    tool_call_id: str
    name: str
    args: dict[str, Any]
    start_ns: int
    result_ns: int | None
    injected_latency_ms: int | None
    injected_failure: str | None
    result: dict[str, Any] | None


@dataclass(frozen=True)
class SimRow:
    turn_idx: int
    text: str
    tts_words: list[dict[str, Any]]
    send_ns: int
    think_ms: float | None
    speculative: bool
    discarded: bool


@dataclass
class CallRecord:
    """Everything one call left behind; loaded by `cli score`, read-only (BUILD_PLAN §2.9).

    The first eight fields are the BUILD_PLAN contract. The identity fields after
    them are needed to fill `Observation` rows.
    """
    events: list[Event]
    playout: np.ndarray            # structured, dtype PLAYOUT_DTYPE
    overlays: list[OverlayRow]
    tool_calls: list[ToolRow]
    sim_turns: list[SimRow]
    wav_path: Path
    scenario: "Scenario"
    twin: "CallRecord | None"
    call_id: str = ""
    run_id: str = ""
    trial: int = 0
    condition: str = ""
    system_id: str = ""
    is_control: bool = False

    @classmethod
    def load(cls, store: "Store | Path | str", call_id: str, *,
             scenarios: "Mapping[str, Scenario] | None" = None,
             with_twin: bool = True) -> "CallRecord":
        """Read one call's rows from the event store (BUILD_PLAN M2-T12).

        `store` is a `Store` (flushed first) or a path to its SQLite file. The
        scenario comes from `scenarios` (id -> Scenario) or else from the run's
        `runs.config_json["scenarios"]` (id -> scenario dump); without either a
        stub scenario with the id and no steps or overlays is used. A control call
        gets `make_twin(scenario)`. `with_twin`: an overlay call gets its control
        twin's record (same run, scenario, condition, system, trial) as `twin`.
        Rows are ordered deterministically: events by (t_ns, seq), playout by
        t_ns, overlays by overlay_id.
        """
        conn = _connect(store)
        try:
            return _load(conn, call_id, scenarios, with_twin)
        finally:
            conn.close()


# -- loading (BUILD_PLAN M2-T12) ---------------------------------------------------------
def _connect(store: Any) -> sqlite3.Connection:
    if hasattr(store, "flush_sync"):
        store.flush_sync()
        path = store.path
    else:
        path = Path(store)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _j(s: str | None, default: Any) -> Any:
    return default if not s else json.loads(s)


def _scenario(conn: sqlite3.Connection, run_id: str, scenario_id: str,
              scenarios: "Mapping[str, Scenario] | None") -> "Scenario":
    from duplexbench.scenarios.schema import Scenario
    if scenarios and scenario_id in scenarios:
        return scenarios[scenario_id]
    row = conn.execute("SELECT config_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    cfg = _j(row["config_json"], {}) if row else {}
    dumped = (cfg.get("scenarios") or {}) if isinstance(cfg, dict) else {}
    if scenario_id in dumped:
        return Scenario.model_validate(dumped[scenario_id])
    return Scenario.model_construct(id=scenario_id, version="", split="public-dev", domain="",
                                    track="A", source="scripted", caller_voice="", steps=[],
                                    overlays=[], tools=[], conditions=[], trials=1,
                                    control_twin=False)


def _load(conn: sqlite3.Connection, call_id: str, scenarios: Any, with_twin: bool) -> CallRecord:
    c = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    if c is None:
        raise KeyError(f"no call {call_id!r} in the store")
    scen = _scenario(conn, c["run_id"], c["scenario_id"] or "", scenarios)
    is_control = bool(c["is_control"])
    if is_control:
        from duplexbench.scenarios.control_twin import make_twin
        scen = make_twin(scen)
    events = [Event(int(r["t_ns"]), int(r["seq"]), r["kind"], r["src"], _j(r["payload_json"], {}))
              for r in conn.execute("SELECT t_ns, seq, kind, src, payload_json FROM events "
                                    "WHERE call_id = ? ORDER BY t_ns, seq", (call_id,))]
    prow = conn.execute("SELECT t_ns, frame_idx, rms_dbfs, active, queue_depth, backlog_ms, "
                        "flushed FROM playout WHERE call_id = ? ORDER BY t_ns, frame_idx",
                        (call_id,)).fetchall()
    playout = np.array([(r[0], r[1], np.nan if r[2] is None else r[2], bool(r[3]), r[4],
                         r[5] or 0.0, bool(r[6])) for r in prow], dtype=PLAYOUT_DTYPE)
    overlays = [OverlayRow(r["overlay_id"], r["cls"] or "", r["scheduled_ns"], r["actual_ns"],
                           r["onset_ns"], r["end_ns"], tuple(_j(r["tags_json"], [])))
                for r in conn.execute("SELECT * FROM overlays WHERE call_id = ? "
                                      "ORDER BY overlay_id", (call_id,))]
    tools = [ToolRow(r["tool_call_id"], r["name"], _j(r["args_json"], {}), r["start_ns"],
                     r["result_ns"], r["injected_latency_ms"], r["injected_failure"],
                     _j(r["result_json"], None))
             for r in conn.execute("SELECT * FROM tool_calls WHERE call_id = ? "
                                   "ORDER BY start_ns, tool_call_id", (call_id,))]
    sims = [SimRow(r["turn_idx"], r["text"] or "", _j(r["tts_words_json"], []), r["send_ns"],
                   r["think_ms"], bool(r["speculative"]), bool(r["discarded"]))
            for r in conn.execute("SELECT * FROM sim_turns WHERE call_id = ? ORDER BY turn_idx",
                                  (call_id,))]
    twin = None
    if with_twin and not is_control:
        t = conn.execute("SELECT call_id FROM calls WHERE run_id = ? AND scenario_id = ? AND "
                         "condition = ? AND system_id = ? AND trial = ? AND is_control = 1 "
                         "ORDER BY call_id", (c["run_id"], c["scenario_id"], c["condition"],
                                              c["system_id"], c["trial"])).fetchone()
        if t is not None:
            twin = _load(conn, t["call_id"], {scen.id: scen}, False)
    return CallRecord(events=events, playout=playout, overlays=overlays, tool_calls=tools,
                      sim_turns=sims, wav_path=Path(c["wav_path"] or ""), scenario=scen,
                      twin=twin, call_id=call_id, run_id=c["run_id"], trial=int(c["trial"] or 0),
                      condition=c["condition"] or "", system_id=c["system_id"] or "",
                      is_control=is_control)
