"""Live stream transport: one provider WebSocket per call (spec §6.1; BUILD_PLAN M3-T04).

    ls = LiveStream(clock, url, headers={"Authorization": "Bearer ..."})
    await ls.connect()                         # raises on connect failure (runner -> transport_error)
    await ls.send_json({...}); await ls.send_bytes(b"...")
    async for m in ls.messages():              # StreamMessage(kind, t_ns, data, info)
        ...                                     # kind: "text" | "binary" | "error" | "closed"
    await ls.close()

Receive loop: one task reads the socket and puts every message on an
`asyncio.Queue`, stamped with `clock.now_ns()` at receive (the harness
observation time of spec §1.1). Real sockets are awaited directly, never via
`clock.idle`: this transport runs under a RealClock only.

Reconnect policy: OFF. The close rule (adapters/ws_adapter.py maps it to adapter events):

    socket closed after our own close()         -> closed {"by": "harness"}
    provider closed cleanly (1000 / 1001)        -> closed {"by": "provider"}
    abnormal close / reset / read error          -> error {"type": "transport", ...}, then closed

`messages()` ends after `closed`. Sends after the socket is gone are dropped and
counted (`dropped_sends`), never raised: the drop is reported once, as the
`error` above, through the event stream.

Pacing: `send_*` records how long each send blocked (`send_block_ns`); the
caller-frame pacing slip itself is logged by the runner (flags.pacing_slip_p99_ms).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from duplexbench.clock import Clock

_CLEAN_CODES = (1000, 1001)      # RFC 6455 normal closure / going away  # threshold-ok: protocol code
_P99 = 99


class WsLike(Protocol):
    async def send(self, message: str | bytes) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


Connector = Callable[[str, dict[str, str]], Awaitable[WsLike]]


async def ws_connect(url: str, headers: dict[str, str] | None = None, *,
                     open_timeout: float = 15.0) -> WsLike:
    from websockets.asyncio.client import connect
    return await connect(url, additional_headers=headers or None, max_size=None,
                         open_timeout=open_timeout, ping_interval=None, compression=None)


@dataclass
class StreamMessage:
    kind: Literal["text", "binary", "error", "closed"]
    t_ns: int
    data: str | bytes | None = None
    info: dict[str, Any] = field(default_factory=dict)


class LiveStream:
    def __init__(self, clock: "Clock", url: str, headers: dict[str, str] | None = None, *,
                 connector: Connector | None = None, open_timeout_s: float = 15.0):
        self.clock = clock
        self.url = url
        self.headers = dict(headers or {})
        self._connector = connector
        self.open_timeout_s = open_timeout_s
        self.ws: WsLike | None = None
        self.queue: asyncio.Queue[StreamMessage] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._closing = False
        self._gone = False             # socket no longer usable (closed or dropped)
        self.sent = 0
        self.received = 0
        self.dropped_sends = 0
        self.send_block_ns: list[int] = []
        self.t_connect_ns: int | None = None

    # -- lifecycle --------------------------------------------------------------
    async def connect(self) -> None:
        if self._connector is not None:
            self.ws = await self._connector(self.url, self.headers)
        else:
            self.ws = await ws_connect(self.url, self.headers, open_timeout=self.open_timeout_s)
        self.t_connect_ns = self.clock.now_ns()
        self._reader = asyncio.get_running_loop().create_task(self._read())

    async def close(self) -> None:
        self._closing = True
        ws, self._gone = self.ws, True
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not an error
                pass
        if self._reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._reader), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._reader.cancel()

    def abort(self) -> None:
        """Kill the transport without a close handshake (chaos: a dropped socket)."""
        tr = getattr(self.ws, "transport", None)
        if tr is not None:
            tr.abort()

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self._gone

    # -- send -------------------------------------------------------------------
    async def send(self, message: str | bytes) -> bool:
        if self.ws is None or self._gone:
            self.dropped_sends += 1
            return False
        t0 = self.clock.now_ns()
        try:
            await self.ws.send(message)
        except Exception:  # noqa: BLE001 - the reader reports the drop as error+closed
            self.dropped_sends += 1
            return False
        self.send_block_ns.append(self.clock.now_ns() - t0)
        self.sent += 1
        return True

    async def send_json(self, obj: dict) -> bool:
        return await self.send(json.dumps(obj, separators=(",", ":")))

    async def send_bytes(self, b: bytes) -> bool:
        return await self.send(bytes(b))

    # -- receive ----------------------------------------------------------------
    async def _read(self) -> None:
        from websockets.exceptions import ConnectionClosed
        q, ws = self.queue, self.ws
        assert ws is not None
        try:
            async for msg in ws:
                self.received += 1
                q.put_nowait(StreamMessage("text" if isinstance(msg, str) else "binary",
                                           self.clock.now_ns(), msg))
            code = _close_code(ws)
            self._finish(code, None)
        except ConnectionClosed as e:
            self._finish(_close_code(ws, e), e)
        except asyncio.CancelledError:
            self._gone = True
            q.put_nowait(StreamMessage("closed", self.clock.now_ns(), info={"by": "harness"}))
            raise
        except Exception as e:  # noqa: BLE001 - any read failure is a transport error
            self._finish(None, e)

    def _finish(self, code: int | None, exc: BaseException | None) -> None:
        self._gone = True
        t = self.clock.now_ns()
        if self._closing:
            self.queue.put_nowait(StreamMessage("closed", t, info={"by": "harness", "code": code}))
        elif code in _CLEAN_CODES:
            self.queue.put_nowait(StreamMessage("closed", t, info={"by": "provider", "code": code}))
        else:
            self.queue.put_nowait(StreamMessage("error", t, info={
                "type": "transport", "code": code,
                "message": f"{type(exc).__name__}: {exc}" if exc else f"socket closed ({code})"}))
            self.queue.put_nowait(StreamMessage("closed", t, info={"by": "provider", "code": code}))

    async def recv(self) -> StreamMessage:
        """Next message (awaited directly: real network, not clock time)."""
        return await self.queue.get()

    async def messages(self) -> AsyncIterator[StreamMessage]:
        while True:
            m = await self.queue.get()
            yield m
            if m.kind == "closed":
                return

    def stats(self) -> dict[str, Any]:
        blk = sorted(self.send_block_ns)
        p99 = blk[min(len(blk) - 1, len(blk) * _P99 // 100)] / 1e6 if blk else None
        return {"sent": self.sent, "received": self.received, "dropped_sends": self.dropped_sends,
                "send_block_p99_ms": p99}


def _close_code(ws: Any, e: BaseException | None = None) -> int | None:
    for src in (getattr(e, "rcvd", None), getattr(ws, "close_code", None)):
        if isinstance(src, int):
            return src
        code = getattr(src, "code", None)
        if code is not None:
            return int(code)
    return None
