"""HTTP tool server over a real socket (fix-44). $0, loopback only, no provider.

uvicorn serves `create_app(ToolServer)` on an ephemeral 127.0.0.1 port as a task on the
test's loop; httpx calls it over TCP. Covers /healthz, GET /tools/{token}, a POST that
reaches the ToolServer binding and writes a `tool_calls` row, port parsing and refusals,
the `tools serve` builder and command, and `cli run` with and without `needs_http_tools`.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from duplexbench import systems as systems_mod
from duplexbench.adapters.mock_agent import MockAgent, MockProgram
from duplexbench.cli import app
from duplexbench.clock import RealClock
from duplexbench.storage import Store
from duplexbench.tools.http import (DEFAULT_TOOL_PORT, ToolHttp, ToolPortError, bind_socket,
                                    build_test_server, port_from_base, serve_test)
from duplexbench.tools.mock_backends import BillingBackend
from duplexbench.tools.server import ToolServer

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "configs" / "runs" / "smoke_mock.yaml"


def _url(http: ToolHttp) -> str:
    return f"http://127.0.0.1:{http.port}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# -- port parsing -----------------------------------------------------------------------
@pytest.mark.parametrize("base,port", [
    ("http://<harness-host>:8900", 8900),
    ("http://10.0.0.5:9100/", 9100),
    ("http://<harness-host>", DEFAULT_TOOL_PORT),
    (None, DEFAULT_TOOL_PORT),
    ("", DEFAULT_TOOL_PORT),
])
def test_port_from_base(base, port):
    assert port_from_base(base) == port


@pytest.mark.parametrize("base", ["http://h:443", "http://h:80", "https://h", "https://h:8900",
                                  "http://h:notaport"])
def test_port_from_base_refuses(base):
    with pytest.raises(ToolPortError):
        port_from_base(base)


def test_port_in_use_is_refused_naming_the_port():
    a = bind_socket("127.0.0.1", 0)
    port = a.getsockname()[1]
    try:
        with pytest.raises(ToolPortError, match=f"port {port} in use"):
            bind_socket("127.0.0.1", port)
    finally:
        a.close()
    bind_socket("127.0.0.1", port).close()            # free again after close


def test_bind_refuses_443():
    with pytest.raises(ToolPortError, match="443"):
        bind_socket("127.0.0.1", 443)


# -- the served app ------------------------------------------------------------------------
async def test_healthz_get_token_and_post_reach_the_binding(tmp_path):
    clock = RealClock()
    store = Store(tmp_path / "events.sqlite")
    server = ToolServer()
    server.register_call("tok-1", BillingBackend(), None, store, clock, run_id="r1",
                         call_id="c1")
    seen: list[str] = []
    server.subscribe("tok-1", lambda kind, info: seen.append(kind))
    async with ToolHttp(server.app, bind_socket("127.0.0.1", 0)) as http:
        async with httpx.AsyncClient(base_url=_url(http)) as c:
            h = await c.get("/healthz")
            g = await c.get("/tools/tok-1")
            g404 = await c.get("/tools/nope")
            p = await c.post("/tools/tok-1/get_balance", json={"account_id": "AC-77"},
                             headers={"X-Tool-Call-Id": "fc_9"})
            bad = await c.post("/tools/tok-1/get_balance", json={})
    assert (h.status_code, h.json()) == (200, {"ok": True})
    assert g.status_code == 200
    assert [t["name"] for t in g.json()] == [t.name for t in BillingBackend().tools()]
    assert (g404.status_code, g404.json()["error"]) == (404, "unknown_call_token")
    assert p.status_code == 200 and p.json()["balance"] == "120.00"
    assert p.headers["X-Tool-Call-Id"] == "fc_9"
    assert bad.status_code == 400
    assert seen == ["start", "result", "start", "result"]
    await store.flush()
    store.close()
    conn = sqlite3.connect(str(tmp_path / "events.sqlite"))
    rows = conn.execute("SELECT run_id, call_id, tool_call_id, name, args_json FROM tool_calls "
                        "ORDER BY start_ns").fetchall()
    conn.close()
    assert rows[0] == ("r1", "c1", "fc_9", "get_balance", '{"account_id":"AC-77"}')
    assert len(rows) == 2


async def test_stop_closes_the_port():
    http = ToolHttp(ToolServer().app, bind_socket("127.0.0.1", 0))
    await http.start()
    port = http.port
    await http.stop()
    bind_socket("127.0.0.1", port).close()             # rebindable: nothing listens any more


# -- tools serve ------------------------------------------------------------------------------
def test_build_test_server_binding():
    server = build_test_server()
    assert server.binding("TEST").exposed() == [t.name for t in BillingBackend().tools()]
    with pytest.raises(KeyError):
        build_test_server(domain="nope")


async def test_serve_test_answers_until_stopped():
    server = build_test_server("TEST", "billing")
    lines: list[str] = []
    stop = asyncio.Event()
    sock = bind_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    task = asyncio.create_task(serve_test(server, sock, token="TEST", domain="billing",
                                          echo=lines.append, stop=stop))
    for _ in range(400):
        if lines:
            break
        await asyncio.sleep(0.005)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
        g = await c.get("/tools/TEST")
        empty = await c.post("/tools/TEST/get_balance", json={})
        wrong = await c.post("/tools/TEST/get_balance", json={"customer_id": "AC-77"})
        ok = await c.post("/tools/TEST/get_balance", json={"account_id": "AC-77"})
    stop.set()
    await task
    assert g.status_code == 200 and "get_balance" in [t["name"] for t in g.json()]
    assert empty.status_code == wrong.status_code == 400            # mock answers, bad args
    assert empty.json()["error"] == "bad_arguments"
    assert ok.status_code == 200 and ok.json()["account_id"] == "AC-77"
    assert lines[0].startswith(f"tool server: listening on 127.0.0.1:{port}")
    assert any(f"/tools/TEST/get_balance" in ln for ln in lines)
    assert any("get_balance -> 200" in ln for ln in lines)
    bind_socket("127.0.0.1", port).close()


def test_tools_serve_cli_refuses_busy_port():
    a = bind_socket("127.0.0.1", 0)
    port = a.getsockname()[1]
    try:
        r = CliRunner().invoke(app, ["tools", "serve", "--host", "127.0.0.1",
                                     "--port", str(port)])
    finally:
        a.close()
    assert r.exit_code == 2
    assert f"port {port} in use" in r.output


def test_tools_serve_cli_refuses_443_and_unknown_domain():
    r = CliRunner().invoke(app, ["tools", "serve", "--port", "443"])
    assert r.exit_code == 2 and "443" in r.output
    r = CliRunner().invoke(app, ["tools", "serve", "--domain", "nope"])
    assert r.exit_code == 2 and "nope" in r.output


# -- cli run -----------------------------------------------------------------------------------
class HttpMockAgent(MockAgent):
    """The mock agent, flagged as an HTTP-tools system; on open it calls the harness over
    TCP like Dograh would (healthz + a tool POST for this call's token)."""
    needs_http_tools = True
    probes: list[tuple[int, int]] = []

    def __init__(self, clock, system):
        super().__init__(clock, MockProgram(**(system.params.get("mock") or {})))
        self.base = system.params["harness_tool_base"]

    async def open(self, call):
        async with httpx.AsyncClient(base_url=self.base) as c:
            h = await c.get("/healthz")
            p = await c.post(f"/tools/{call.tool_token}/get_balance", json={})
        HttpMockAgent.probes.append((h.status_code, p.status_code))
        await super().open(call)


def _setup(tmp_path: Path, system: dict, name: str) -> Path:
    sysdir = tmp_path / "systems"
    sysdir.mkdir()
    (sysdir / f"{name}.yaml").write_text(yaml.safe_dump(system), encoding="utf-8")
    th = tmp_path / "thresholds.yaml"
    th.write_text("eend_silence_ms: 1500\n", encoding="utf-8")
    cfg = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
    cfg.update(systems=[name], systems_dir=str(sysdir), thresholds=str(th), limit=1,
               results_dir=str(tmp_path / "results"))
    p = tmp_path / "run.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _mock_raw() -> dict:
    return yaml.safe_load((ROOT / "configs/systems/mock_stops.yaml").read_text(encoding="utf-8"))


def _run_card(tmp_path: Path) -> dict:
    db = next((tmp_path / "results").iterdir()) / "events.sqlite"
    conn = sqlite3.connect(str(db))
    card = json.loads(conn.execute("SELECT run_card_json FROM runs").fetchone()[0])
    conn.close()
    return card


def test_cli_run_mock_starts_no_server(tmp_path):
    cfg = _setup(tmp_path, _mock_raw(), "mock_stops")
    r = CliRunner().invoke(app, ["run", str(cfg), "--yes", "--allow-unregistered"])
    assert r.exit_code == 0, r.output
    assert "tool server" not in r.output
    card = _run_card(tmp_path)
    assert card["tool_server"] is None and "tool_server" not in card["systems"]["mock_stops"]


def test_cli_run_serves_tools_for_needs_http_tools(tmp_path, monkeypatch):
    port = _free_port()
    raw = _mock_raw()
    raw.update(system_id="mock_http", adapter="mock_http")
    raw["params"]["harness_tool_base"] = f"http://127.0.0.1:{port}"
    monkeypatch.setitem(systems_mod.ADAPTERS, "mock_http", f"{__name__}:HttpMockAgent")
    HttpMockAgent.probes = []
    cfg = _setup(tmp_path, raw, "mock_http")
    r = CliRunner().invoke(app, ["run", str(cfg), "--yes", "--allow-unregistered",
                                 "--tool-host", "127.0.0.1"])
    assert r.exit_code == 0, r.output
    out = r.output
    line = (f"tool server: listening on 127.0.0.1:{port} (Dograh must reach "
            f"http://127.0.0.1:{port}/tools/...)")
    assert line in out and out.index(line) < out.index("total projected")
    assert f"tool server: stopped (127.0.0.1:{port})" in out
    # during the call: healthz 200; the call has no tools so its token is unbound -> 404
    assert HttpMockAgent.probes == [(200, 404)]
    card = _run_card(tmp_path)
    ts = {"host": "127.0.0.1", "port": port, "bind": f"127.0.0.1:{port}"}
    assert card["tool_server"] == ts
    assert card["systems"]["mock_http"]["tool_server"] == ts
    assert card["systems"]["mock_http"]["harness_tool_base"] == f"http://127.0.0.1:{port}"
    bind_socket("127.0.0.1", port).close()            # released after the run


def test_cli_run_refusal_releases_port_and_busy_port_refused(tmp_path, monkeypatch):
    port = _free_port()
    raw = _mock_raw()
    raw.update(system_id="mock_http", adapter="mock_http")
    raw["params"]["harness_tool_base"] = f"http://127.0.0.1:{port}"
    monkeypatch.setitem(systems_mod.ADAPTERS, "mock_http", f"{__name__}:HttpMockAgent")
    cfg = _setup(tmp_path, raw, "mock_http")
    r = CliRunner().invoke(app, ["run", str(cfg), "--allow-unregistered",
                                 "--tool-host", "127.0.0.1"])           # no --yes
    assert r.exit_code == 1 and "tool server: listening" in r.output
    bind_socket("127.0.0.1", port).close()            # the refusal closed the socket
    busy = bind_socket("127.0.0.1", port)
    try:
        r = CliRunner().invoke(app, ["run", str(cfg), "--yes", "--allow-unregistered",
                                     "--tool-host", "127.0.0.1"])
    finally:
        busy.close()
    assert r.exit_code == 2 and f"port {port} in use" in r.output
    r = CliRunner().invoke(app, ["run", str(cfg), "--allow-unregistered", "--tool-port", "443"])
    assert r.exit_code == 2 and "443" in r.output
