"""Clock-driven tool latency and failure injection (BUILD_PLAN M4-T03; spec §4.2 scenario features).

`Injector(clock, tooldef).apply(call)`:

    start_ns = clock.now_ns()
    sleep on the clock until start_ns + latency_ms
    failure = none     -> run `call()` and return its result            (HTTP 200)
              partial  -> run `call()`, drop one required field         (HTTP 200)
              error    -> do NOT run `call()`; HTTP 500 error body
              timeout  -> do NOT run `call()`; no result (body None);
                          result_ns = start + latency                  (HTTP 504 on the wire)
    a PolicyError from `call()` -> HTTP 403 with {"error": "policy_violation", "code": ...}
    a ToolArgError from `call()` -> HTTP 400 with {"error": "bad_arguments", ...}

The backend runs *after* the latency so that the state changes at the moment
the result is delivered (the write is "committed" at result_ns). For error
and timeout the backend is not called, so the state does not change: the
agent cannot know whether a timed-out write happened, and the mock makes the
conservative choice that it did not.

`partial` drops the first field (sorted by name) of the result other than
`result_anchors`, unless `drop_field` names one.
"""
from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable

from duplexbench.clock import Clock
from duplexbench.scenarios.schema import ToolDef
from duplexbench.tools.state import PolicyError, ToolArgError

NS_PER_MS = 1_000_000
HTTP_OK = int(HTTPStatus.OK)
HTTP_BAD = int(HTTPStatus.BAD_REQUEST)
HTTP_POLICY = int(HTTPStatus.FORBIDDEN)
HTTP_ERROR = int(HTTPStatus.INTERNAL_SERVER_ERROR)
HTTP_TIMEOUT = int(HTTPStatus.GATEWAY_TIMEOUT)


@dataclass
class Injected:
    status: int
    body: dict | None
    failure: str              # the injected failure mode: none | timeout | error | partial
    latency_ms: int
    start_ns: int
    result_ns: int
    policy_error: str | None = None


class Injector:
    def __init__(self, clock: Clock, tooldef: ToolDef, drop_field: str | None = None) -> None:
        self.clock = clock
        self.tooldef = tooldef
        self.drop_field = drop_field

    async def apply(self, call: Callable[[], dict], start_ns: int | None = None) -> Injected:
        td = self.tooldef
        start = self.clock.now_ns() if start_ns is None else start_ns
        due = start + td.latency_ms * NS_PER_MS
        if due > self.clock.now_ns():
            await self.clock.sleep_until_ns(due)
        if td.failure == "timeout":
            return Injected(HTTP_TIMEOUT, None, "timeout", td.latency_ms, start, due)
        if td.failure == "error":
            return Injected(HTTP_ERROR, {"error": "internal_error", "detail": "injected"},
                            "error", td.latency_ms, start, self.clock.now_ns())
        try:
            body = call()
        except PolicyError as e:
            return Injected(HTTP_POLICY, {"error": "policy_violation", "code": e.code,
                                          "detail": e.detail},
                            td.failure, td.latency_ms, start, self.clock.now_ns(), e.code)
        except ToolArgError as e:
            return Injected(HTTP_BAD, {"error": "bad_arguments", "detail": str(e)},
                            td.failure, td.latency_ms, start, self.clock.now_ns())
        if td.failure == "partial":
            body = dict(body)
            keys = sorted(k for k in body if k != "result_anchors")
            victim = self.drop_field if self.drop_field in body else (keys[0] if keys else None)
            if victim is not None:
                body.pop(victim)
        return Injected(HTTP_OK, body, td.failure, td.latency_ms, start, self.clock.now_ns())
