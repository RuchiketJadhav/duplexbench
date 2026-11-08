"""Serve the ToolServer app over HTTP with uvicorn (fix-44; spec §4.2, dograh_stack step 4).

`create_app(server)` (tools/server.py) is the FastAPI app; this module puts it on a
port. Two users:

* `cli run`: when a system's adapter sets `needs_http_tools` (dograh_stack: Dograh's
  org-level http_api tools POST to `{harness_tool_base}/tools/{{call_id}}/<tool>`),
  `run_command` binds the socket before the projection (`bind_socket`, fail fast on a
  port in use) and `execute` serves it for the whole run (`ToolHttp.start` / `stop`).
* `duplexbench tools serve`: one test binding (`build_test_server`), no run, so a remote
  party can check reachability (`serve_test`).

uvicorn runs as a task on the caller's asyncio loop (`Server.serve(sockets=[sock])`
with `Config(loop="none")`), not in a thread: the runner is async, so HTTP tool calls
reach `ToolServer.invoke` on the same loop the call's RealClock and the injector sleep
on, exactly like the in-process ToolClient path. `EmbeddedServer` installs no signal
handlers, so Ctrl-C still stops the run (asyncio.Runner cancels the main task) and the
`finally` blocks stop the server instead of uvicorn swallowing the signal.

Ports (`port_from_base`): the port of `harness_tool_base` (`http://<host>:8900` -> 8900),
8900 when the URL has none. 80 and 443 are refused, and so is any scheme but http: the
harness serves plain HTTP behind no proxy.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from typing import Any, Callable
from urllib.parse import urlsplit

import uvicorn

DEFAULT_TOOL_PORT = 8900
DEFAULT_TOOL_HOST = "0.0.0.0"
REFUSED_PORTS = (80, 443)
TEST_TOKEN = "TEST"
TEST_DOMAIN = "billing"
STARTUP_TIMEOUT_S = 10.0
SHUTDOWN_TIMEOUT_S = 2           # uvicorn timeout_graceful_shutdown (s)


class ToolPortError(ValueError):
    """The tool server cannot use this URL or port."""


def port_from_base(base: str | None) -> int:
    """Port the harness listens on for a `harness_tool_base` URL (8900 when it has none)."""
    if not base:
        return DEFAULT_TOOL_PORT
    parts = urlsplit(str(base).strip())
    if parts.scheme and parts.scheme != "http":
        raise ToolPortError(f"harness_tool_base {base!r}: scheme {parts.scheme!r} not allowed; "
                            "the harness tool server serves plain http (no TLS, no proxy)")
    try:
        port = parts.port
    except ValueError as e:
        raise ToolPortError(f"harness_tool_base {base!r}: bad port ({e})") from None
    if port is None:
        return DEFAULT_TOOL_PORT
    check_port(port, f"harness_tool_base {base!r}")
    return port


def check_port(port: int, where: str = "tool port") -> int:
    if port in REFUSED_PORTS:
        raise ToolPortError(f"{where}: port {port} is not allowed; the harness serves plain "
                            f"HTTP behind no proxy, use e.g. {DEFAULT_TOOL_PORT}")
    if not 0 <= port <= 65535:
        raise ToolPortError(f"{where}: port {port} out of range")
    return port


def bind_socket(host: str, port: int) -> socket.socket:
    """A listening TCP socket on host:port; ToolPortError naming the port when it is taken.

    Windows: SO_EXCLUSIVEADDRUSE, so a listener already on the port (any address) makes
    this fail instead of silently sharing it. Elsewhere SO_REUSEADDR, as uvicorn does
    (TIME_WAIT is fine, a live listener still fails)."""
    check_port(port)
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError as e:
        sock.close()
        raise ToolPortError(f"tool server: cannot listen on {host}:{port} (port {port} in use "
                            f"or not bindable: {e.strerror or e}); free the port or pass "
                            "--tool-port") from None
    sock.setblocking(False)
    return sock


class EmbeddedServer(uvicorn.Server):
    """uvicorn.Server without signal capture (the owner's loop handles Ctrl-C)."""

    def capture_signals(self):  # type: ignore[override]
        import contextlib
        return contextlib.nullcontext()


class ToolHttp:
    """uvicorn serving `app` on an already bound socket, as a task on the running loop."""

    def __init__(self, app: Any, sock: socket.socket, *, access_log: bool = False) -> None:
        self.sock = sock
        self.host, self.port = sock.getsockname()[:2]
        # The run keeps logging config untouched (warnings only); `tools serve` (access_log)
        # uses uvicorn's own console config so every remote hit prints with its address.
        from uvicorn.config import LOGGING_CONFIG
        cfg = uvicorn.Config(app, loop="none", lifespan="off",
                             log_config=LOGGING_CONFIG if access_log else None,
                             log_level="info" if access_log else "warning",
                             access_log=access_log,
                             timeout_graceful_shutdown=SHUTDOWN_TIMEOUT_S)
        self.server = EmbeddedServer(cfg)
        self.task: asyncio.Task | None = None

    def info(self) -> dict:
        return {"host": self.host, "port": self.port, "bind": f"{self.host}:{self.port}"}

    async def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self.server.serve(sockets=[self.sock]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_TIMEOUT_S
        while not self.server.started:
            if self.task.done():
                exc = self.task.exception() if not self.task.cancelled() else None
                raise ToolPortError(f"tool server on {self.host}:{self.port} did not start: "
                                    f"{exc!r}")
            if loop.time() > deadline:
                await self.stop()
                raise ToolPortError(f"tool server on {self.host}:{self.port} did not start "
                                    f"within {STARTUP_TIMEOUT_S:.0f} s")
            await asyncio.sleep(0.005)

    async def stop(self) -> None:
        """Stop accepting, finish in-flight requests (bounded), close the socket."""
        t = self.task
        if t is not None and not t.done():
            self.server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(t), SHUTDOWN_TIMEOUT_S + 3)
            except (asyncio.TimeoutError, Exception):      # noqa: BLE001 - stop must not raise
                self.server.force_exit = True
                t.cancel()
                try:
                    await t
                except BaseException:                      # noqa: BLE001
                    pass
        self.sock.close()

    async def __aenter__(self) -> "ToolHttp":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


# -- `duplexbench tools serve` ---------------------------------------------------------
def build_test_server(token: str = TEST_TOKEN, domain: str = TEST_DOMAIN) -> Any:
    """A ToolServer with one binding: `token` -> the `domain` mock backend in its default
    (SEED) state, no scenario tooldefs (every tool exposed, no injection), no store. The
    same `register_call` path `cli run` uses per call."""
    from duplexbench.clock import RealClock
    from duplexbench.tools.mock_backends import get_backend
    from duplexbench.tools.server import ToolServer

    server = ToolServer()
    server.register_call(token, get_backend(domain)(), None, None, RealClock(),
                         run_id="tools-serve", call_id=token)
    return server


def example_lines(host: str, port: int, token: str, tools: list[str], domain: str) -> list[str]:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    base = f"http://{shown}:{port}"
    lines = [f"  GET  {base}/healthz", f"  GET  {base}/tools/{token}"]
    if domain == "billing" and "get_balance" in tools:
        lines.append(f"  POST {base}/tools/{token}/get_balance  "
                     '{"account_id": "AC-77"}  (200; {} or other keys -> 400 bad_arguments)')
    elif tools:
        lines.append(f"  POST {base}/tools/{token}/{tools[0]}  <JSON args>")
    return lines


async def serve_test(server: Any, sock: socket.socket, *, token: str, domain: str,
                     echo: Callable[[str], None], stop: asyncio.Event | None = None) -> None:
    """Serve `server` until `stop` is set (tests) or the task is cancelled (Ctrl-C)."""
    tools = server.binding(token).exposed()

    def on_tool(kind: str, info: dict) -> None:
        if kind == "result":
            echo(f"tool call {info.get('tool_call_id')}: {info.get('name')} -> {info.get('status')}")

    server.subscribe(token, on_tool)
    http = ToolHttp(server.app, sock, access_log=True)
    await http.start()
    try:
        echo(f"tool server: listening on {http.host}:{http.port} "
             f"(token {token}, {domain} mock backend: {', '.join(tools)})")
        for line in example_lines(http.host, http.port, token, tools, domain):
            echo(line)
        echo("a remote party uses this machine's public address in place of 127.0.0.1; "
             "Ctrl-C stops")
        if stop is None:
            await http.task  # type: ignore[misc]
        else:
            await stop.wait()
    finally:
        await http.stop()
