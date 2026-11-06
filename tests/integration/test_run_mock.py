"""`duplexbench run` end to end against the mock agent under a RealClock (M3-T04). $0, no network."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import soundfile as sf
import yaml
from typer.testing import CliRunner

from duplexbench.cli import app

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "configs" / "runs" / "smoke_mock.yaml"


def _setup(tmp_path: Path, systems: list[str], extra_systems: dict | None = None) -> Path:
    sysdir = tmp_path / "systems"
    sysdir.mkdir()
    shutil.copy(ROOT / "configs" / "systems" / "mock_stops.yaml", sysdir)
    for name, raw in (extra_systems or {}).items():
        (sysdir / f"{name}.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    th = tmp_path / "thresholds.yaml"
    th.write_text("eend_silence_ms: 1500\n", encoding="utf-8")     # short calls for the test
    cfg = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
    cfg.update(systems=systems, systems_dir=str(sysdir), thresholds=str(th),
               results_dir=str(tmp_path / "results"))
    p = tmp_path / "run.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _invoke(*args: str):
    return CliRunner().invoke(app, ["run", *args])


def _empty(d: Path) -> bool:
    return not d.exists() or not any(d.iterdir())


def test_smoke_mock_two_calls_complete(tmp_path):
    cfg = _setup(tmp_path, ["mock_stops"])
    r = _invoke(str(cfg), "--yes", "--allow-unregistered")
    assert r.exit_code == 0, r.output
    assert "total projected: 0.0000 USD" in r.output
    runs = list((tmp_path / "results").iterdir())
    assert len(runs) == 1
    conn = sqlite3.connect(str(runs[0] / "events.sqlite"))
    conn.row_factory = sqlite3.Row
    calls = [dict(c) for c in conn.execute("SELECT * FROM calls ORDER BY call_id")]
    assert [c["outcome"] for c in calls] == ["completed", "completed"]
    assert [c["scenario_id"] for c in calls] == ["smoke_a_001", "smoke_a_002"]
    for c in calls:
        assert c["usd_actual"] == 0.0 and c["usd_projected"] == 0.0
        data, rate = sf.read(c["wav_path"], dtype="int16", always_2d=True)
        assert rate == 8000 and data.shape[1] == 2 and abs(data[:, 1]).max() > 0  # agent heard
        kinds = {r[0] for r in conn.execute("SELECT kind FROM events WHERE call_id = ?",
                                            (c["call_id"],))}
        assert {"session_open", "caller_turn_end", "first_audio_byte", "audio",
                "call_end"} <= kinds
    run = dict(conn.execute("SELECT * FROM runs").fetchone())
    cfgj = json.loads(run["config_json"])
    assert cfgj["baseline_condition"] == "C0" and cfgj["comparisons"] == []
    s = cfgj["systems"]["mock_stops"]
    for k in ("config_id", "preregistered_at", "reasoning_effort", "turn_detection",
              "self_hosted", "components_only", "cancel_event"):
        assert k in s
    assert s["preregistered_at"] is None                     # ran with --allow-unregistered
    card = json.loads(run["run_card_json"])
    assert card["summary"]["outcomes"] == {"completed": 2}
    assert card["systems"]["mock_stops"]["price_ref"] is None
    assert {"pack", "thresholds", "systems"} <= set(json.loads(run["hashes_json"]))
    conn.close()


def test_refuses_unregistered_and_without_yes(tmp_path):
    cfg = _setup(tmp_path, ["mock_stops"])
    r = _invoke(str(cfg), "--yes")
    assert r.exit_code == 4 and "pre-registration" in r.output
    r = _invoke(str(cfg), "--allow-unregistered")
    assert r.exit_code == 1 and "not started" in r.output
    assert _empty(tmp_path / "results")


def test_budget_refusal_for_priced_system(tmp_path):
    priced = yaml.safe_load((ROOT / "configs" / "systems" / "mock_stops.yaml").read_text())
    priced.update(system_id="mock_priced", price_ref="openai_realtime")
    cfg = _setup(tmp_path, ["mock_priced"], {"mock_priced": priced})
    r = _invoke(str(cfg), "--yes", "--allow-unregistered", "--max-usd", "0")
    assert r.exit_code == 3, r.output
    assert "refused: projected" in r.output and "USD/min" in r.output
    assert "developers.openai.com" in r.output                # price source printed
    assert _empty(tmp_path / "results")
