"""fix-30: the run config's simulator / scripted blocks are pre-registered; a change refuses the run."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from duplexbench import preregister as pr
from duplexbench.cli_run import EXIT_PREREG, RunConfig, RunRefused, check_plan
from duplexbench.simulator.caller_llm import prompt_hash

FAKE_KEY = "sk-FAKE-planted-0123456789abcdef"
URL_PASS = "hunter2-url-password"

BASE = {"systems": {"a": "1111"}, "pack": "p0", "langpack": "l0", "thresholds": "t0",
        "private_tree": "unmounted", "harness_commit": None}
RECORD = "preregister-20261107T000000000000Z.json"

SIM = {
    "caller_first": True,
    "refasr": {"candidate": "deepgram",
               "kwargs": {"pinned_model_uuid": "uuid-1", "api_key_env": "DEEPGRAM_API_KEY",
                          "api_key": FAKE_KEY, "endpointing": 10}},
    "llm": {"kind": "openai_compat", "base_url": f"https://user:{URL_PASS}@llm.example/v1?token={FAKE_KEY}",
            "model": "phonellm-alpha-1", "api_key_env": "SIM_LLM_KEY", "api_key": FAKE_KEY,
            "extra_body": {"headers": {"Authorization": f"Bearer {FAKE_KEY}"}}},
    "tts": {"vendor": "fish", "model": "s1", "voice": "v-1", "secret": FAKE_KEY},
}


def _cfg(sim=None, scripted=None):
    d = {"simulator": copy.deepcopy(SIM) if sim is None else sim}
    if scripted is not None:
        d["scripted"] = scripted
    return d


def _hashes(cfg):
    return {**BASE, "run_config": pr.run_config_hashes(cfg)}


def _record(tmp_path, hashes):
    (tmp_path / RECORD).write_text(json.dumps({"schema": 1, "created_utc": "2026-11-07T00:00:00Z",
                                               "hashes": hashes}), encoding="utf-8")


def _refusal(diffs):
    plan = SimpleNamespace(prereg_diffs=diffs, scenarios=[SimpleNamespace(split="private-test")])
    with pytest.raises(RunRefused) as e:
        check_plan(plan, max_usd=0.0, yes=True, allow_unregistered=False)
    assert e.value.code == EXIT_PREREG
    return str(e.value)


def test_fields_are_flat_dotted_paths_and_prompt_is_hashed():
    f = pr.run_config_fields(_cfg(scripted={"lead_in": {"ms": 400}}))
    assert f["simulator.refasr.kwargs.pinned_model_uuid"] == "uuid-1"
    assert f["simulator.refasr.kwargs.api_key_env"] == "DEEPGRAM_API_KEY"   # a variable NAME, kept
    assert f["simulator.llm.model"] == "phonellm-alpha-1"
    assert f["simulator.llm.base_url"] == "https://llm.example/v1"
    assert f["simulator.llm.prompt_sha256"] == prompt_hash()               # default prompt
    assert f["simulator.caller_first"] is True and f["simulator.tts.voice"] == "v-1"
    assert f["scripted.lead_in.ms"] == 400                                 # read generically
    named = pr.run_config_fields({"simulator": {"llm": {"model": "m", "prompt": "caller_v1.md"}}})
    assert named["simulator.llm.prompt_sha256"] == prompt_hash()
    assert "simulator.llm.prompt" not in named
    text = pr.run_config_fields({"simulator": {"llm": {"model": "m", "prompt": "You are a caller."}}})
    assert len(text["simulator.llm.prompt_sha256"]) == 64
    assert "You are a caller." not in json.dumps(text)


def test_identical_blocks_pass(tmp_path):
    _record(tmp_path, _hashes(_cfg(scripted={"lead_in": 1})))
    assert pr.check_against_latest(_hashes(_cfg(scripted={"lead_in": 1})), tmp_path) == []
    # a rotated secret or a different URL password is not a configuration change
    sim = copy.deepcopy(SIM)
    sim["llm"]["api_key"] = "other"
    sim["llm"]["base_url"] = "https://u:other@llm.example/v1?token=zzz"
    assert pr.check_against_latest(_hashes(_cfg(sim, scripted={"lead_in": 1})), tmp_path) == []


def test_absent_on_both_sides_is_equal(tmp_path):
    _record(tmp_path, {**BASE, "run_config": pr.run_config_hashes(None)})
    for cur in (None, {}, {"simulator": {}}, {"simulator": None, "scripted": {}}):
        assert pr.check_against_latest({**BASE, "run_config": pr.run_config_hashes(cur)},
                                       tmp_path) == []
    # a RunConfig always carries its scripted defaults (fix-34), so they are fixed explicitly
    rc = RunConfig(name="n", pack="p", systems=["s"])
    assert pr.run_config_fields(rc) == {"scripted.lead_in": "caller_first"}
    _record(tmp_path, {**BASE, "run_config": pr.run_config_hashes(rc)})
    assert pr.check_against_latest({**BASE, "run_config": pr.run_config_hashes(
        RunConfig(name="other", pack="q", systems=["t"]))}, tmp_path) == []
    diffs = pr.check_against_latest({**BASE, "run_config": pr.run_config_hashes(RunConfig(
        name="n", pack="p", systems=["s"], scripted={"lead_in": "agent_first"}))}, tmp_path)
    assert pr.refusal_message(diffs).endswith("scripted.lead_in changed")


def test_changed_pinned_uuid_refused_with_path(tmp_path):
    _record(tmp_path, _hashes(_cfg()))
    sim = copy.deepcopy(SIM)
    sim["refasr"]["kwargs"]["pinned_model_uuid"] = "uuid-2"
    diffs = pr.check_against_latest(_hashes(_cfg(sim)), tmp_path)
    assert diffs == [f"{RECORD}: run_config: simulator.refasr.kwargs.pinned_model_uuid "
                     "changed: uuid-1 -> uuid-2"]
    msg = pr.refusal_message(diffs)
    assert msg == (f"pre-registration {RECORD} is stale: run-config (1) differ (1 hash): "
                   "simulator.refasr.kwargs.pinned_model_uuid changed")
    assert msg in _refusal(diffs)


def test_changed_caller_llm_model_refused(tmp_path):
    _record(tmp_path, _hashes(_cfg()))
    sim = copy.deepcopy(SIM)
    sim["llm"]["model"] = "gpt-5.4-mini-2026-03-17"
    sim["caller_first"] = False
    sim["tts"]["voice"] = "v-2"
    diffs = pr.check_against_latest({**_hashes(_cfg(sim)), "thresholds": "t1"}, tmp_path)
    msg = _refusal(diffs)
    assert ("is stale: thresholds, run-config (3) differ (4 hashes): simulator.caller_first changed, "
            "simulator.llm.model changed, simulator.tts.voice changed") in msg


def test_scripted_block_added_or_removed_refused(tmp_path):
    _record(tmp_path, _hashes(_cfg()))
    diffs = pr.check_against_latest(_hashes(_cfg(scripted={"lead_in": "beep.wav"})), tmp_path)
    assert pr.refusal_message(diffs).endswith("scripted.lead_in added")
    _record(tmp_path, _hashes(_cfg(scripted={"lead_in": "beep.wav"})))
    diffs = pr.check_against_latest(_hashes(_cfg()), tmp_path)
    assert pr.refusal_message(diffs).endswith("scripted.lead_in removed")


def test_old_record_without_field_refused_as_predates(tmp_path):
    _record(tmp_path, dict(BASE))                       # written before fix-30
    diffs = pr.check_against_latest(_hashes(_cfg()), tmp_path)
    assert diffs == [f"{RECORD}: run_config: predates run-config hashing: re-register"]
    msg = pr.refusal_message(diffs)
    assert msg == f"pre-registration {RECORD} predates run-config hashing: re-register"
    assert msg in _refusal(diffs)
    # even with no simulator block at all (Track A), an old record must be re-registered
    assert pr.check_against_latest({**BASE, "run_config": pr.run_config_hashes(None)}, tmp_path)
    both = pr.check_against_latest({**_hashes(_cfg()), "pack": "p1"}, tmp_path)
    assert pr.refusal_message(both) == (f"pre-registration {RECORD} predates run-config hashing: "
                                        "re-register; also stale: pack differ (1 hash)")


def test_secrets_never_in_record(tmp_path):
    path = pr.write_record(_hashes(_cfg(scripted={"lead_in": {"token": FAKE_KEY}})), tmp_path)
    text = path.read_text(encoding="utf-8")
    assert FAKE_KEY not in text and URL_PASS not in text and "Bearer" not in text
    assert "SIM_LLM_KEY" in text and "DEEPGRAM_API_KEY" in text


def test_cli_preregister_run_config(tmp_path, monkeypatch):
    from duplexbench.cli import app
    monkeypatch.delenv("DUPLEX_PRIVATE_DIR", raising=False)
    monkeypatch.setenv("SIM_LLM_KEY", FAKE_KEY)          # never read into the record
    run = tmp_path / "run.yaml"
    raw = {"name": "t", "pack": "tests/fixtures/scenarios/smoke", "systems": ["mock_stops"],
           "results_dir": str(tmp_path / "res"), "simulator": _cfg()["simulator"]}
    run.write_text(yaml.safe_dump(raw), encoding="utf-8")
    r = CliRunner().invoke(app, ["preregister", "--run-config", str(run)])
    assert r.exit_code == 0, r.output
    recs = list((tmp_path / "res").glob("preregister-*.json"))
    assert len(recs) == 1
    text = recs[0].read_text(encoding="utf-8")
    assert FAKE_KEY not in text and URL_PASS not in text and FAKE_KEY not in r.output
    hashes = json.loads(text)["hashes"]
    assert hashes["run_config"]["fields"]["simulator.refasr.kwargs.pinned_model_uuid"] == "uuid-1"
    # what cli run computes for the same config matches the record
    cur, rd = pr.collect_for_run_config(run)
    assert rd == tmp_path / "res" and pr.check_against_latest(cur, rd) == []
    r = CliRunner().invoke(app, ["preregister", "--run-config", str(run), "--check"])
    assert r.exit_code == 0 and "matches the latest pre-registration" in r.output
    raw["simulator"]["llm"]["model"] = "other"
    run.write_text(yaml.safe_dump(raw), encoding="utf-8")
    r = CliRunner().invoke(app, ["preregister", "--run-config", str(run), "--check"])
    assert r.exit_code == 1 and "simulator.llm.model changed" in r.output
