"""`duplexbench run` on a Track C simulated scenario against the mock agent (M4-T12). $0, no network.

MockAgent `filler_then_result` + fixture `track_c_001` + a fake caller LLM + FakeTTS,
calibration refasr stage from FakeASR, RealClock (cli_run's call clock), 1 trial:
the run writes a calls row, sim_turns rows and tool_calls rows, and `score_run`
produces pass_at_1 and t_tool_to_info_ear observations.

track_c_001's tool names (find_booking / check_availability / reschedule_booking)
are not tools of the scheduling mock backend, so the test copies the fixture and
maps its tools onto the backend's (find_slots / book_appointment); everything else
(persona, goal, steps, entities, required_spoken) is the fixture's.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from duplexbench import cli_run
from duplexbench.calibrate import DEFAULT_REFASR_CLIP, refasr_result
from duplexbench.cli_score import score_run
from duplexbench.refasr.engine import FakeASR, Word
from duplexbench.simulator.caller_llm import END
from duplexbench.stimuli.fake_tts import FakeTTS
from duplexbench.thresholds import Thresholds

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "scenarios" / "public-dev" / "track_c_001.yaml"
UTTS = ["Hi, I need to move my cleaning, booking B K four four seven one.",
        "The first slot please.", "Thanks, bye."]


class FakeLLM:
    """Line = caller turns so far; END after the script (context-keyed, no network)."""
    prompt_hash = "ph-fake"
    config_hash = "cfg-fake"

    def __init__(self):
        self.calls = 0

    async def next_utterance(self, ctx):
        self.calls += 1
        i = ctx.caller_turns
        return UTTS[i] if i < len(UTTS) else END


def _pack(tmp: Path) -> Path:
    raw = yaml.safe_load(FIX.read_text(encoding="utf-8"))
    raw["tools"] = [
        {"name": "find_slots", "kind": "read", "latency_ms": 2000, "failure": "none",
         "result_anchors": ["9:00", "nine"], "anchor_step": "s2"},
        {"name": "book_appointment", "kind": "write", "latency_ms": 0, "failure": "none",
         "result_anchors": ["booked"]},
    ]
    raw["expected_state"] = {"slots": {"S1": {"booked_by": None}}}
    raw["trials"] = 1
    pack = tmp / "pack"
    pack.mkdir()
    (pack / "track_c_001.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return pack


def _setup(tmp: Path, refasr: dict | None) -> Path:
    sysdir = tmp / "systems"
    sysdir.mkdir()
    sys_raw = yaml.safe_load((ROOT / "configs/systems/mock_stops.yaml").read_text(encoding="utf-8"))
    sys_raw["system_id"] = "mock_filler"
    sys_raw["params"]["mock"].update(behaviour="filler_then_result", tool_at_ms=600,
                                     reply_len_ms=800, tool_name="find_slots",
                                     tool_args={"date": "2026-11-10"})
    (sysdir / "mock_filler.yaml").write_text(yaml.safe_dump(sys_raw), encoding="utf-8")
    res = tmp / "results"
    res.mkdir()
    if refasr is not None:
        (res / "calibration-20261105T000000Z.json").write_text(
            json.dumps({"pass": True, "host": "test", "refasr": refasr}), encoding="utf-8")
    cfg = {"name": "track_c_mock", "pack": str(_pack(tmp)), "track": "C",
           "systems": ["mock_filler"], "systems_dir": str(sysdir), "conditions": ["C0"],
           "trials": 1, "max_usd": 0.0, "results_dir": str(res), "seed": 3}
    p = tmp / "run.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _refasr_pass() -> dict:
    words = [Word("hello", 100, 400), Word("world", 500, 900)]
    r = refasr_result(FakeASR(words), DEFAULT_REFASR_CLIP, Thresholds())
    assert r["passed"] is True, r
    return r


def test_refused_without_refasr_calibration(tmp_path):
    cfg = _setup(tmp_path, refasr=None)
    with pytest.raises(cli_run.RunRefused) as e:
        cli_run.run_command(cfg, yes=True, max_usd=None, allow_unregistered=True,
                            echo=lambda *_: None, sim_llm=FakeLLM())
    assert e.value.code == cli_run.EXIT_PREREG and "D4" in str(e.value)
    failed = dict(_refasr_pass(), passed=False)
    (tmp_path / "b").mkdir()
    cfg2 = _setup(tmp_path / "b", refasr=failed)
    with pytest.raises(cli_run.RunRefused) as e2:
        cli_run.run_command(cfg2, yes=True, max_usd=None, allow_unregistered=True,
                            echo=lambda *_: None, sim_llm=FakeLLM())
    assert e2.value.code == cli_run.EXIT_PREREG and "passed=False" in str(e2.value)


def test_refused_without_simulator_refasr(tmp_path):
    """fix-20: no injected engine and no simulator.refasr -> refused before any call."""
    cfg = _setup(tmp_path, refasr=_refasr_pass())
    with pytest.raises(cli_run.RunRefused) as e:
        cli_run.run_command(cfg, yes=True, max_usd=None, allow_unregistered=True,
                            echo=lambda *_: None, sim_llm=FakeLLM(), sim_tts=FakeTTS())
    assert e.value.code == cli_run.EXIT_CONFIG and "simulator.refasr" in str(e.value)


def test_build_refasr_config_key():
    assert cli_run.build_refasr({}) is None
    with pytest.raises(ValueError):
        cli_run.build_refasr({"refasr": {"candidate": "no-such-model"}})


def test_track_c_mock_run_rows_and_score(tmp_path):
    cfg = _setup(tmp_path, refasr=_refasr_pass())
    lines: list[str] = []
    ear = FakeASR([Word("one", 0, 200), Word("moment", 300, 700)])
    out = cli_run.run_command(cfg, yes=True, max_usd=None, allow_unregistered=True,
                              echo=lines.append, sim_llm=FakeLLM(), sim_tts=FakeTTS(),
                              sim_refasr=ear)
    db, run_id = out["db"], out["run_id"]
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    calls = [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY call_id")]
    assert calls and calls[0]["scenario_id"] == "track_c_001", "\n".join(lines)
    assert calls[0]["outcome"] in ("completed", "validation_failed"), "\n".join(lines)
    flags = json.loads(calls[0]["flags_json"])
    assert flags["scored"] is (len(calls) == 1) and flags["regenerated"] == 0
    sims = [dict(r) for r in conn.execute("SELECT * FROM sim_turns WHERE call_id = ? AND "
                                          "discarded = 0", (calls[0]["call_id"],))]
    assert len(sims) >= 2 and sims[0]["prompt_hash"] == "ph-fake"
    tools = [dict(r) for r in conn.execute("SELECT * FROM tool_calls WHERE call_id = ?",
                                           (calls[0]["call_id"],))]
    assert tools and tools[0]["name"] == "find_slots"
    assert tools[0]["injected_latency_ms"] == 2000              # one row, written by the ToolServer
    assert len({t["tool_call_id"] for t in tools}) == len(tools)
    kinds = {r[0] for r in conn.execute("SELECT kind FROM events WHERE call_id = ?",
                                        (calls[0]["call_id"],))}
    assert {"final_state", "tool_call", "tool_result", "caller_turn_end", "mishearing"} <= kinds
    mh = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload_json FROM events WHERE call_id = ? AND kind = 'mishearing'",
        (calls[0]["call_id"],))]
    assert len(mh) == 1 and mh[0]["refasr_text"] == "one moment"     # heard on the ear
    assert mh[0]["provider_text"] == "" and mh[0]["refasr"] == "fake:0"  # mock: no transcript
    conn.close()
    shutil.copy(db, tmp_path / "scored.sqlite")
    obs = score_run(tmp_path / "scored.sqlite", run_id, Thresholds())
    mine = [o for o in obs if o.call_id == calls[0]["call_id"]]
    metrics = {o.metric for o in mine}
    assert {"pass_at_1", "t_tool_to_info_ear"} <= metrics, sorted(metrics)
    offs = [o for o in mine if o.metric == "t_tool_offset"]
    assert offs and all("domain:scheduling" in o.tags for o in offs)   # task.py's copy only
    assert len({o.event_id for o in offs}) == len(offs)
    p1 = next(o for o in mine if o.metric == "pass_at_1")
    assert p1.valid and p1.value in (0.0, 1.0)              # final_state event present
