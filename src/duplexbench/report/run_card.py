"""Run card (spec §10 item 1; BUILD_PLAN M7-T06).

Assembled from the `runs` row: `run_card_json` (free-form facts the run wrote:
date, harness version, reference-ASR version and floors, simulator versions and
prompt hash, instance type, iteration counts, vendors that received audio,
network baseline, classifier validation), `hashes_json`, `calibration_json`,
`config_json` (per-system config_id, pre-registration timestamp, reasoning
effort, turn detection) and the thresholds in force (`th.items()`).

Every spec field is present in the output; a field the run did not record is
`None` and listed under `missing`, so an incomplete card is visible, never hidden.
"""
from __future__ import annotations

import json
from typing import Any

from duplexbench.report.aggregate import _connect
from duplexbench.thresholds import Thresholds

# spec §10 item 1, in order
FIELDS = ("date", "harness_version", "scenario_pack_hash", "systems", "thresholds",
          "thresholds_hash", "refasr", "simulator", "simulator_patience_rule", "calibration",
          "network_baseline", "instance_type", "iteration_counts", "vendors_audio")
SYSTEM_FIELDS = ("config_id", "preregistered_at", "reasoning_effort", "turn_detection")


def _j(s: str | None) -> Any:
    return {} if not s else json.loads(s)


def patience_rule(th: Thresholds) -> str:
    """spec §7 patience rule, worded from the thresholds in force."""
    return (f"While a tool is pending, and for {th.sim_patience_ms} ms after its result, the "
            "simulated caller does not take the floor on silence; outside tool waits, agent end "
            f"of turn = ear inactivity >= {th.sim_eot_ms} ms, floor delay U{list(th.sim_floor_delay_ms)} ms.")


def build(store: Any, run_id: str, th: Thresholds, netcheck: dict | None = None) -> dict:
    conn = _connect(store)
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"run not in store: {run_id}")
    card, hashes = _j(row["run_card_json"]), _j(row["hashes_json"])
    calib, config = _j(row["calibration_json"]), _j(row["config_json"])

    systems = {}
    for sid, s in sorted((config.get("systems") or {}).items()):
        s = s or {}
        systems[sid] = {k: s.get(k) for k in SYSTEM_FIELDS}
        for k, v in sorted((card.get("systems") or {}).get(sid, {}).items()):
            systems[sid].setdefault(k, v)
            if systems[sid][k] is None:
                systems[sid][k] = v

    out: dict[str, Any] = {
        "run_id": run_id,
        "date": card.get("date"),
        "harness_version": card.get("harness_version"),
        "scenario_pack_hash": hashes.get("pack"),
        "hashes": dict(sorted(hashes.items())),
        "systems": systems or None,
        "thresholds": th.items(),
        "thresholds_hash": th.hash(),
        "refasr": card.get("refasr"),
        "simulator": card.get("simulator"),
        "simulator_patience_rule": patience_rule(th),
        "calibration": calib or None,
        "network_baseline": netcheck if netcheck is not None else card.get("network_baseline"),
        "instance_type": card.get("instance_type"),
        "iteration_counts": card.get("iteration_counts"),
        "vendors_audio": card.get("vendors_audio"),
        "classifier_validation": card.get("classifier_validation"),
    }
    if card.get("scripted") is not None:     # fix-34: Track A/D lead-in (per-call
        out["scripted"] = card["scripted"]   # greeting_end_ms / no_greeting are call flags)
    missing = [f for f in FIELDS if out.get(f) in (None, {}, [])]
    for sid, s in systems.items():
        missing += [f"systems.{sid}.{k}" for k in SYSTEM_FIELDS if s.get(k) is None]
    if calib and "rtf" not in calib and "refasr_rtf" not in calib:
        missing.append("calibration.rtf")
    out["missing"] = missing
    return out
