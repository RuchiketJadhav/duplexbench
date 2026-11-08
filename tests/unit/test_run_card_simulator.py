"""fix-27: cli_run.simulator_card, the run card `simulator` block (spec §7, §10 item 1)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from duplexbench import cli_run
from duplexbench.scenarios.loader import load_pack
from duplexbench.stimuli.fake_tts import FakeTTS

ROOT = Path(__file__).resolve().parents[2]
PACK = load_pack(ROOT / "scenarios" / "public-dev")


class _Engine:
    name, version = "deepgram", "nova-3/unpinned"


def _plan(ids, *, simulator=None, sim_llm_info=None, sim_tts=None, sim_refasr=None,
          fields=None):
    scen = [s for s in PACK.scenarios if s.id in ids]
    cfg = cli_run.RunConfig.model_validate({"name": "x", "pack": "p", "systems": ["s"],
                                            "simulator": simulator or {}})
    return SimpleNamespace(cfg=cfg, pack=PACK, scenarios=scen, sim_llm=None,
                           sim_llm_info=sim_llm_info, sim_tts=sim_tts, sim_refasr=sim_refasr,
                           hashes={"run_config": {"fields": fields or {}}})


def test_scripted_only_run_shows_lead_in_and_clip_tts():
    card = cli_run.simulator_card(_plan({"pd_a_001", "pd_a_002", "pd_d_001"}))
    assert set(card) == {"scripted"}
    s = card["scripted"]
    assert s["lead_in"] == "caller_first"
    assert s["tts"]["source"] == "stimulus_clips" and s["tts"]["vendors"] == ["fish"]
    assert s["tts"]["voices"] and all(v.startswith("voice-") for v in s["tts"]["voices"])


def test_track_c_run_discloses_llm_refasr_tts_caller_first():
    info = {"kind": "openai_compat", "model": "m", "base_url": "http://h/v1",
            "billing": "per_token", "price_ref": "openai_chat"}
    plan = _plan({"pd_c_001", "pd_c_002"}, simulator={"caller_first": False},
                 sim_llm_info=info, sim_tts=FakeTTS(), sim_refasr=_Engine(),
                 fields={"simulator.llm.prompt_sha256": "abc"})
    card = cli_run.simulator_card(plan)
    assert card == {"simulated": {
        "caller_first": False,
        "llm": {**info, "prompt_sha256": "abc"},
        "refasr": "deepgram:nova-3/unpinned",
        "tts": {"vendor": "fake", "voices": ["voice-f2", "voice-m1"]}}} or \
        card["simulated"]["tts"]["voices"] == sorted({s.caller_voice for s in plan.scenarios})
    final = cli_run.simulator_card(plan, {"refasr_version_final": "deepgram:nova-3/uuid-1"})
    assert final["simulated"]["refasr"] == "deepgram:nova-3/uuid-1"


def test_mixed_run_has_both_blocks():
    card = cli_run.simulator_card(_plan({"pd_a_001", "pd_c_001"}))
    assert set(card) == {"scripted", "simulated"}
    assert card["simulated"]["llm"]["model"] is None          # no simulator.llm: shown as null
    assert card["simulated"]["tts"] == {"vendor": "fake", "voices": ["voice-f2"]}
