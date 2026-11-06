"""`duplexbench run` on a Track A overlay scenario: the overlay engine and the control
twin run through cli_run exactly as in the golden M2 (overlays.scripted_run). $0, RealClock.

Fixture track_a_002 (Y-INT, H-BC, P-PAUSE, wait-class Y-CMD, E-END) copied into a
temporary pack with its resumption clips under the `<clip stem>_resumption<ext>`
convention (BUILD_PLAN §2.5 note; the golden reuses a002_s2.wav the same way).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import yaml

from duplexbench import cli_run

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "scenarios" / "public-dev"


def _pack(tmp: Path) -> Path:
    pack = tmp / "pack"
    (pack / "audio").mkdir(parents=True)
    raw = yaml.safe_load((FIX / "track_a_002.yaml").read_text(encoding="utf-8"))
    raw.update(conditions=["C0"], trials=1)
    (pack / "track_a_002.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    for f in (FIX / "audio").glob("a002_*.wav"):
        shutil.copy(f, pack / "audio" / f.name)
    shutil.copy(FIX / "audio" / "a002_s2.wav", pack / "audio" / "a002_s1_resumption.wav")
    shutil.copy(FIX / "audio" / "a002_s2.wav", pack / "audio" / "a002_bc_resumption.wav")
    return pack


def test_track_a_overlays_and_control_twin(tmp_path):
    sysdir = tmp_path / "systems"
    sysdir.mkdir()
    shutil.copy(ROOT / "configs" / "systems" / "mock_stops.yaml", sysdir)
    cfg = {"name": "track_a_ovl", "pack": str(_pack(tmp_path)), "track": "A",
           "systems": ["mock_stops"], "systems_dir": str(sysdir), "conditions": ["C0"],
           "trials": 1, "max_usd": 0.0, "results_dir": str(tmp_path / "results"), "seed": 11}
    p = tmp_path / "run.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    lines: list[str] = []
    out = cli_run.run_command(p, yes=True, max_usd=None, allow_unregistered=True,
                              echo=lines.append)
    conn = sqlite3.connect(str(out["db"]))
    calls = conn.execute("SELECT call_id, is_control, outcome FROM calls ORDER BY call_id").fetchall()
    assert calls == [("mock_stops-track_a_002-C0-t0", 0, "completed"),
                     ("mock_stops-track_a_002-C0-t0-ctl", 1, "completed")], "\n".join(lines)
    ovs = {}
    for cid, oid, cls, tags in conn.execute("SELECT call_id, overlay_id, cls, tags_json FROM overlays"):
        ovs.setdefault(cid, {})[oid] = (cls, json.loads(tags or "[]"))
    ovl, ctl = (ovs.get(c[0], {}) for c in calls)
    assert set(ovl) >= {"o1", "o2", "o3", "o4", "o5"}, ovl          # every overlay has a row
    assert {"o3", "o5"} <= set(ctl)        # the twin keeps the caller's own P-PAUSE / E-END
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM events WHERE call_id = ?",
                                        (calls[0][0],))}
    assert kinds & {"overlay_injected", "overlay_not_injected"}
    run = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
    assert "track_a_002" in run["scenarios"]                         # cli score reads it
    conn.close()
