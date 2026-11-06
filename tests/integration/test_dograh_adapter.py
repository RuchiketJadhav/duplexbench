"""M6-T02: Dograh stack adapter against a fake Dograh (FastAPI REST + websockets telephony WS).

The fake implements only what docs/dograh-ingress.md documents: POST /runs (mode
twilio, X-API-Key), GET /fetch/{wf} and /{wf}/versions, the telephony WS
handshake (connected, start{streamSid, callSid}), media both ways, `clear` on
interruption, and an org-level http_api tool whose URL is rendered from
`{{call_id}}` (= callSid) and POSTed to the harness ToolServer.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import numpy as np
import pytest
from fastapi import FastAPI, Header, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from _ws_fake import collect, ctx, fake_server, kinds  # noqa: E402

from duplexbench.adapters.dograh_stack import (DograhClient, DograhSetupError, DograhStack,  # noqa: E402
                                               definition_hash, mint_token, tool_url_template)
from duplexbench.channel import mulaw  # noqa: E402
from duplexbench.scenarios.schema import ToolDef  # noqa: E402
from duplexbench.storage import Store  # noqa: E402
from duplexbench.tools.mock_backends.account import AccountBackend  # noqa: E402
from duplexbench.tools.server import ToolServer  # noqa: E402
from duplexbench.transport import twilio_media as tm  # noqa: E402

WF, ORG, RUN, DEF = 41, 7, 9001, 12
KEY, SECRET = "dummy-key", "dummy-secret"
TOKEN = "tok-dograh-1"
FRAME = mulaw.encode(np.full(160, 4000, np.int16))
VERSIONS = [{"id": 11, "version_number": 1, "status": "archived", "workflow_json": {"n": 1},
             "workflow_configurations": {}, "template_context_variables": {}},
            {"id": DEF, "version_number": 2, "status": "published", "workflow_json": {"n": 2},
             "workflow_configurations": {"llm": "x"},
             "template_context_variables": {"provider": "twilio", "telephony_configuration_id": 3}}]
TOOL_URL = tool_url_template("http://harness", "get_account")


def fake_rest(provider="twilio") -> tuple[FastAPI, list]:
    app, calls = FastAPI(), []

    def auth(k):
        if k != KEY:
            raise HTTPException(401)

    @app.post("/api/v1/workflow/{wf}/runs")
    async def runs(wf: int, body: dict, x_api_key: str = Header(None)):
        auth(x_api_key)
        calls.append(("runs", wf, body))
        assert body["mode"] == "twilio"
        return {"id": RUN, "workflow_id": wf, "name": body["name"], "mode": "twilio",
                "created_at": "2026-11-05T00:00:00", "definition_id": DEF,
                "initial_context": {"provider": provider, "telephony_configuration_id": 3}}

    @app.get("/api/v1/workflow/fetch/{wf}")
    async def fetch(wf: int, x_api_key: str = Header(None)):
        auth(x_api_key)
        return {"id": wf, "current_definition_id": DEF, "version_number": 2, "workflow_uuid": "u"}

    @app.get("/api/v1/workflow/{wf}/versions")
    async def versions(wf: int, x_api_key: str = Header(None)):
        auth(x_api_key)
        return VERSIONS

    return app, calls


def fake_telephony_ws(seen: list, harness_app: FastAPI, drop: bool = False):
    async def handler(ws, path):
        seen.append(("path", path))
        first = tm.decode(await ws.recv())
        start = tm.decode(await ws.recv())
        seen.extend([first, start])
        assert first.event == "connected" and start.event == "start"
        sid, call_id = start.stream_sid, start.call_sid       # Dograh: call_id := callSid
        if drop:
            raise RuntimeError("pipeline crashed")
        n = 0
        async for raw in ws:
            m = tm.decode(raw)
            if m.event != "media":
                seen.append(m)
                continue
            n += 1
            if n == 1:
                for _ in range(3):
                    await ws.send(tm.encode_media(sid, FRAME))
            elif n == 3:          # the workflow's LLM calls the org-level http_api tool
                url = TOOL_URL.replace("{{call_id}}", quote(call_id, safe=""))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness_app)) as h:
                    r = await h.post(url, json={"customer_id": "C-100"})
                seen.append(("tool", url, r.status_code, r.json()))
                await ws.send(tm.encode_clear(sid))
            elif n == 5:
                await ws.close()
                return
    return handler


def _cfg(ws_url):
    return {"system_id": "dograh_stack", "adapter": "dograh_stack", "components_only": False,
            "params": {"endpoint": "http://dograh", "ws_endpoint": ws_url,
                               "workflow_id": WF, "org_id": ORG,
                               "org_model_config": {"stt": "deepgram"}}}


async def test_run_handshake_media_clear_and_tool_via_harness(tmp_path):
    rest, rest_calls = fake_rest()
    client = DograhClient("http://dograh", KEY, transport=httpx.ASGITransport(app=rest))
    store = Store(tmp_path / "e.sqlite")
    call = ctx(token=TOKEN)
    tools = ToolServer()
    tools.register_call(TOKEN, AccountBackend(),
                        [ToolDef(name="get_account", kind="read", latency_ms=0)],
                        store, call.clock, run_id="run1", call_id="call1")
    seen: list = []
    async with fake_server(fake_telephony_ws(seen, tools.app)) as ws_url:
        a = DograhStack(call.clock, _cfg(ws_url), client=client, tool_events=tools,
                        ws_token_secret=SECRET)
        await a.open(call)

        async def drive():
            call.turn_ended(call.clock.now_ns())
            for _ in range(5):
                await a.send_frame(FRAME, call.clock.now_ns())
                await asyncio.sleep(0.02)

        evs = await collect(a, call, drive)
        await a.close()
    await client.aclose()
    store.flush_sync()
    # run created in twilio mode, named after the call token
    assert rest_calls == [("runs", WF, {"mode": "twilio", "name": f"DB-{TOKEN}"})]
    # WS URL with the minted HMAC token as the 4th segment
    tok = hmac.new(SECRET.encode(), f"{WF}:{ORG}:{RUN}".encode(), hashlib.sha256).hexdigest()
    assert seen[0] == ("path", f"/api/v1/telephony/ws/{WF}/{ORG}/{RUN}/{tok}")
    # handshake: connected, then start with callSid = harness tool token
    assert seen[1].protocol == "Call" and seen[2].call_sid == TOKEN
    assert seen[2].media_format == {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
    # media both ways, clear -> cancel_or_truncate
    assert kinds(evs) == (["session_open", "first_audio_byte"] + ["audio"] * 3
                          + ["cancel_or_truncate", "closed"])
    assert "tool_call" not in kinds(evs)          # tools never come over this socket
    # tool call reached this call's ToolServer binding through {{call_id}}
    tool = [s for s in seen if isinstance(s, tuple) and s[0] == "tool"][0]
    assert tool[1] == f"http://harness/tools/{TOKEN}/get_account" and tool[2] == 200
    assert [k for k, _ in a.tool_log] == ["start", "result"]
    con = sqlite3.connect(store.path)
    rows = con.execute("select call_id, name from tool_calls").fetchall()
    con.close()
    store.close()
    assert rows == [("call1", "get_account")]
    # run card
    card = a.run_card_fields()
    assert card["definition_id"] == DEF and card["definition_matches_pinned"] is True
    assert card["config_hash"] == definition_hash(VERSIONS[1], {"stt": "deepgram"})
    assert any("no concurrency slot" in d for d in card["disclosures"])


async def test_drop_mid_call_is_error_then_closed():
    rest, _ = fake_rest()
    client = DograhClient("http://dograh", KEY, transport=httpx.ASGITransport(app=rest))
    seen: list = []
    async with fake_server(fake_telephony_ws(seen, FastAPI(), drop=True)) as ws_url:
        call = ctx(token=TOKEN)
        a = DograhStack(call.clock, _cfg(ws_url), client=client)
        await a.open(call)
        evs = await collect(a, call)
    await client.aclose()
    assert kinds(evs) == ["session_open", "error", "closed"]
    assert seen[0] == ("path", f"/api/v1/telephony/ws/{WF}/{ORG}/{RUN}")    # no secret, no token


async def test_run_without_twilio_provider_is_refused():
    rest, _ = fake_rest(provider=None)
    client = DograhClient("http://dograh", KEY, transport=httpx.ASGITransport(app=rest))
    a = DograhStack(None, _cfg("ws://127.0.0.1:9"), client=client)
    with pytest.raises(DograhSetupError, match="provider"):
        await a.open(ctx(token=TOKEN))
    await client.aclose()


async def test_pin_definition_and_hash():
    rest, _ = fake_rest()
    client = DograhClient("http://dograh", KEY, transport=httpx.ASGITransport(app=rest))
    def_id, h = await client.pin_definition(WF)
    await client.aclose()
    assert def_id == DEF and len(h) == 64
    assert h == definition_hash(VERSIONS[1]) != definition_hash(VERSIONS[0])
    assert definition_hash({**VERSIONS[1], "status": "x", "id": 99}) == h    # only behaviour fields


def test_mint_token_and_tool_url():
    assert mint_token("s", 1, 2, 3) == hmac.new(b"s", b"1:2:3", hashlib.sha256).hexdigest()
    assert tool_url_template("http://h:8900/", "update_phone") == \
        "http://h:8900/tools/{{call_id}}/update_phone"


def test_config_requires_ids_and_key(monkeypatch):
    monkeypatch.delenv("DOGRAH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="workflow_id"):
        DograhStack(None, {"params": {"endpoint": "http://d", "org_id": 1}})
    with pytest.raises(DograhSetupError, match="API key"):
        DograhStack(None, {"params": {"endpoint": "http://d", "workflow_id": 1, "org_id": 1}})
