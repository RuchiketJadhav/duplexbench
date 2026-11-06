"""M1-T14: calibration under the VirtualClock (spec §1.4)."""
from __future__ import annotations

import asyncio
import json

import pytest
import typer
from typer.testing import CliRunner

from duplexbench.calibrate import CalibConfig, calibrate, format_table
from duplexbench.cli_calibrate import register
from duplexbench.thresholds import Thresholds

TH = Thresholds()


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    out = tmp_path_factory.mktemp("cal")
    cfg = CalibConfig.load(calls=3)
    return asyncio.run(calibrate(cfg, virtual=True, th=TH, out_dir=out)), cfg


def test_event_errors_are_zero(result):
    res, _ = result
    for k in ("t_ttfa_recv", "t_stop_model", "t_tool_offset"):
        m = res["metrics"][k]
        assert m["n"] == 3 and m["abs_error_ms_max"] <= 1 and m["pass"], (k, m)


def test_audio_errors_within_one_frame_of_the_buffer_fill(result):
    res, _ = result
    stop = res["metrics"]["t_stop_ear"]
    assert stop["abs_error_ms_max"] <= TH.frame_ms                 # 250 ms stop lands on the next tick
    # T_turn_gap_ear error vs (first_audio + resampler delay) is the jitter-buffer fill: the third
    # real-time frame arrives 2 x 20 ms after the first, plus <= 1 frame of 24k->8k hold-back.
    gap = res["metrics"]["t_turn_gap_ear"]
    fill = TH.jitter_buffer_ms - TH.frame_ms
    assert fill <= gap["error_ms_p50"] <= fill + TH.frame_ms and gap["pass"]


def test_health_and_silero_pass(result):
    res, _ = result
    assert res["health"]["loop_lag_p99_ms"]["value"] == 0.0
    assert res["health"]["pacing_slip_p99_ms"]["value"] == 0.0
    assert res["health"]["silero_disagreement_ms"]["pass"]
    assert res["pass"] is True and res["failed_calls"] == 0


def test_json_has_every_field(result):
    res, _ = result
    on_disk = json.loads(open(res["path"], encoding="utf-8").read())
    for key in ("utc", "host", "clock", "condition", "calls", "failed_calls", "outcomes", "program",
                "thresholds_hash", "metrics", "health", "refasr", "pass"):
        assert key in on_disk, key
    assert set(on_disk["metrics"]) == {"t_ttfa_recv", "t_stop_model", "t_tool_offset",
                                       "t_turn_gap_ear", "t_stop_ear"}
    for m in on_disk["metrics"].values():
        assert {"n", "error_ms_p50", "abs_error_ms_p95", "abs_error_ms_max", "tolerance_ms",
                "pass"} <= set(m)
    assert on_disk["refasr"]["status"] == "skipped" and "M4-T01" in on_disk["refasr"]["reason"]
    assert on_disk["thresholds_hash"] == TH.hash()


def test_tolerances_come_from_thresholds(result):
    res, _ = result
    assert res["metrics"]["t_ttfa_recv"]["tolerance_ms"] == TH.calib_event_tol_ms
    assert res["metrics"]["t_stop_ear"]["tolerance_ms"] == TH.calib_audio_tol_ms + TH.jitter_buffer_ms
    assert res["health"]["silero_disagreement_ms"]["limit"] == TH.silero_disagree_ms


def test_table_renders(result):
    res, _ = result
    t = format_table(res)
    assert "t_turn_gap_ear" in t and "RESULT: PASS" in t and "refasr" in t


def test_cli_register(tmp_path):
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        """test app"""

    register(app)
    r = CliRunner().invoke(app, ["calibrate", "--help"])
    assert r.exit_code == 0 and "--virtual" in r.output
    r = CliRunner().invoke(app, ["calibrate", "--virtual", "--calls", "1", "--out-dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "RESULT: PASS" in r.output and list(tmp_path.glob("calibration-*.json"))
