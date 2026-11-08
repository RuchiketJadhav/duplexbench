"""Tool server: per-call token registry + FastAPI app (BUILD_PLAN §2.8, M4-T04; spec §4.2).

    runner                                  agent under test
    ------                                  ----------------
    server.register_call(token, backend,    Dograh workflow:  POST /tools/{token}/{tool}  (JSON args)
                         tooldefs, store,   hosted adapter:   ToolClient(server, token).call(name, args)
                         clock)                       \\               /
                                                       ToolServer.invoke   <- the one code path
                                                        |  Injector (latency/failure on the harness clock)
                                                        |  Backend.call (state, policy)
                                                        v
                                               store.put("tool_calls", row)

Both paths call `ToolServer.invoke`, so the `tool_calls` rows are identical:
`start_ns` is the harness clock when the request reached the server,
`result_ns` when the result (or the injected failure) was produced.
`tool_call_id` is the adapter's id when given (HTTP: `X-Tool-Call-Id`
header), else `<token>-<n>` counting calls per token.

Routes:
    POST /tools/{token}/{tool}   -> 200 result | 400 bad args | 403 policy | 404 unknown
                                    token or tool | 500 injected error | 504 injected timeout
    GET  /tools/{token}          -> the tool schemas exposed for this call (each has "name");
                                    404 unknown_call_token
    GET  /state/{token}          -> {"state": canonical state, "hash": sha256}
    GET  /healthz                -> {"ok": true}, no token (tunnel / security-group checks, fix-44)

Nothing here listens on a port: `tools/http.py` serves `create_app(server)` with
uvicorn, inside `cli run` for adapters with `needs_http_tools` and in `tools serve`.

Listeners (`subscribe(token, cb)`) get `cb("start" | "result", info)` so the
simulator can track `tool_pending` (spec §7) without polling.

Under a VirtualClock the HTTP handler must be driven from an accounted task
(e.g. a test awaiting an httpx ASGI request); the injector's clock sleep then
balances its own accounting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Callable, Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from duplexbench.clock import Clock
from duplexbench.scenarios.schema import ToolDef
from duplexbench.storage import Store, canonical_json
from duplexbench.tools.latency_injector import HTTP_BAD, Injected, Injector
from duplexbench.tools.state import Backend

if TYPE_CHECKING:
    from duplexbench.tools.client import ToolClient

HTTP_NOT_FOUND = int(HTTPStatus.NOT_FOUND)
TOOL_CALL_ID_HEADER = "X-Tool-Call-Id"
Listener = Callable[[str, dict], None]


class UnknownCall(KeyError):
    """No call registered under this token."""


class UnknownTool(KeyError):
    """The tool is not exposed for this call."""


@dataclass
class CallBinding:
    token: str
    backend: Backend
    tooldefs: dict[str, ToolDef]
    store: Store | None
    clock: Clock
    run_id: str
    call_id: str
    listeners: list[Listener] = field(default_factory=list)
    n_calls: int = 0

    def exposed(self) -> list[str]:
        names = [t.name for t in self.backend.tools()]
        return [n for n in names if n in self.tooldefs] if self.tooldefs else names

    def tooldef(self, name: str) -> ToolDef:
        if name not in self.exposed():
            raise UnknownTool(name)
        td = self.tooldefs.get(name)
        if td is None:                                  # no scenario tooldefs: no injection
            td = ToolDef(name=name, kind=self.backend.tool(name).kind)
        return td


@dataclass
class ToolOutcome:
    tool_call_id: str
    status: int
    body: dict | None
    injected: Injected
    row: dict


class ToolServer:
    def __init__(self) -> None:
        self._calls: dict[str, CallBinding] = {}
        self.app = create_app(self)

    # -- registry ----------------------------------------------------------------
    def register_call(self, token: str, backend: Backend,
                      tooldefs: Iterable[ToolDef] | dict[str, ToolDef] | None,
                      store: Store | None, clock: Clock, *, run_id: str | None = None,
                      call_id: str | None = None) -> CallBinding:
        if token in self._calls:
            raise ValueError(f"call token {token!r} already registered")
        tds = dict(tooldefs) if isinstance(tooldefs, dict) else {t.name: t for t in tooldefs or ()}
        unknown = sorted(set(tds) - {t.name for t in backend.tools()})
        if unknown:
            raise ValueError(f"scenario tools not in the {backend.domain} backend: {unknown}")
        backend.bind_anchors({n: list(t.result_anchors) for n, t in tds.items()
                              if t.result_anchors})
        b = CallBinding(token, backend, tds, store, clock, run_id or token, call_id or token)
        self._calls[token] = b
        return b

    def unregister_call(self, token: str) -> None:
        self._calls.pop(token, None)

    def binding(self, token: str) -> CallBinding:
        try:
            return self._calls[token]
        except KeyError:
            raise UnknownCall(token) from None

    def subscribe(self, token: str, cb: Listener) -> None:
        self.binding(token).listeners.append(cb)

    def client(self, token: str) -> "ToolClient":
        from duplexbench.tools.client import ToolClient
        return ToolClient(self, token)

    # -- the one call path ---------------------------------------------------------
    def tool_schemas(self, token: str) -> list[dict]:
        b = self.binding(token)
        return [b.backend.tool(n).to_dict() for n in b.exposed()]

    async def invoke(self, token: str, name: str, args: dict | None,
                     tool_call_id: str | None = None) -> ToolOutcome:
        b = self.binding(token)
        td = b.tooldef(name)                            # UnknownTool before any row
        b.n_calls += 1
        tcid = tool_call_id or f"{token}-{b.n_calls}"
        args = dict(args or {})
        start = b.clock.now_ns()
        info = {"tool_call_id": tcid, "name": name, "kind": td.kind, "t_ns": start}
        for cb in list(b.listeners):
            cb("start", dict(info))
        inj = await Injector(b.clock, td).apply(lambda: b.backend.call(name, args),
                                                start_ns=start)
        row = {"run_id": b.run_id, "call_id": b.call_id, "tool_call_id": tcid, "name": name,
               "args_json": canonical_json(args), "start_ns": inj.start_ns,
               "result_ns": inj.result_ns, "injected_latency_ms": inj.latency_ms,
               "injected_failure": inj.failure,
               "result_json": canonical_json({"status": inj.status, "body": inj.body})}
        if b.store is not None:
            b.store.put("tool_calls", row)
        for cb in list(b.listeners):
            cb("result", {**info, "t_ns": inj.result_ns, "status": inj.status})
        return ToolOutcome(tcid, inj.status, inj.body, inj, row)

    def state(self, token: str) -> dict:
        b = self.binding(token)
        return {"state": b.backend.state(), "hash": b.backend.snapshot_hash()}


def _not_found(what: str, err: KeyError) -> JSONResponse:
    return JSONResponse({"error": f"unknown_{what}", "detail": str(err.args[0])},
                        status_code=HTTP_NOT_FOUND)


def create_app(server: ToolServer) -> FastAPI:
    app = FastAPI(title="duplexbench tool server")

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/tools/{token}/{tool}")
    async def call_tool(token: str, tool: str, request: Request) -> JSONResponse:
        raw = await request.body()
        try:
            args = await request.json() if raw else {}
        except ValueError:
            return JSONResponse({"error": "bad_arguments", "detail": "body is not JSON"},
                                status_code=HTTP_BAD)
        if not isinstance(args, dict):
            return JSONResponse({"error": "bad_arguments", "detail": "body must be an object"},
                                status_code=HTTP_BAD)
        try:
            out = await server.invoke(token, tool, args,
                                      request.headers.get(TOOL_CALL_ID_HEADER))
        except UnknownCall as e:
            return _not_found("call_token", e)
        except UnknownTool as e:
            return _not_found("tool", e)
        body = out.body if out.body is not None else {"error": "timeout"}
        return JSONResponse(body, status_code=out.status,
                            headers={TOOL_CALL_ID_HEADER: out.tool_call_id})

    @app.get("/tools/{token}")
    async def list_tools(token: str) -> JSONResponse:
        try:
            return JSONResponse(server.tool_schemas(token))
        except UnknownCall as e:
            return _not_found("call_token", e)

    @app.get("/state/{token}")
    async def get_state(token: str) -> JSONResponse:
        try:
            return JSONResponse(server.state(token))
        except UnknownCall as e:
            return _not_found("call_token", e)

    return app
