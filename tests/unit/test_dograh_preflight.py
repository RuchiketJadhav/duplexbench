"""fix-22: plan-time preflight (Adapter.preflight, DograhStack.preflight, systems.preflight_system)."""
from __future__ import annotations

import pytest

from duplexbench.adapters.base import Adapter
from duplexbench.adapters.dograh_stack import DEFAULT_API_KEY_ENV, DograhSetupError, DograhStack
from duplexbench.adapters.mock_agent import MockAgent
from duplexbench.systems import load_system, parse_system, preflight_system

GOOD = {"endpoint": "http://d", "workflow_id": 41, "org_id": 7}


def _system(params: dict):
    return parse_system({"system_id": "dograh_x", "adapter": "dograh_stack", "model": None,
                         "reasoning_effort": None, "turn_detection": None, "self_hosted": False,
                         "components_only": False, "cancel_event": True, "price_ref": None,
                         "params": params})


@pytest.fixture
def no_env(monkeypatch):
    for k in (DEFAULT_API_KEY_ENV, "MY_KEY", "WS_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_base_default_is_empty():
    assert Adapter.preflight.__func__(Adapter, {}) == []
    assert MockAgent.preflight({"params": {}}) == []


def test_dograh_ready(no_env):
    no_env.setenv(DEFAULT_API_KEY_ENV, "k")
    assert DograhStack.preflight({"params": GOOD}) == []


def test_dograh_reports_every_missing_precondition(no_env):
    lines = DograhStack.preflight({"params": {"org_id": "", "ws_token_secret_env": "WS_SECRET"}})
    assert len(lines) == 5
    joined = "\n".join(lines)
    for frag in ("params.endpoint is missing", "params.workflow_id is missing",
                 "params.org_id is missing", f"env var {DEFAULT_API_KEY_ENV} is unset",
                 "ws_token_secret_env names WS_SECRET, which is unset"):
        assert frag in joined


def test_dograh_custom_key_env_and_secret_set(no_env):
    cfg = {"params": {**GOOD, "api_key_env": "MY_KEY", "ws_token_secret_env": "WS_SECRET"}}
    no_env.setenv(DEFAULT_API_KEY_ENV, "wrong-var")
    no_env.setenv("WS_SECRET", "s")
    assert DograhStack.preflight(cfg) == ["dograh_stack: Dograh API key env var MY_KEY is unset"]
    no_env.setenv("MY_KEY", "k")
    assert DograhStack.preflight(cfg) == []


def test_dograh_placeholder_ids_are_flagged(no_env):
    no_env.setenv(DEFAULT_API_KEY_ENV, "k")
    lines = DograhStack.preflight({"params": {**GOOD, "workflow_id": "TO_SET"}})
    assert lines == ["dograh_stack: params.workflow_id is still the placeholder 'TO_SET'"]


def test_preflight_system_resolves_adapter(no_env):
    assert preflight_system(load_system("mock_stops")) == []
    assert any(DEFAULT_API_KEY_ENV in line for line in preflight_system(_system(GOOD)))
    no_env.setenv(DEFAULT_API_KEY_ENV, "k")
    assert preflight_system(_system(GOOD)) == []
    # fix-43: the shipped dograh_stack.yaml carries the live ids (workflow 14735, org 6993);
    # only the API key is missing at plan time
    no_env.delenv(DEFAULT_API_KEY_ENV, raising=False)
    shipped = preflight_system(load_system("dograh_stack"))
    assert not any("workflow_id" in line or "org_id" in line for line in shipped)
    assert any(DEFAULT_API_KEY_ENV in line for line in shipped)


def test_init_key_rules(no_env):
    with pytest.raises(DograhSetupError, match="API key"):
        DograhStack(None, {"params": GOOD})
    a = DograhStack(None, {"params": GOOD}, connector=object())     # test double: no key needed
    assert a.client is not None
