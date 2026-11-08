"""`duplexbench run` on a Track C simulated scenario against the mock agent (M4-T12). $0, no network.

MockAgent `filler_then_result` + fixture `track_c_001` + a fake caller LLM + FakeTTS,
calibration refasr stage from FakeASR, RealClock (cli_run's call clock), 1 trial:
the run writes a calls row, sim_turns rows and tool_calls rows, and `score_run`
produces pass_at_1 and t_tool_to_info_ear observations.

track_c_001's tool names (find_booking / check_availability / reschedule_booking)
are not tools of the scheduling mock backend, so the test copies the fixture and
maps its tools onto the backend's (find_slots / book_appointment) and points
required_spoken.after_tool at book_appointment (fix-33 validates it); everything else
(persona, goal, steps, entities, required_spoken content) is the fixture's.
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
    # fix-33: validate checks required_spoken.after_tool against the scenario's tools
    raw["required_spoken"] = [dict(rs, after_tool="book_appointment") for rs in raw["required_spoken"]]
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
    # fix-28: the three parts are printed and stored, and the mock Track C total stays 0.0
    assert "  agent: 0.0000 USD (the system lines above)" in lines
    assert "  reference ASR: 0.0000 USD  (fake:0: not a paid engine)" in lines
    assert ("  caller LLM: 0.0000 USD  (injected client: billing external "
            "(0 USD in the harness))") in lines
    assert "  total projected: 0.0000 USD" in lines
    assert calls[0]["usd_projected"] == 0.0 and calls[0]["usd_actual"] == 0.0
    zero = {"agent": 0.0, "refasr": 0.0, "caller_llm": 0.0}
    assert flags["cost"]["projected"] == zero and flags["cost"]["actual"] == zero
    assert flags["cost"]["caller_llm_billing"] == "external"
    assert out["usd_projected_total"] == 0.0 and out["usd_actual_parts"] == zero
    conn.close()
    shutil.copy(db, tmp_path / "scored.sqlite")
    # fix-23: the ear channel of the real recording through a FakeASR, words on the call clock
    from duplexbench import cli_score
    ear2 = FakeASR([Word("one", 0, 200), Word("moment", 300, 700)])
    jobs = cli_score.plan_ear_jobs(tmp_path / "scored.sqlite", run_id, ear2, {})
    assert jobs and all(j.error is None and j.audio_s > 0 for j in jobs), [j.error for j in jobs]
    words = {j.call_id: cli_score.transcribe_ear(ear2, j.pcm, j.t0_ms) for j in jobs}
    assert all(w[0].start_ms >= round(j.t0_ms) for j, w in zip(jobs, words.values()))
    bare = score_run(tmp_path / "scored.sqlite", run_id, Thresholds())
    assert any("no_refasr" in o.tags for o in bare if o.metric == "pass_at_1")
    obs = score_run(tmp_path / "scored.sqlite", run_id, Thresholds(), refasr_words=words)
    mine = [o for o in obs if o.call_id == calls[0]["call_id"]]
    metrics = {o.metric for o in mine}
    assert {"pass_at_1", "t_tool_to_info_ear"} <= metrics, sorted(metrics)
    offs = [o for o in mine if o.metric == "t_tool_offset"]
    assert offs and all("domain:scheduling" in o.tags for o in offs)   # task.py's copy only
    assert len({o.event_id for o in offs}) == len(offs)
    p1 = next(o for o in mine if o.metric == "pass_at_1")
    assert p1.valid and p1.value in (0.0, 1.0)              # final_state event present
    assert "no_refasr" not in p1.tags


# -- sim-llm: simulator.llm wiring (no network: OpenAICompatClient._create is patched) --
SIM_LLM = {"kind": "openai_compat", "base_url": "http://127.0.0.1:9/v1",
           "model": "pipecat-ai/phonellm-alpha-1", "temperature": 0, "send_temperature": True,
           "max_tokens": 400, "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def _with_simulator(cfg_path: Path, simulator: dict) -> Path:
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["simulator"] = simulator
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg_path


def test_refused_without_simulator_llm(tmp_path):
    cfg = _setup(tmp_path, refasr=_refasr_pass())
    with pytest.raises(cli_run.RunRefused) as e:
        cli_run.run_command(cfg, yes=True, max_usd=None, allow_unregistered=True,
                            echo=lambda *_: None, sim_tts=FakeTTS(), sim_refasr=FakeASR([]))
    assert e.value.code == cli_run.EXIT_CONFIG == 2
    assert "set simulator.llm {kind, base_url, model} in the run config" in str(e.value)


def test_bad_simulator_llm_is_config_error(tmp_path):
    cfg = _with_simulator(_setup(tmp_path, refasr=_refasr_pass()),
                          {"llm": dict(SIM_LLM, kind="nope")})
    with pytest.raises(cli_run.RunRefused) as e:
        cli_run.run_command(cfg, yes=True, max_usd=None, allow_unregistered=True,
                            echo=lambda *_: None, sim_tts=FakeTTS(), sim_refasr=FakeASR([]))
    assert e.value.code == cli_run.EXIT_CONFIG
    assert str(e.value).startswith("config error: simulator.llm: unknown kind")


def test_simulator_llm_configured_runs_without_network(tmp_path, monkeypatch):
    import re

    from duplexbench.simulator.openai_compat import ChatResponse, OpenAICompatClient
    seen: list[dict] = []

    async def fake_create(self, req):
        seen.append({"url": self.url, "body": self.body(req)})
        n = int(re.search(r"Your turn number: (\d+)", req["messages"][-1]["content"]).group(1))
        text = UTTS[n - 1] if n <= len(UTTS) else END
        return ChatResponse([{"type": "text", "text": text}], "end_turn", "stop",
                            {"input_tokens": 1, "output_tokens": 1}, self.model)

    monkeypatch.setattr(OpenAICompatClient, "_create", fake_create)
    cfg = _with_simulator(_setup(tmp_path, refasr=_refasr_pass()), {"llm": SIM_LLM})
    lines: list[str] = []
    out = cli_run.run_command(cfg, yes=True, max_usd=None, allow_unregistered=True,
                              echo=lines.append, sim_tts=FakeTTS(),
                              sim_refasr=FakeASR([Word("one", 0, 200)]))
    assert seen, chr(10).join(lines)
    b = seen[0]["body"]
    assert seen[0]["url"] == "http://127.0.0.1:9/v1/chat/completions"
    assert b["model"] == SIM_LLM["model"] and b["temperature"] == 0
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    conn = sqlite3.connect(str(out["db"]))
    flags = json.loads(conn.execute("SELECT flags_json FROM calls ORDER BY call_id").fetchone()[0])
    sims = conn.execute("SELECT COUNT(*) FROM sim_turns").fetchone()[0]
    card = json.loads(conn.execute("SELECT run_card_json FROM runs").fetchone()[0])
    conn.close()
    assert flags["simulator"]["llm"] == {"kind": "openai_compat", "model": SIM_LLM["model"],
                                         "base_url": "http://127.0.0.1:9/v1",
                                         "billing": "external"}
    assert sims >= 2
    # fix-27: the run card discloses the simulator setup (Track C only run: no scripted block)
    from duplexbench.simulator.caller_llm import prompt_hash
    assert card["simulator"] == {"simulated": {
        "caller_first": True,
        "llm": {"kind": "openai_compat", "model": SIM_LLM["model"],
                "base_url": "http://127.0.0.1:9/v1", "billing": "external",
                "prompt_sha256": prompt_hash()},
        "refasr": card["summary"]["refasr_version_final"],
        "tts": {"vendor": "fake", "voices": ["fixture-voice-c"]}}}
    assert card["simulator"]["simulated"]["refasr"].startswith("fake")


# -- fix-28: paid reference ASR + per-token caller LLM in the projection and actual cost --
class PaidFakeASR(FakeASR):
    """FakeASR billed like Deepgram Nova-3 (rate from configs/prices/deepgram_nova3.yaml)."""
    paid = True
    PRICE_REF = "deepgram_nova3"

    def usd_per_audio_min(self) -> float:
        from duplexbench.refasr.candidates.deepgram import usd_per_audio_min
        return usd_per_audio_min()


GPT_MINI = "gpt-5.4-mini-2026-03-17"


def _paid_cfg(tmp_path: Path, monkeypatch) -> Path:
    import re

    from duplexbench.simulator.openai_compat import ChatResponse, OpenAICompatClient

    async def fake_create(self, req):
        n = int(re.search(r"Your turn number: (\d+)", req["messages"][-1]["content"]).group(1))
        text = UTTS[n - 1] if n <= len(UTTS) else END
        return ChatResponse([{"type": "text", "text": text}], "end_turn", "stop",
                            {"input_tokens": 1500, "output_tokens": 60}, self.model)

    monkeypatch.setattr(OpenAICompatClient, "_create", fake_create)
    return _with_simulator(_setup(tmp_path, refasr=_refasr_pass()),
                           {"llm": dict(SIM_LLM, model=GPT_MINI, price_ref="openai_chat")})


def test_track_c_paid_parts_projected_and_actual(tmp_path, monkeypatch):
    from duplexbench.metrics import cost
    cfg = _paid_cfg(tmp_path, monkeypatch)
    plan = cli_run.plan_run(cli_run.RunConfig.load(cfg))
    minutes = plan.calls[0].minutes                     # cost.est_call_minutes of track_c_001
    ra_p = minutes * 0.0077
    llm_p = (3 + 2) * (1500 * 0.75 + 60 * 4.50) / 1e6   # 3 steps + 2 turns, [Assumption] tokens
    lines: list[str] = []
    out = cli_run.run_command(cfg, yes=True, max_usd=1.0, allow_unregistered=True,
                              echo=lines.append, sim_tts=FakeTTS(),
                              sim_refasr=PaidFakeASR([Word("one", 0, 200)]))
    log = chr(10).join(lines)
    assert "  agent: 0.0000 USD (the system lines above)" in lines, log
    assert (f"  reference ASR: {ra_p:.4f} USD  (fake:0: 1 Track C calls x {minutes:.2f} est. "
            f"min total x 0.0077 USD/audio min [https://deepgram.com/pricing checked "
            f"2026-11-06])") in lines, log
    assert (f"  caller LLM: {llm_p:.4f} USD  ({GPT_MINI}: 5 caller turns x (1500 in x 0.75 + "
            f"60 out x 4.5) USD/1M tokens, token counts [Assumption] "
            f"[https://platform.openai.com/docs/pricing checked 2026-11-06])") in lines, log
    assert f"  total projected: {ra_p + llm_p:.4f} USD" in lines, log
    conn = sqlite3.connect(str(out["db"]))
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM calls ORDER BY call_id").fetchone())
    conn.close()
    flags = json.loads(row["flags_json"])
    c = flags["cost"]
    assert c["projected"]["agent"] == 0.0
    assert c["projected"]["refasr"] == pytest.approx(ra_p)
    assert c["projected"]["caller_llm"] == pytest.approx(llm_p)
    assert row["usd_projected"] == pytest.approx(ra_p + llm_p)
    dur_min = (row["ended_ns"] - row["started_ns"]) / 6e10       # recorded call duration
    assert c["refasr_audio_min"] == pytest.approx(dur_min, abs=1e-6)
    assert c["actual"]["refasr"] == pytest.approx(dur_min * 0.0077)
    n = c["caller_llm_usage"]["responses"]
    assert n >= 2 and c["caller_llm_usage"] == {"input_tokens": 1500 * n,
                                                "output_tokens": 60 * n, "responses": n}
    assert c["actual"]["caller_llm"] == pytest.approx(n * (1500 * 0.75 + 60 * 4.50) / 1e6)
    assert row["usd_actual"] == pytest.approx(c["actual"]["refasr"] + c["actual"]["caller_llm"])
    assert c["caller_llm_billing"] == "per_token" and c["refasr_billed"] is True
    assert flags["simulator"]["llm"]["billing"] == "per_token"
    assert flags["simulator"]["llm"]["price_ref"] == "openai_chat"
    assert "llm_usage" not in flags["simulator"]
    assert out["usd_projected_parts"]["refasr"] == pytest.approx(ra_p, abs=1e-6)
    assert out["usd_actual_total"] >= row["usd_actual"] - 1e-6
    assert cost.sum_chat_usage([]) == {"input_tokens": 0, "output_tokens": 0, "responses": 0}


def test_track_c_paid_total_exceeds_budget(tmp_path, monkeypatch):
    """The plan total (agent + reference ASR + caller LLM) is what max_usd is checked against."""
    cfg = _paid_cfg(tmp_path, monkeypatch)
    with pytest.raises(cli_run.RunRefused) as e:
        cli_run.run_command(cfg, yes=True, max_usd=0.001, allow_unregistered=True,
                            echo=lambda *_: None, sim_tts=FakeTTS(),
                            sim_refasr=PaidFakeASR([]))
    assert e.value.code == cli_run.EXIT_BUDGET
