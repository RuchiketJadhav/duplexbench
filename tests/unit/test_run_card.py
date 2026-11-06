"""M7-T06 (run card only): spec §10 item 1 assembled from the runs row + thresholds."""
import json

import pytest

from _report_fixture import HASHES, build
from duplexbench.report.run_card import FIELDS, build as build_card
from duplexbench.storage import Store
from duplexbench.thresholds import Thresholds

TH = Thresholds()


def test_complete_card(tmp_path):
    p = build(tmp_path / "e.sqlite")
    card = build_card(p, "r1", TH)
    for f in FIELDS:
        assert f in card
    assert card["missing"] == []
    assert card["scenario_pack_hash"] == HASHES["pack"]
    assert card["hashes"] == dict(sorted(HASHES.items()))
    assert card["thresholds"] == TH.items() and card["thresholds_hash"] == TH.hash()
    assert card["systems"]["sysA"] == {"config_id": "cfgA",
                                       "preregistered_at": "2026-11-01T00:00:00Z",
                                       "reasoning_effort": "low",
                                       "turn_detection": {"type": "server_vad"}}
    assert card["calibration"]["rtf"] == 0.2
    assert card["refasr"]["floors"]["phone"] == 0.05
    assert str(TH.sim_patience_ms) in card["simulator_patience_rule"]
    assert card["vendors_audio"] == ["vendorA", "vendorB"]


def test_netcheck_overrides_card_baseline(tmp_path):
    p = build(tmp_path / "e.sqlite")
    nc = {"x:1": {"tcp_connect_ms": 1.0}}
    assert build_card(p, "r1", TH, netcheck=nc)["network_baseline"] == nc


def test_missing_fields_listed(tmp_path):
    s = Store(tmp_path / "e.sqlite")
    s.put("runs", {"run_id": "bare", "started_ns": 0,
                   "config_json": json.dumps({"systems": {"x": {"config_id": "c"}}}),
                   "hashes_json": "{}", "calibration_json": json.dumps({"passed": True}),
                   "run_card_json": "{}"})
    s.close()
    card = build_card(tmp_path / "e.sqlite", "bare", TH)
    for f in ("date", "harness_version", "scenario_pack_hash", "refasr", "simulator",
              "network_baseline", "instance_type", "iteration_counts", "vendors_audio",
              "systems.x.preregistered_at", "calibration.rtf"):
        assert f in card["missing"], f
    assert "thresholds" not in card["missing"]


def test_unknown_run(tmp_path):
    p = build(tmp_path / "e.sqlite")
    with pytest.raises(KeyError):
        build_card(p, "nope", TH)
