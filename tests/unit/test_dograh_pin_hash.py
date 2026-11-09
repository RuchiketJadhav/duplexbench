"""fix-35 / fix-36 / fix-37: what `DograhClient.pin` hashes beyond the workflow definition.

Fake HTTP layer: httpx.MockTransport behind the real DograhClient (no network).
Routes and shapes are the ones read in Dograh's source (routes/tool.py:96-134,
schemas/tool.py:683-697; routes/user.py:158-183; services/configuration/masking.py:138-150).
"""
from __future__ import annotations

import copy

import httpx

from duplexbench.adapters.dograh_stack import (DISCLOSURES, HASHED_FIELDS, DograhClient,
                                               DograhStack, definition_hash,
                                               find_connection_provider, referenced_tool_uuids)
from duplexbench.systems import load_system

WF, DEF = 41, 12
U1, U2, U_OTHER = "uuid-get-account", "uuid-verify", "uuid-unreferenced"

OVERRIDE = {"version": 2, "mode": "byok", "byok": {"mode": "realtime", "realtime": {
    "realtime": {"provider": "openai_realtime", "api_key": "****************cdef",
                 "model": "gpt-realtime-2.1", "voice": "alloy"},
    "llm": {"provider": "openai", "api_key": "****************wxyz", "model": "gpt-4.1"}}}}


def _version(override: dict | None = None) -> dict:
    wcfg = {"max_call_duration": 600}
    if override is not None:
        wcfg["model_configuration_v2_override"] = override
    return {"id": DEF, "version_number": 2, "status": "published",
            "workflow_json": {"nodes": [
                {"id": "n5", "type": "agentNode", "data": {"tool_uuids": [U1, U2]}},
                {"id": "n4", "type": "agentNode", "data": {"tool_uuids": [U2]}},
                {"id": "n1", "type": "startCall", "data": {}}], "edges": []},
            "workflow_configurations": wcfg,
            "template_context_variables": {"provider": "twilio"}}


def _tool(uuid: str, name: str, url: str, *, tid: int = 1, headers=None, cred=None) -> dict:
    cfg = {"method": "POST", "url": url, "timeout_ms": 15000,
           "parameters": [{"name": "customer_id", "type": "string",
                           "description": "id", "required": True}]}
    if headers is not None:
        cfg["headers"] = headers
    if cred is not None:
        cfg["credential_uuid"] = cred
    return {"id": tid, "tool_uuid": uuid, "name": name, "description": f"{name} tool",
            "category": "http_api", "icon": None, "icon_color": None, "status": "active",
            "definition": {"schema_version": 1, "type": "http_api", "config": cfg},
            "created_at": "2026-11-07T00:00:00", "updated_at": None}


def _tools() -> list[dict]:
    base = "http://h:8900/tools/{{call_id}}"
    return [_tool(U1, "get_account", f"{base}/get_account", tid=1,
                  headers={"X-Env": "bench-secret-1"}, cred="cred-aaa"),
            _tool(U2, "verify_identity", f"{base}/verify_identity", tid=2),
            _tool(U_OTHER, "unused", f"{base}/unused", tid=3)]


ORG_CFG = {"llm": {"provider": "openai", "api_key": "****************abcd", "model": "gpt-4.1"},
           "tts": {"provider": "elevenlabs", "api_key": "****************efgh", "model": "m",
                   "voice": "v"},
           "stt": {"provider": "deepgram", "api_key": "****************ijkl", "model": "nova-3"},
           "embeddings": None, "realtime": None, "is_realtime": False,
           "test_phone_number": "+15550001111", "timezone": "UTC",
           "organization_pricing": {"price_per_second_usd": 0.01}}


class Fake:
    """Answers the Dograh REST routes the pin uses; records every request path."""

    def __init__(self, version: dict, *, tools=None, tools_status: int = 200,
                 org=None, org_status: int = 200, conns=None, conn_status: int = 200):
        self.version, self.tools, self.tools_status = version, tools, tools_status
        self.conns, self.conn_status = conns, conn_status
        self.org, self.org_status = org, org_status
        self.paths: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        self.paths.append(p)
        if p == f"/api/v1/workflow/fetch/{WF}":
            return httpx.Response(200, json={"id": WF, "current_definition_id": DEF})
        if p == f"/api/v1/workflow/{WF}/versions":
            return httpx.Response(200, json=[self.version])
        if p == "/api/v1/tools/":
            if self.tools_status != 200:
                return httpx.Response(self.tools_status, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.tools)
        if p == "/api/v1/model-connections/model-configurations":
            if self.conn_status != 200:
                return httpx.Response(self.conn_status, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.conns)
        if p == "/api/v1/user/configurations/user":
            if self.org_status != 200:
                return httpx.Response(self.org_status, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.org)
        return httpx.Response(404, json={"detail": "Not Found"})


async def _pin(fake: Fake, pasted=None):
    c = DograhClient("http://dograh", "k", transport=httpx.MockTransport(fake))
    try:
        return await c.pin(WF, pasted)
    finally:
        await c.aclose()


def test_referenced_tool_uuids_walks_every_node():
    assert referenced_tool_uuids(_version()["workflow_json"]) == [U1, U2]
    assert referenced_tool_uuids({"nodes": []}) == []


# ---- fix-35: tool rows -------------------------------------------------------------------

async def test_tool_rows_fetched_and_hashed():
    fake = Fake(_version(OVERRIDE), tools=_tools())
    r = await _pin(fake)
    assert "/api/v1/tools/" in fake.paths
    assert r.tool_count == 2 and r.tool_refs == 2          # the unreferenced row is not hashed
    assert "tool_definitions (GET /api/v1/tools/)" in r.scope
    assert r.config_hash != definition_hash(_version(OVERRIDE))   # tools entered the hash
    assert not any("not hashed" in d for d in r.disclosures)


async def test_hash_changes_when_a_tool_url_changes():
    tools2 = _tools()
    tools2[0]["definition"]["config"]["url"] = "http://h:8901/tools/{{call_id}}/get_account"
    a = await _pin(Fake(_version(OVERRIDE), tools=_tools()))
    b = await _pin(Fake(_version(OVERRIDE), tools=tools2))
    assert a.config_hash != b.config_hash


async def test_hash_changes_on_timeout_header_name_and_status():
    base = (await _pin(Fake(_version(OVERRIDE), tools=_tools()))).config_hash
    for mutate in (lambda t: t[1]["definition"]["config"].__setitem__("timeout_ms", 5000),
                   lambda t: t[0]["definition"]["config"]["headers"].__setitem__("X-New", "v"),
                   lambda t: t[1].__setitem__("status", "archived"),
                   lambda t: t[0]["definition"]["config"].pop("credential_uuid")):
        tools = _tools()
        mutate(tools)
        assert (await _pin(Fake(_version(OVERRIDE), tools=tools))).config_hash != base


async def test_hash_ignores_credentials_ids_timestamps_and_unreferenced_rows():
    base = (await _pin(Fake(_version(OVERRIDE), tools=_tools()))).config_hash
    tools = _tools()
    tools[0]["definition"]["config"]["headers"]["X-Env"] = "rotated-secret-2"
    tools[0]["definition"]["config"]["credential_uuid"] = "cred-bbb"
    tools[0]["definition"]["config"]["api_key"] = "sk-live-should-never-hash"
    for i, t in enumerate(tools):
        t["id"] = 100 + i
        t["created_at"] = "2027-01-01T00:00:00"
        t["updated_at"] = "2027-01-02T00:00:00"
        t["icon"] = "wrench"
    tools[2]["definition"]["config"]["url"] = "http://elsewhere/unused"
    tools.reverse()                               # row order is not behaviour
    assert (await _pin(Fake(_version(OVERRIDE), tools=tools))).config_hash == base


async def test_tool_fetch_failure_discloses_and_hashes_no_tools():
    fake = Fake(_version(OVERRIDE), tools_status=404)
    r = await _pin(fake)
    assert r.tool_count is None
    assert r.config_hash == definition_hash(_version(OVERRIDE))
    assert [d for d in r.disclosures if d.startswith("tool definitions not hashed: ")] == [
        "tool definitions not hashed: HTTP 404 from GET /api/v1/tools/"]
    assert not any(s.startswith("tool_definitions") for s in r.scope)


async def test_tools_not_a_list_is_a_disclosed_failure():
    r = await _pin(Fake(_version(OVERRIDE), tools={"tools": []}))
    assert r.tool_count is None
    assert any(d.startswith("tool definitions not hashed: ValueError") for d in r.disclosures)


async def test_missing_referenced_tool_is_counted_and_disclosed():
    r = await _pin(Fake(_version(OVERRIDE), tools=_tools()[:1]))
    assert r.tool_count == 1
    assert any("1 of 2 tool_uuids" in d for d in r.disclosures)


async def test_no_tool_refs_no_fetch():
    v = _version(OVERRIDE)
    v["workflow_json"] = {"nodes": [{"id": "n1", "data": {}}]}
    fake = Fake(v, tools=_tools())
    r = await _pin(fake)
    assert "/api/v1/tools/" not in fake.paths
    assert r.config_hash == definition_hash(v) and r.tool_count is None


# ---- fix-36: model source ----------------------------------------------------------------

async def test_override_present_is_workflow_override_and_no_org_fetch():
    fake = Fake(_version(OVERRIDE), tools=_tools(), org=ORG_CFG)
    r = await _pin(fake, pasted={"stt": "pasted"})
    assert "/api/v1/user/configurations/user" not in fake.paths
    assert r.model_source == "workflow_override"
    assert r.model_names == {"mode": "byok", "byok_mode": "realtime",
                             "llm": {"provider": "openai", "model": "gpt-4.1"},
                             "realtime": {"provider": "openai_realtime",
                                          "model": "gpt-realtime-2.1"}}
    assert "api_key" not in repr(r.model_names)
    assert not any(s.startswith("org_model_config") for s in r.scope)
    # org config and pasted value never enter the hash under the override
    tools_snap_hash = (await _pin(Fake(_version(OVERRIDE), tools=_tools()))).config_hash
    assert r.config_hash == tools_snap_hash


async def test_non_realtime_override_is_disclosed():
    ov = copy.deepcopy(OVERRIDE)
    ov["byok"] = {"mode": "pipeline", "pipeline": {"llm": {"provider": "openai", "model": "x"}}}
    r = await _pin(Fake(_version(ov), tools=_tools()))
    assert r.model_source == "workflow_override"
    assert any("not byok/realtime" in d for d in r.disclosures)


async def test_override_absent_fetches_org_config_and_hashes_it_without_secrets():
    fake = Fake(_version(), tools=_tools(), org=ORG_CFG)
    r = await _pin(fake)
    assert "/api/v1/user/configurations/user" in fake.paths
    assert r.model_source == "org_config"
    assert r.model_names["stt"] == {"provider": "deepgram", "model": "nova-3"}
    assert "org_model_config (GET /api/v1/user/configurations/user)" in r.scope
    # masked key tails, test phone and pricing do not move the hash; a model change does
    org2 = copy.deepcopy(ORG_CFG)
    org2["llm"]["api_key"] = "****************zzzz"
    org2["test_phone_number"] = "+15559999999"
    org2["organization_pricing"] = None
    assert (await _pin(Fake(_version(), tools=_tools(), org=org2))).config_hash == r.config_hash
    org3 = copy.deepcopy(ORG_CFG)
    org3["llm"]["model"] = "gpt-4.1-mini"
    assert (await _pin(Fake(_version(), tools=_tools(), org=org3))).config_hash != r.config_hash


async def test_org_config_404_is_unknown_with_disclosure():
    r = await _pin(Fake(_version(), tools=_tools(), org_status=404))
    assert r.model_source == "unknown" and r.model_names == {}
    assert any(d.startswith("org model configuration not hashed: HTTP 404 from GET "
                            "/api/v1/user/configurations/user") for d in r.disclosures)


async def test_org_config_404_with_pasted_value_is_marked_pasted():
    pasted = {"stt": "deepgram"}
    r = await _pin(Fake(_version(), tools_status=404, org_status=404), pasted=pasted)
    assert r.model_source == "pasted"
    assert "org_model_config (pasted in the system YAML)" in r.scope
    assert r.config_hash == definition_hash(_version(), pasted)
    assert any("pasted in params.org_model_config was hashed" in d for d in r.disclosures)


async def test_fetched_org_config_wins_over_pasted():
    r = await _pin(Fake(_version(), tools=_tools(), org=ORG_CFG), pasted={"stt": "old"})
    assert r.model_source == "org_config"
    assert any("params.org_model_config ignored" in d for d in r.disclosures)


# ---- run card + fix-37 -------------------------------------------------------------------

async def test_run_card_carries_scope_source_and_disclosures():
    fake = Fake(_version(OVERRIDE), tools=_tools())
    client = DograhClient("http://dograh", "k", transport=httpx.MockTransport(fake))
    a = DograhStack(None, {"params": {"endpoint": "http://dograh", "workflow_id": WF,
                                      "org_id": 7}}, client=client)
    await a.pin()
    await client.aclose()
    card = a.run_card_fields()
    assert card["config_hash_scope"] == list(HASHED_FIELDS) + [
        "tool_definitions (GET /api/v1/tools/)"]
    assert card["model_source"] == "workflow_override"
    assert card["model_names"]["realtime"]["model"] == "gpt-realtime-2.1"
    assert card["tool_definitions_hashed"] == 2 and card["tool_uuids_referenced"] == 2
    assert card["disclosures"][:len(DISCLOSURES)] == list(DISCLOSURES)


def test_mute_disclosures_present():
    tool = [d for d in DISCLOSURES if "FunctionCallUserMuteStrategy" in d]
    first = [d for d in DISCLOSURES if "MuteUntilFirstBotCompleteUserMuteStrategy" in d]
    assert len(tool) == 1 and "every tool call" in tool[0] and "run_pipeline.py" in tool[0]
    assert len(first) == 1 and "realtime path" in first[0] and "run_pipeline.py" in first[0]


def test_definition_hash_backward_compatible():
    v = _version()
    assert definition_hash(v, {"a": 1}) == definition_hash(v, {"a": 1}, tool_definitions=None)
    assert definition_hash(v) != definition_hash(v, tool_definitions={"rows": [], "missing": 0})


# ---- fix-43: hosted model config v3 override (decision B23) -------------------------------

CONN = "64141402-aa1a-4fbe-9aa9-c70f18ca5f66"
V3 = {"mode": "realtime", "realtime": {"provider_connection_uuid": CONN,
                                       "settings": {"model": "gpt-realtime-2.1",
                                                    "voice": "alloy", "language": "en"}}}
# the hosted response shape is not in the local Dograh source: one plausible layout
CONNS = {"connections": [{"uuid": "other", "provider": "google_realtime"},
                         {"uuid": CONN, "name": "full duplex bench",
                          "provider": "openai_realtime", "api_key": "****wxyz"}]}
LIVE_NAMES = {"mode": "realtime", "provider": "openai_realtime", "model": "gpt-realtime-2.1",
              "voice": "alloy", "language": "en"}


def _v3_version(v3=V3, v2=None) -> dict:
    v = _version(v2)
    v["workflow_configurations"]["model_configuration_override"] = v3
    return v


def test_find_connection_provider_is_shape_agnostic():
    assert find_connection_provider(CONNS, CONN) == "openai_realtime"
    assert find_connection_provider(CONNS["connections"], CONN) == "openai_realtime"
    assert find_connection_provider({"items": {"x": [{"provider_connection_uuid": CONN,
                                                      "provider": "p"}]}}, CONN) == "p"
    assert find_connection_provider(CONNS, "nope") is None


async def test_v3_override_resolves_provider_and_skips_org_fetch():
    fake = Fake(_v3_version(), tools=_tools(), org=ORG_CFG, conns=CONNS)
    r = await _pin(fake)
    assert "/api/v1/user/configurations/user" not in fake.paths
    assert "/api/v1/model-connections/model-configurations" in fake.paths
    assert r.model_source == "workflow_override"
    assert r.model_names == LIVE_NAMES
    assert "key" not in repr(r.model_names) and "wxyz" not in repr(r.model_names)
    assert not any("provider" in d and "not resolved" in d for d in r.disclosures)


async def test_v3_unresolved_provider_falls_back_with_disclosure():
    for fake in (Fake(_v3_version(), tools=_tools(), conn_status=404),
                 Fake(_v3_version(), tools=_tools(), conns={"connections": []})):
        r = await _pin(fake)
        assert r.model_source == "workflow_override"
        assert r.model_names["provider"] == f"unresolved:{CONN}"
        assert r.model_names["model"] == "gpt-realtime-2.1"
        assert any(d.startswith(f"model provider not resolved for connection {CONN}")
                   for d in r.disclosures)


async def test_v3_failed_lookup_does_not_move_the_hash():
    a = await _pin(Fake(_v3_version(), tools=_tools(), conns=CONNS))
    b = await _pin(Fake(_v3_version(), tools=_tools(), conn_status=500))
    assert a.config_hash == b.config_hash


async def test_both_override_keys_prefer_v3_and_disclose():
    r = await _pin(Fake(_v3_version(v2=OVERRIDE), tools=_tools(), conns=CONNS))
    assert r.model_names == LIVE_NAMES
    assert any("both model_configuration_override and model_configuration_v2_override"
               in d for d in r.disclosures)


async def test_v3_non_realtime_mode_is_disclosed():
    v3 = {"mode": "pipeline", "pipeline": {"provider_connection_uuid": CONN, "settings": {}}}
    r = await _pin(Fake(_v3_version(v3), tools=_tools(), conns=CONNS))
    assert r.model_names["mode"] == "pipeline"
    assert any("not 'realtime'" in d for d in r.disclosures)


def test_shipped_yaml_targets_the_live_workflow():
    s = load_system("dograh_stack")
    assert s.model == "gpt-realtime-2.1"
    assert s.params["endpoint"] == "https://app.dograh.com"
    assert (s.params["workflow_id"], s.params["org_id"]) == (14735, 6993)
    assert s.params["api_key_env"] == "DOGRAH_API_KEY" and s.params["ws_token_secret_env"] == "TELEPHONY_WS_TOKEN_SECRET"
    c = DograhClient(s.params["endpoint"], "k")
    assert str(c._http.base_url).rstrip("/") + "/api/v1" + "/workflow/fetch/14735" ==         "https://app.dograh.com/api/v1/workflow/fetch/14735"
