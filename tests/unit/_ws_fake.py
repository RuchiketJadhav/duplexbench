"""Test helpers for the M6 adapters: a localhost `websockets` fake server and a driver.

No provider is contacted; every fake binds 127.0.0.1 on an ephemeral port.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from websockets.asyncio.server import serve

from duplexbench.clock import RealClock
from duplexbench.context import CallContext

MS = 1_000_000
TIMEOUT_S = 10


@contextlib.asynccontextmanager
async def fake_server(handler: Callable[..., Awaitable[None]]) -> AsyncIterator[str]:
    """Yield `ws://127.0.0.1:<port>`; the handler gets (ws, path)."""
    async def _h(ws):
        await handler(ws, ws.request.path)

    async with serve(_h, "127.0.0.1", 0, max_size=None) as srv:
        port = srv.sockets[0].getsockname()[1] if hasattr(srv, "sockets") else None
        if port is None:
            port = list(srv.server.sockets)[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


def ctx(clock=None, token="tok-abc", condition="C0") -> CallContext:
    return CallContext(run_id="run1", call_id="call1", scenario=None, trial=0,
                       condition=condition, system_id="sys", is_control=False,
                       clock=clock or RealClock(), store=None, tool_token=token)


async def collect(adapter, call, driver: Callable[[], Awaitable[None]] | None = None) -> list:
    """Run safe_events to `closed` while `driver()` sends frames; returns the events."""
    out = []

    async def _recv():
        async for ev in adapter.safe_events(call):
            out.append(ev)

    t = asyncio.create_task(_recv())
    if driver is not None:
        await driver()
    await asyncio.wait_for(t, TIMEOUT_S)
    return out


def kinds(events) -> list[str]:
    return [e.kind for e in events]
