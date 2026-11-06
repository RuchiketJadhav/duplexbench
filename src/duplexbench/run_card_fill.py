"""Fill the run card facts a run collects outside the call loop (spec §10 item 1; BUILD_PLAN M7-T06, M9-T02).

    fill(store, run_id, *, calibration=None, netcheck=None, loopback=None, sysmon=None,
         refasr=None, simulator=None, instance_type=None, **extra) -> None

Writes into the `runs` row, in the shapes report/run_card.build reads:

    calibration   -> runs.calibration_json   (whole dict; `passed`, `rtf`, ... ; spec §1.4, D4)
    netcheck      -> run_card_json.network_baseline   (netcheck.netcheck() result; spec §4.4)
    loopback      -> run_card_json.loopback_baseline  (transport/loopback_baseline result; spec §6.2)
    sysmon        -> run_card_json.sysmon             (SysMon.stop() summary; spec §1.4)
    refasr        -> run_card_json.refasr             ({version, floors, ...}; spec §4.3)
    simulator     -> run_card_json.simulator          ({llm, tts, prompt_hash, ...}; spec §7)
    instance_type -> run_card_json.instance_type
    **extra       -> run_card_json.<key>  (date, harness_version, iteration_counts,
                     vendors_audio, classifier_validation, phone_path, ...)

An argument left as None keeps what the row already has. The row is rewritten whole
(INSERT OR REPLACE) through the Store writer; a missing run raises KeyError.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from duplexbench.report.aggregate import _connect
from duplexbench.storage import Store, canonical_json

CARD_KEYS = {"netcheck": "network_baseline", "loopback": "loopback_baseline", "sysmon": "sysmon",
             "refasr": "refasr", "simulator": "simulator", "instance_type": "instance_type"}


def fill(store: Any, run_id: str, *, calibration: dict | None = None,
         netcheck: dict | None = None, loopback: dict | None = None, sysmon: dict | None = None,
         refasr: dict | None = None, simulator: dict | None = None,
         instance_type: str | None = None, **extra: Any) -> None:
    conn = _connect(store)
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"run not in store: {run_id}")
    row = dict(row)
    card = json.loads(row["run_card_json"] or "{}")
    given = {"netcheck": netcheck, "loopback": loopback, "sysmon": sysmon, "refasr": refasr,
             "simulator": simulator, "instance_type": instance_type}
    for arg, key in CARD_KEYS.items():
        if given[arg] is not None:
            card[key] = given[arg]
    for k, v in extra.items():
        if v is not None:
            card[k] = v
    row["run_card_json"] = canonical_json(card)
    if calibration is not None:
        row["calibration_json"] = canonical_json(calibration)
    own = not hasattr(store, "put")
    s = Store(Path(store)) if own else store
    try:
        s.put("runs", row)
        s.flush_sync()
    finally:
        if own:
            s.close()
