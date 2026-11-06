"""`WsAdapter`: the receive loop the M6 WebSocket adapters share, on `LiveStream`.

The socket, its receive task, timestamps and the close rule live in
`transport/live_stream.py` (one implementation for every live adapter):

    socket closed after our own close()         -> closed {"by": "harness"}
    provider closed cleanly (1000 / 1001)        -> closed {"by": "provider"}
    abnormal close / reset / read error          -> error {"type": "transport", ...}, then closed

Subclasses implement `open` (calling `_connect`), `send_frame`, `tool_result` and
`_map(msg, t_ns) -> events`; `t_ns` is LiveStream's receive stamp. The LiveStream
is `self.live` (its `stats()` lands in calls.flags_json["stream"]).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any

from duplexbench.adapters._convert import dumps, params_of, system_dict
from duplexbench.adapters.base import Adapter, AdapterEvent
from duplexbench.transport.live_stream import Connector, LiveStream

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

OPEN_TIMEOUT_S = 30.0


class WsAdapter(Adapter):
    """Receive loop shared by the M6 adapters."""

    def __init__(self, clock: "Clock", config: Any, connector: Connector | None = None):
        self.clock = clock
        self.config = system_dict(config)
        self.acfg: dict = params_of(config)
        self._connector = connector
        self.live: LiveStream | None = None
        self.call: "CallContext | None" = None
        self._closing = False
        self.raw_log: list[tuple[int, str]] = []        # (t_ns, provider event) of unmapped msgs

    @property
    def ws(self) -> LiveStream | None:
        return self.live

    async def _connect(self, url: str, headers: dict[str, str] | None = None) -> LiveStream:
        self.live = LiveStream(self.clock, url, dict(headers or {}), connector=self._connector,
                               open_timeout_s=float(self.acfg.get("open_timeout_s")
                                                    or OPEN_TIMEOUT_S))
        await self.live.connect()
        return self.live

    async def _send(self, message: str | bytes) -> None:
        if self.live is None or self._closing:
            return
        await self.live.send(message)       # a dead socket drops + counts; reader reports it

    async def _send_json(self, obj: dict) -> None:
        await self._send(dumps(obj))

    def _initial_events(self) -> Iterable[AdapterEvent]:
        return ()

    def _map(self, msg: str | bytes, t_ns: int) -> Iterable[AdapterEvent]:
        raise NotImplementedError

    def events(self) -> AsyncIterator[AdapterEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[AdapterEvent]:
        for ev in self._initial_events():
            yield ev
        live = self.live
        if live is None:
            raise RuntimeError(f"{self.name}: events() before open()")
        async for m in live.messages():
            if m.kind in ("text", "binary"):
                for ev in self._map(m.data, m.t_ns):
                    yield ev
                    if ev.kind == "closed":
                        return
            elif m.kind == "error":
                yield AdapterEvent("error", m.t_ns, "ws.close", dict(m.info))
            else:
                yield AdapterEvent("closed", m.t_ns, "ws.close", dict(m.info))
                return

    async def close(self) -> None:
        self._closing = True
        if self.live is not None:
            await self.live.close()
