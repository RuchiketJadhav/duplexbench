"""fix-28: Track C simulated-caller costs (reference ASR + caller LLM) in the projection
and the actual cost. No network; FakeASR marked paid by a small stub."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from duplexbench import cli_run
from duplexbench.metrics import cost
from duplexbench.refasr.candidates import deepgram
from duplexbench.refasr.engine import FakeASR
from duplexbench.simulator.caller_llm import CallerLLM, SimContext
from duplexbench.systems import load_prices

MODEL = "gpt-5.4-mini-2026-03-17"


class PaidFakeASR(FakeASR):
    """FakeASR billed like Deepgram (the rate is the one in deepgram_nova3.yaml)."""
    paid = True
    PRICE_REF = "deepgram_nova3"

    def usd_per_audio_min(self) -> float:
        return deepgram.usd_per_audio_min()


def test_openai_chat_price_file_contract():
    p = load_prices("openai_chat")
    assert p["price_ref"] == "openai_chat" and p["checked"] == "2026-11-06"
    assert p["source_url"] == "https://platform.openai.com/docs/pricing"
    assert p["rates"][MODEL] == {"input_per_1m": 0.75, "output_per_1m": 4.50}
    assert p["projection"] == {"input_tokens_per_turn": 1500, "output_tokens_per_turn": 60,
                               "extra_turns_per_call": 2}


def test_refasr_call_usd_is_minutes_times_rate():
    assert deepgram.usd_per_audio_min() == pytest.approx(0.0077)
    assert cost.refasr_call_usd(3.0, deepgram.usd_per_audio_min()) == pytest.approx(0.0231)
    assert cost.refasr_call_usd(-1.0, 0.0077) == 0.0


def test_caller_llm_projection_hand_value():
    p = load_prices("openai_chat")
    # 3 steps + 2 = 5 turns x (1500 x 0.75 + 60 x 4.50) / 1e6 = 5 x 1395e-6
    assert cost.caller_llm_turns(3, p) == 5
    assert cost.caller_llm_projected_usd(3, p, MODEL) == pytest.approx(0.006975)
    assert cost.caller_llm_projected_usd(3, None, MODEL) == 0.0
    with pytest.raises(ValueError, match="no input_per_1m"):
        cost.caller_llm_projected_usd(3, p, "gpt-unknown")


def test_caller_llm_actual_from_usage():
    p = load_prices("openai_chat")
    usages = [{"input_tokens": 1000, "output_tokens": 100},
              {"prompt_tokens": 500, "completion_tokens": 20}]       # raw OpenAI shape too
    assert cost.sum_chat_usage(usages) == {"input_tokens": 1500, "output_tokens": 120,
                                           "responses": 2}
    assert cost.caller_llm_actual_usd(usages, p, MODEL) == pytest.approx(
        (1500 * 0.75 + 120 * 4.50) / 1e6)
    assert cost.caller_llm_actual_usd(usages, None, MODEL) == 0.0
    assert cost.caller_llm_actual_usd([], p, MODEL) == 0.0


def test_simulator_costs_paid_flag_sources():
    free = cli_run.simulator_costs({}, FakeASR([]))
    assert free["refasr"]["paid"] is False and free["refasr"]["usd_per_audio_min"] == 0.0
    assert free["caller_llm"]["billing"] == "external"
    paid = cli_run.simulator_costs({}, PaidFakeASR([]))
    assert paid["refasr"]["paid"] is True
    assert paid["refasr"]["usd_per_audio_min"] == pytest.approx(0.0077)
    # configured candidate, no engine built: Candidate.paid + the module's price
    cfgd = cli_run.simulator_costs({"refasr": {"candidate": "deepgram"}})
    assert cfgd["refasr"]["paid"] is True and cfgd["refasr"]["price_ref"] == "deepgram_nova3"
    assert cfgd["refasr"]["usd_per_audio_min"] == pytest.approx(0.0077)
    assert cfgd["refasr"]["checked"] == "2026-11-06"
    local = cli_run.simulator_costs({"refasr": {"candidate": "faster-whisper"}})
    assert local["refasr"]["paid"] is False
    llm = cli_run.simulator_costs({"llm": {"model": MODEL, "price_ref": "openai_chat"}})
    assert llm["caller_llm"]["billing"] == "per_token"
    assert llm["caller_llm"]["input_per_1m"] == 0.75
    with pytest.raises(ValueError):
        cli_run.simulator_costs({"llm": {"model": "nope", "price_ref": "openai_chat"}})


def _plan(sim_cfg: dict, n_steps: int = 3, minutes: float = 2.0, agent_usd: float = 0.5):
    scn = SimpleNamespace(source="simulated", steps=[object()] * n_steps)
    a = cli_run.CallPlan("c-t0", "sys", scn, "C0", 0, minutes, agent_usd, 0)
    scripted = cli_run.CallPlan("a-t0", "sys", SimpleNamespace(source="scripted", steps=[]),
                                "C0", 0, 1.0, 0.25, 1)
    plan = SimpleNamespace(cfg=SimpleNamespace(simulator=sim_cfg), calls=[a, scripted],
                           projection={"sys": {"usd": 0.75}}, sim_cost=None)
    return plan, a, scripted


def test_apply_simulator_costs_parts_and_total():
    plan, a, scripted = _plan({"llm": {"model": MODEL, "price_ref": "openai_chat"}})
    cli_run.apply_simulator_costs(plan, PaidFakeASR([]))
    ra, llm = 2.0 * 0.0077, 5 * (1500 * 0.75 + 60 * 4.50) / 1e6
    assert a.usd_parts["agent"] == 0.5
    assert a.usd_parts["refasr"] == pytest.approx(ra)
    assert a.usd_parts["caller_llm"] == pytest.approx(llm)
    assert a.usd_parts["caller_turns"] == 5
    assert a.projected_usd == pytest.approx(0.5 + ra + llm)
    assert scripted.usd_parts is None and scripted.projected_usd == 0.25   # Track A untouched
    assert plan.projection["sys"]["usd"] == 0.75                            # agent part
    assert plan.projection["sys"]["usd_total"] == pytest.approx(0.75 + ra + llm, abs=1e-6)
    cli_run.apply_simulator_costs(plan, PaidFakeASR([]))                     # idempotent
    assert a.projected_usd == pytest.approx(0.5 + ra + llm)
    lines = cli_run.simulator_cost_lines(plan)
    assert lines[0] == "  agent: 0.7500 USD (the system lines above)"
    assert lines[1].startswith(f"  reference ASR: {ra:.4f} USD  (fake:0: 1 Track C calls x "
                               "2.00 est. min total x 0.0077 USD/audio min")
    assert lines[2].startswith(f"  caller LLM: {llm:.4f} USD  ({MODEL}: 5 caller turns x "
                               "(1500 in x 0.75 + 60 out x 4.5) USD/1M tokens")


def test_apply_simulator_costs_free_is_zero():
    plan, a, _ = _plan({}, agent_usd=0.0)
    cli_run.apply_simulator_costs(plan, FakeASR([]))
    assert a.projected_usd == 0.0
    assert a.usd_parts == {"agent": 0.0, "refasr": 0.0, "caller_llm": 0.0, "caller_turns": None}
    lines = cli_run.simulator_cost_lines(plan)
    assert lines[1] == "  reference ASR: 0.0000 USD  (fake:0: not a paid engine)"
    assert lines[2] == ("  caller LLM: 0.0000 USD  (injected client: billing external "
                        "(0 USD in the harness))")


def test_budget_reserves_the_total():
    from duplexbench.budget import Budget, BudgetExceeded
    plan, a, _ = _plan({"llm": {"model": MODEL, "price_ref": "openai_chat"}}, agent_usd=0.0)
    cli_run.apply_simulator_costs(plan, PaidFakeASR([]))
    b = Budget(max_usd=a.projected_usd - 1e-6)
    with pytest.raises(BudgetExceeded):
        b.reserve(a.projected_usd)
    assert Budget(max_usd=a.projected_usd).reserve(a.projected_usd).projected_usd == \
        a.projected_usd


def test_build_sim_llm_billing_flag(monkeypatch):
    base = {"kind": "openai_compat", "base_url": "http://127.0.0.1:9/v1", "model": MODEL}
    assert cli_run.build_sim_llm({"llm": base}).endpoint_info["billing"] == "external"
    info = cli_run.build_sim_llm({"llm": dict(base, price_ref="openai_chat")}).endpoint_info
    assert info["billing"] == "per_token" and info["price_ref"] == "openai_chat"


async def test_caller_llm_records_usage():
    class Client:
        class messages:  # noqa: N801 - Anthropic-shaped client stub
            @staticmethod
            async def create(**req):
                return {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1400, "output_tokens": 7}}

    llm = CallerLLM(Client())
    ctx = SimContext("p", "g", {}, [], [])
    await llm.next_utterance(ctx)
    await llm.next_utterance(ctx)
    assert llm.usage_log == [{"input_tokens": 1400, "output_tokens": 7}] * 2
