"""M4-T04: tool server over HTTP (httpx ASGI transport) vs in-process ToolClient.

Both paths run the same script on a fresh VirtualClock and a fresh Store; the
`tool_calls` rows and the final state must be identical.
"""
import asyncio
import sqlite3

import httpx
import pytest

from duplexbench.clock import VirtualClock
from duplexbench.scenarios.schema import ToolDef
from duplexbench.storage import Store
from duplexbench.tools.mock_backends.account import AccountBackend
from duplexbench.tools.server import TOOL_CALL_ID_HEADER, ToolServer

MS = 1_000_000
TOKEN = "tok-123"
TOOLDEFS = [
    ToolDef(name="verify_identity", kind="read", latency_ms=500),
    ToolDef(name="update_phone", kind="write", latency_ms=2000, result_anchors=["ending 0199"]),
    ToolDef(name="update_email", kind="write", latency_ms=5000, failure="timeout"),
    ToolDef(name="get_account", kind="read", latency_ms=0, failure="partial"),
]
SCRIPT = [  # (at_ms, tool, args, adapter tool_call_id or None); calls are sequential
    (100, "update_phone", {"customer_id": "C-100", "phone": "(415) 555-0199"}, "fc_1"),   # ->2100
    (2500, "verify_identity", {"customer_id": "C-100", "dob": "March 3 1990", "zip": "94107"},
     None),                                                                              # ->3000
    (3500, "update_phone", {"customer_id": "C-100", "phone": "(415) 555-0199"}, "fc_3"),  # ->5500
    (6000, "update_email", {"customer_id": "C-100", "email": "new@example.com"}, None),   # ->11000
    (12000, "get_account", {"customer_id": "C-100"}, None),
]
EXPECTED_STATUS = [403, 200, 200, 504, 200]


def _rows(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("select * from tool_calls order by start_ns, tool_call_id")]
    con.close()
    return rows


def _run(tmp_path, name, via_http: bool):
    store = Store(tmp_path / f"{name}.sqlite")
    server = ToolServer()
    events = []

    async def main(clock):
        server.register_call(TOKEN, AccountBackend(), TOOLDEFS, store, clock,
                             run_id="run1", call_id="call1")
        server.subscribe(TOKEN, lambda kind, info: events.append((kind, info["t_ns"])))
        statuses, bodies = [], []
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://harness") as http:
            client = server.client(TOKEN)
            for at_ms, tool, args, tcid in SCRIPT:
                await clock.sleep_until_ns(at_ms * MS)
                if via_http:
                    hdr = {TOOL_CALL_ID_HEADER: tcid} if tcid else {}
                    r = await http.post(f"/tools/{TOKEN}/{tool}", json=args, headers=hdr)
                    statuses.append(r.status_code)
                    bodies.append(r.json())
                else:
                    out = await client.outcome(tool, args, tcid)
                    statuses.append(out.status)
                    bodies.append(await _body(out))
            state = (await http.get(f"/state/{TOKEN}")).json()
        return statuses, bodies, state

    async def top():
        clock = VirtualClock()
        return await clock.run(main(clock))

    statuses, bodies, state = asyncio.run(top())
    store.flush_sync()
    rows = _rows(store.path)
    store.close()
    return statuses, bodies, state, rows, events


async def _body(out):
    return out.body if out.body is not None else {"error": "timeout"}


def test_http_and_client_paths_produce_identical_rows_and_state(tmp_path):
    s_http, b_http, st_http, rows_http, ev_http = _run(tmp_path, "http", via_http=True)
    s_cli, b_cli, st_cli, rows_cli, ev_cli = _run(tmp_path, "client", via_http=False)
    assert s_http == s_cli == EXPECTED_STATUS
    assert b_http == b_cli
    assert rows_http == rows_cli and len(rows_http) == len(SCRIPT)
    assert st_http == st_cli
    assert ev_http == ev_cli


def test_rows_carry_harness_clock_times_and_injection(tmp_path):
    _, bodies, state, rows, _ = _run(tmp_path, "rows", via_http=True)
    by = {(r["name"], r["start_ns"]): r for r in rows}
    up = by[("update_phone", 3500 * MS)]
    assert (up["tool_call_id"], up["result_ns"], up["injected_latency_ms"],
            up["injected_failure"]) == ("fc_3", 5500 * MS, 2000, "none")
    assert (up["run_id"], up["call_id"]) == ("run1", "call1")
    assert bodies[2]["result_anchors"] == ["ending 0199"]         # scenario-declared anchor
    first = by[("update_phone", 100 * MS)]                         # before verify: auth bypass
    assert '"code":"auth_required"' in first["result_json"] and first["tool_call_id"] == "fc_1"
    assert first["result_ns"] == 2100 * MS                         # refused after the latency
    to = by[("update_email", 6000 * MS)]
    assert (to["injected_failure"], to["result_ns"]) == ("timeout", 11000 * MS)
    assert by[("verify_identity", 2500 * MS)]["tool_call_id"] == f"{TOKEN}-2"
    assert "address" not in bodies[4] and "customer_id" in bodies[4]   # partial: sorted-first dropped
    # state endpoint: canonical state + hash; the timed-out email write did not commit
    cust = state["state"]["customers"]["C-100"]
    assert cust["phone"] == "4155550199" and cust["email"] == "jordan@example.com"
    assert len(state["hash"]) == 64


def test_unknown_token_and_tool_are_404(tmp_path):
    server = ToolServer()

    async def main(clock):
        server.register_call(TOKEN, AccountBackend(), TOOLDEFS, None, clock)
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://h") as http:
            a = await http.post("/tools/nope/update_phone", json={})
            b = await http.post(f"/tools/{TOKEN}/update_address", json={})   # not in tooldefs
            c = await http.get("/state/nope")
            d = await http.get(f"/tools/{TOKEN}")
            e = await http.post(f"/tools/{TOKEN}/verify_identity", content=b"[1]")
        unknown = await server.client(TOKEN).call("update_address", {})
        return a, b, c, d, e, unknown

    async def top():
        clock = VirtualClock()
        return await clock.run(main(clock))

    a, b, c, d, e, unknown = asyncio.run(top())
    assert (a.status_code, a.json()["error"]) == (404, "unknown_call_token")
    assert (b.status_code, b.json()) == (404, {"error": "unknown_tool", "detail": "update_address"})
    assert unknown == b.json()
    assert c.status_code == 404
    assert sorted(t["name"] for t in d.json()) == sorted(t.name for t in TOOLDEFS)
    assert e.status_code == 400


def test_register_rejects_duplicates_and_unknown_tooldefs():
    server = ToolServer()
    clock = VirtualClock()
    server.register_call("t1", AccountBackend(), [], None, clock)
    with pytest.raises(ValueError):
        server.register_call("t1", AccountBackend(), [], None, clock)
    with pytest.raises(ValueError):
        server.register_call("t2", AccountBackend(),
                             [ToolDef(name="book_appointment", kind="write")], None, clock)
