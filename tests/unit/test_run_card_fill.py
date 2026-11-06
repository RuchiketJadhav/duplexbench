"""run_card_fill.fill (BUILD_PLAN M7-T06): every filled fact lands where run_card.build reads it."""
from __future__ import annotations

import json
import sqlite3

import pytest

from duplexbench.report.run_card import build
from duplexbench.run_card_fill import fill
from duplexbench.storage import Store
from duplexbench.thresholds import Thresholds

TH = Thresholds()


def _bare_run(path):
    s = Store(path)
    s.put("runs", {"run_id": "r", "started_ns": 0,
                   "config_json": json.dumps({"systems": {"sysA": {
                       "config_id": "c", "preregistered_at": "2026-11-01T00:00:00Z",
                       "reasoning_effort": "low", "turn_detection": {"type": "server_vad"}}}}),
                   "hashes_json": json.dumps({"pack": "p"}), "calibration_json": None,
                   "run_card_json": json.dumps({"date": "2026-11-05"})})
    s.close()
    return path


def test_fill_completes_the_card(tmp_path):
    p = _bare_run(tmp_path / "e.sqlite")
    before = build(p, "r", TH)["missing"]
    assert {"calibration", "network_baseline", "refasr", "simulator", "instance_type"} <= set(before)
    fill(p, "r", calibration={"passed": True, "rtf": 0.2},
         netcheck={"api.x:443": {"tcp_connect_ms": 11.0}},
         loopback={"rtt_ms_median": 41.0, "jitter_ms_median": 2.0},
         sysmon={"loop_lag_p99_ms": 0.4, "valid": True},
         refasr={"version": "ref-1", "floors": {"phone": 0.05}},
         simulator={"llm": "m", "tts": "t", "prompt_hash": "h"}, instance_type="c6i.xlarge",
         harness_version="0.1.0", iteration_counts={"sysA": 0}, vendors_audio=["v"])
    card = build(p, "r", TH)
    assert card["missing"] == []
    assert card["calibration"] == {"passed": True, "rtf": 0.2}
    assert card["network_baseline"] == {"api.x:443": {"tcp_connect_ms": 11.0}}
    assert card["instance_type"] == "c6i.xlarge" and card["date"] == "2026-11-05"
    raw = json.loads(sqlite3.connect(p).execute("select run_card_json from runs").fetchone()[0])
    assert raw["loopback_baseline"]["rtt_ms_median"] == 41.0 and raw["sysmon"]["valid"] is True


def test_none_keeps_existing_and_open_store_works(tmp_path):
    p = _bare_run(tmp_path / "e.sqlite")
    fill(p, "r", instance_type="a")
    s = Store(p)
    fill(s, "r", refasr={"version": "x"})
    s.close()
    card = build(p, "r", TH)
    assert card["instance_type"] == "a" and card["refasr"] == {"version": "x"}


def test_missing_run_raises(tmp_path):
    p = _bare_run(tmp_path / "e.sqlite")
    with pytest.raises(KeyError):
        fill(p, "nope", instance_type="a")
