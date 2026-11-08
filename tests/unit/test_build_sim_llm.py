"""sim-llm: cli_run.build_sim_llm parses simulator.llm into a CallerLLM (config only, no network)."""
from __future__ import annotations

import pytest

from duplexbench import cli_run
from duplexbench.simulator.caller_llm import DEFAULT_PROMPT, CallerLLM, SimContext
from duplexbench.simulator.openai_compat import OpenAICompatClient

PHONELLM = "pipecat-ai/phonellm-alpha-1"


def _llm(**over):
    spec = {"kind": "openai_compat", "base_url": "http://10.0.0.5:8000/v1/", "model": PHONELLM,
            "temperature": 0, "send_temperature": True, "max_tokens": 400,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    spec.update(over)
    return {"llm": {k: v for k, v in spec.items() if v is not None}}


def test_absent_is_none():
    assert cli_run.build_sim_llm({}) is None
    assert cli_run.build_sim_llm({"refasr": {"candidate": "x"}}) is None
    assert cli_run.build_sim_llm(None) is None


def test_builds_caller_llm():
    llm = cli_run.build_sim_llm(_llm())
    assert isinstance(llm, CallerLLM) and isinstance(llm.client, OpenAICompatClient)
    assert llm.config["model"] == PHONELLM and llm.config["temperature"] == 0
    assert llm.config["send_temperature"] is True and llm.config["max_tokens"] == 400
    assert llm.config["effort"] is None and llm.prompt_path == DEFAULT_PROMPT
    c = llm.client
    assert c.base_url == "http://10.0.0.5:8000/v1" and c.model == PHONELLM and c.api_key is None
    assert c.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    assert llm.endpoint_info == {"kind": "openai_compat", "model": PHONELLM,
                                 "base_url": "http://10.0.0.5:8000/v1", "billing": "external"}
    req = llm.request(SimContext(None, None, {}, [], []))
    assert req["temperature"] == 0 and "output_config" not in req


def test_config_hash_changes_with_model():
    a = cli_run.build_sim_llm(_llm())
    b = cli_run.build_sim_llm(_llm(model="other/model"))
    c = cli_run.build_sim_llm(_llm())
    assert a.config_hash != b.config_hash and a.config_hash == c.config_hash


def test_env_vars(monkeypatch):
    monkeypatch.setenv("SIM_LLM_BASE_URL", "https://modal.example/v1")
    monkeypatch.setenv("SIM_LLM_API_KEY", "sk-abc")
    llm = cli_run.build_sim_llm(_llm(base_url=None, base_url_env="SIM_LLM_BASE_URL",
                                     api_key_env="SIM_LLM_API_KEY"))
    assert llm.client.base_url == "https://modal.example/v1" and llm.client.api_key == "sk-abc"


@pytest.mark.parametrize("over,needle", [
    ({"kind": "anthropic"}, "unknown kind"),
    ({"kind": None}, "unknown kind"),
    ({"base_url": None}, "missing base_url"),
    ({"base_url": None, "base_url_env": "SIM_LLM_BASE_URL_UNSET"}, "base_url_env"),
    ({"base_url": "10.0.0.5:8000/v1"}, "http:// or https://"),
    ({"model": None}, "missing model"),
    ({"api_key_env": "SIM_LLM_API_KEY_UNSET"}, "api_key_env"),
    ({"prompt": "no_such_prompt.md"}, "prompt"),
])
def test_value_errors(monkeypatch, over, needle):
    monkeypatch.delenv("SIM_LLM_BASE_URL_UNSET", raising=False)
    monkeypatch.delenv("SIM_LLM_API_KEY_UNSET", raising=False)
    with pytest.raises(ValueError, match=needle) as e:
        cli_run.build_sim_llm(_llm(**over))
    assert "\n" not in str(e.value)


def test_empty_api_key_env_refused(monkeypatch):
    monkeypatch.setenv("SIM_LLM_API_KEY", "  ")
    with pytest.raises(ValueError, match="api_key_env SIM_LLM_API_KEY"):
        cli_run.build_sim_llm(_llm(api_key_env="SIM_LLM_API_KEY"))
