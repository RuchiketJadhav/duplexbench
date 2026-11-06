"""Shared hand-computable fixture store for the M7 report tests (aggregate, caveats,
run card, html, verify). Built through `Store.put` like `cli score` would write it.

Two systems (sysA, sysB), conditions C0 (+ C1 for sysA pass_at_1), scenarios s1-s4.
Hand values used by the tests are noted beside each block.
"""
from __future__ import annotations

import json
from pathlib import Path

from duplexbench.storage import Store

RUN = "r1"
HASHES = {"pack": "p1", "langpack": "l1", "normalizer": "n1", "refasr": "a1", "simulator": "s1",
          "thresholds": "t1", "harness": "h1"}
SCEN = ("s1", "s2", "s3", "s4")
PRIVATE_SCENARIO = "s3"          # stands in for a private-test scenario id

# Pass@1 trials per scenario.  sysA: 7/12; sysB: 5/12; Pass^3: 1/4 each.
PASS = {
    "sysA": {"s1": [1, 1, 1], "s2": [1, 1, 0], "s3": [1, 0, 1], "s4": [0, 0, 0]},
    "sysB": {"s1": [1, 1, 1], "s2": [0, 1, 0], "s3": [0, 0, 1], "s4": [0, 0, 0]},
}
# sysA under C1: same as C0 except s2 trial 2 passes -> 8/12; C1 - C0 = +1/12
PASS_C1 = {"s1": [1, 1, 1], "s2": [1, 1, 1], "s3": [1, 0, 1], "s4": [0, 0, 0]}


def _obs(store, call_id, metric, scen, trial, cond, sid, value, valid=True, tags=(), event=None,
         run=RUN):
    store.put("observations", {
        "run_id": run, "call_id": call_id, "metric": metric, "scenario_id": scen, "trial": trial,
        "condition": cond, "system_id": sid, "event_id": event, "value": value,
        "valid": int(valid), "tags_json": json.dumps(list(tags))})


def _call(store, call_id, scen, trial, cond, sid, control=False, outcome="completed", run=RUN,
          wav="x.wav"):
    store.put("calls", {
        "call_id": call_id, "run_id": run, "scenario_id": scen, "trial": trial, "condition": cond,
        "system_id": sid, "is_control": int(control), "outcome": outcome, "started_ns": 0,
        "ended_ns": 10_000_000_000, "wav_path": wav, "usd_projected": 0.1, "usd_actual": 0.1,
        "flags_json": "{}"})


def cid(sid, cond, scen, trial, control=False):
    return f"{sid}-{cond}-{scen}-{trial}" + ("-ctl" if control else "")


def build(path: Path, run: str = RUN, hashes: dict | None = None, extra_metric: str | None = None,
          comparisons: list | None = None, classifier_kappa: float | None = None) -> Path:
    store = Store(path)
    card = {
        "date": "2026-11-04", "harness_version": "0.1.0", "instance_type": "c6i.xlarge",
        "refasr": {"version": "ref-1", "floors": {"phone": 0.05, "confirmation": 0.02}},
        "simulator": {"llm": "sim-llm", "tts": "sim-tts", "prompt_hash": "ph1"},
        "iteration_counts": {"sysA": 0, "sysB": 0}, "vendors_audio": ["vendorA", "vendorB"],
        "phone_path": {"disagreements": ["yield_rate"]},
        "network_baseline": {"api.example": {"tcp_connect_ms": 12.0}},
    }
    if classifier_kappa is not None:
        card["classifier_validation"] = {"unsupported_claim_rate": {"kappa": classifier_kappa}}
    config = {
        "systems": {
            "sysA": {"config_id": "cfgA", "preregistered_at": "2026-11-01T00:00:00Z",
                     "reasoning_effort": "low", "turn_detection": {"type": "server_vad"},
                     "self_hosted": False, "components_only": False, "cancel_event": True},
            "sysB": {"config_id": "cfgB", "preregistered_at": "2026-11-01T00:00:00Z",
                     "reasoning_effort": "n/a", "turn_detection": {"type": "semantic"},
                     "self_hosted": True, "components_only": False, "cancel_event": False},
        },
        "comparisons": comparisons if comparisons is not None else
        [{"metric": "pass_at_1", "a": "sysA", "b": "sysB"}],
        "baseline_condition": "C0",
    }
    store.put("runs", {"run_id": run, "started_ns": 0, "config_json": json.dumps(config),
                       "hashes_json": json.dumps(hashes or HASHES),
                       "calibration_json": json.dumps({"passed": True, "rtf": 0.2,
                                                       "loop_lag_p99_ms": 1.1}),
                       "run_card_json": json.dumps(card)})

    for sid in ("sysA", "sysB"):
        for sc in SCEN:
            for t in range(3):
                c = cid(sid, "C0", sc, t)
                _call(store, c, sc, t, "C0", sid, run=run)
                _obs(store, c, "pass_at_1", sc, t, "C0", sid, PASS[sid][sc][t],
                     tags=(f"domain:{'billing' if sc in ('s1', 's2') else 'orders'}",), run=run)
                _obs(store, c, "call_outcome", sc, t, "C0", sid, 1, run=run)
    for sc in SCEN:
        for t in range(3):
            c = cid("sysA", "C1", sc, t)
            _call(store, c, sc, t, "C1", "sysA", run=run)
            _obs(store, c, "pass_at_1", sc, t, "C1", "sysA", PASS_C1[sc][t], run=run)
    # control twin for s1 trial 0 (sysA): clean pass 1 -> interrupted 1 vs clean 1
    c = cid("sysA", "C0", "s1", 0, control=True)
    _call(store, c, "s1", 0, "C0", "sysA", control=True, run=run)
    _obs(store, c, "pass_at_1", "s1", 0, "C0", "sysA", 1, run=run)

    # Track A, one call per scenario trial 0.  yield_rate@1000: sysA 3/4, sysB 1/4.
    yields = {"sysA": [1, 1, 1, 0], "sysB": [0, 0, 1, 0]}
    for sid in ("sysA", "sysB"):
        for i, sc in enumerate(SCEN):
            c = cid(sid, "C0", sc, 0)
            _obs(store, c, "yield_rate", sc, 0, "C0", sid, yields[sid][i], tags=("@1000",),
                 event="ov1", run=run)
            _obs(store, c, "hold_speaking_rate", sc, 0, "C0", sid, 1, tags=("@2000",),
                 event="ov2", run=run)
            _obs(store, c, "response_rate", sc, 0, "C0", sid, 1 if i else 0, tags=("@5000",),
                 event="e_end", run=run)
            _obs(store, c, "hold_listening_rate", sc, 0, "C0", sid, 1, event="ov3", run=run)
        # extra exclusions (sysA): ambiguous_end, not_injected, unmatched_control,
        # slow_caller, unscripted_overlap, contested
        for j, tag in enumerate(("ambiguous_end", "not_injected", "unmatched_control",
                                 "contested")):
            _obs(store, cid(sid, "C0", "s1", 0), "yield_rate", "s1", 0, "C0", sid, None,
                 valid=False, tags=("@1000", tag), event=f"ovx{j}", run=run)
    _obs(store, cid("sysA", "C0", "s2", 1), "t_turn_gap_ear", "s2", 1, "C0", "sysA", None,
         valid=False, tags=("slow_caller",), event="turn9", run=run)
    _obs(store, cid("sysA", "C0", "s2", 1), "t_turn_gap_ear", "s2", 1, "C0", "sysA", None,
         valid=False, tags=("unscripted_overlap",), event="turn8", run=run)

    # t_turn_gap_ear sysA: 100..1000 ms over 10 turns -> p50 550, p90 910, n 10 (low n)
    for k in range(10):
        sc = SCEN[k % 4]
        _obs(store, cid("sysA", "C0", sc, 0), "t_turn_gap_ear", sc, 0, "C0", "sysA",
             100.0 * (k + 1), event=f"turn{k}", run=run)
        _obs(store, cid("sysB", "C0", sc, 0), "t_turn_gap_ear", sc, 0, "C0", "sysB",
             200.0 * (k + 1), event=f"turn{k}", run=run)
        _obs(store, cid("sysA", "C0", sc, 0), "t_ttfa_recv", sc, 0, "C0", "sysA",
             50.0 * (k + 1), event=f"turn{k}", run=run)
    # t_stop_ear sysA: 4 stopped + 1 not_stopped + 1 ambiguous_end -> completion 4/5
    for k, v in enumerate((200.0, 300.0, 400.0, 500.0)):
        sc = SCEN[k]
        _obs(store, cid("sysA", "C0", sc, 0), "t_stop_ear", sc, 0, "C0", "sysA", v,
             event="ov1", run=run)
        _obs(store, cid("sysA", "C0", sc, 0), "t_stop_model", sc, 0, "C0", "sysA", v - 100,
             event="ov1", run=run)
        _obs(store, cid("sysA", "C0", sc, 0), "playout_penalty", sc, 0, "C0", "sysA", 100.0,
             event="ov1", run=run)
        _obs(store, cid("sysA", "C0", sc, 0), "backlog_ms", sc, 0, "C0", "sysA", 60.0,
             event="ov1", run=run)
        _obs(store, cid("sysB", "C0", sc, 0), "t_stop_ear", sc, 0, "C0", "sysB", v + 400,
             event="ov1", run=run)
    _obs(store, cid("sysA", "C0", "s1", 1), "t_stop_ear", "s1", 1, "C0", "sysA", None,
         valid=False, tags=("not_stopped",), event="ov1", run=run)
    _obs(store, cid("sysA", "C0", "s2", 1), "t_stop_ear", "s2", 1, "C0", "sysA", None,
         valid=False, tags=("ambiguous_end",), event="ov1", run=run)

    # Track C extras, Track D, cost, simulator diagnostics
    for sid in ("sysA", "sysB"):
        for i, sc in enumerate(SCEN):
            c = cid(sid, "C0", sc, 0)
            _obs(store, c, "unsupported_claim_rate", sc, 0, "C0", sid, 1 if i == 0 else 0,
                 tags=("classifier",), run=run)
            _obs(store, c, "t_tool_to_info_ear", sc, 0, "C0", sid, 800.0 + 100 * i,
                 tags=("bucket:2000",), event="tool1", run=run)
            _obs(store, c, "anchor_offset_in_utt_ms", sc, 0, "C0", sid, 600.0 * (i + 1),
                 tags=("diagnostic",), event="tool1", run=run)
            _obs(store, c, "capture_first", sc, 0, "C0", sid, 1 if i % 2 else 0,
                 tags=("type:phone",), event="ent1", run=run)
            _obs(store, c, "readback_error_rate", sc, 0, "C0", sid, 0 if i or sid == "sysB" else 1,
                 tags=("type:phone",), event="ent1", run=run)
            _obs(store, c, "cost_per_min", sc, 0, "C0", sid, 0.1 if sid == "sysA" else 0.3,
                 run=run)
            _obs(store, c, "mishearing_rate", sc, 0, "C0", sid, 1 if i == 0 else 0, run=run)
            _obs(store, c, "regeneration_rate", sc, 0, "C0", sid, 1 if i == 0 else 0, run=run)
    # overlays row so the clip window resolves
    store.put("overlays", {"run_id": run, "call_id": cid("sysA", "C0", "s4", 0),
                           "overlay_id": "ov1", "cls": "Y-INT", "scheduled_ns": 1_000_000_000,
                           "actual_ns": 1_000_000_000, "onset_ns": 1_000_000_000,
                           "end_ns": 1_500_000_000, "tags_json": "[]"})
    if extra_metric:
        _obs(store, cid("sysA", "C0", "s1", 0), extra_metric, "s1", 0, "C0", "sysA", 1.0,
             run=run)
    store.close()
    return path
