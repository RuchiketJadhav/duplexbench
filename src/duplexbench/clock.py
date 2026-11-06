# clock_info() and the perf_counter rationale are copied from
# C:\Users\Lenovo\research\asr-bench\src\asr_bench\clock.py (asr-bench, same author).
"""The one clock every duration in duplex-bench is measured on (spec §1.1; BUILD_PLAN §2.1).

The stdlib monotonic clock is GetTickCount64 on Windows with Python 3.12: 15.6 ms
resolution, useless against a 5 ms calibration tolerance. `perf_counter_ns` is
monotonic on every platform (QueryPerformanceCounter on Windows, CLOCK_MONOTONIC
on Linux) with sub-microsecond resolution. Never mix it with wall-clock time.

Two implementations of one protocol:

* `RealClock`    -- perf_counter_ns relative to the run origin; sleeps on the
                    precise loop from `_loop.py`.
* `VirtualClock` -- deterministic simulated time for tests and golden replays.

VirtualClock accounting (BUILD_PLAN §2.1, M1-T02)
-------------------------------------------------
`_active` counts accounted tasks (spawned with `clock.task`) that are neither
sleeping on the clock nor waiting in `clock.idle`. Time advances only when
`_active == 0`: the heap of `(t_ns, seq, future)` is popped, `_now` jumps to the
smallest `t_ns`, and every future with that same `t_ns` is resolved in `seq`
(registration) order. Same-time events are therefore ordered by registration.

Rules for code that runs under a Clock:

* Await something another accounted task will complete (asyncio.Queue,
  asyncio.Event, a Future) only as `await clock.idle(aw)`. An unwrapped await
  keeps the task "active" and freezes virtual time; if nothing else can run,
  the watchdog fails every pending clock wait with `DeadlockError`.
* Await something completed from *outside* the loop (a thread such as
  `Store.flush()`, a real socket) directly, never through `idle`: virtual time
  then holds still until it completes, which keeps replays deterministic.
  Wrapping it in `idle` lets `_active` reach 0 with an empty heap, which is
  reported as `DeadlockError`.
* Spawn every concurrent coroutine with `clock.task(coro)` so it is accounted.
  The top-level coroutine must itself be accounted: use `await clock.run(main())`
  (equivalent to `await clock.task(main())`).
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import Any, Awaitable, Coroutine, Protocol, TypeVar, runtime_checkable

from duplexbench import _loop

T = TypeVar("T")


class DeadlockError(RuntimeError):
    """Virtual time cannot advance: no accounted task can make progress (BUILD_PLAN §2.1)."""


def clock_info() -> dict:
    info = time.get_clock_info("perf_counter")
    return {"name": "perf_counter", "implementation": info.implementation,
            "resolution_s": info.resolution, "monotonic": info.monotonic}


@runtime_checkable
class Clock(Protocol):
    def now_ns(self) -> int: ...                        # ns since the run origin; starts at 0
    async def sleep_until_ns(self, t_ns: int) -> None: ...
    async def idle(self, aw: Awaitable[T]) -> T: ...    # await something that is not time
    def task(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]: ...  # accounted spawn


class RealClock:
    """perf_counter_ns relative to the origin; sleeps on the precise loop (spec §1.1)."""

    name = "real"

    def __init__(self, origin_ns: int | None = None) -> None:
        self._origin = time.perf_counter_ns() if origin_ns is None else origin_ns

    def now_ns(self) -> int:
        return time.perf_counter_ns() - self._origin

    async def sleep_until_ns(self, t_ns: int) -> None:
        await _loop.sleep_until_ns(t_ns, self.now_ns)

    async def idle(self, aw: Awaitable[T]) -> T:
        return await aw

    def task(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        return asyncio.get_running_loop().create_task(coro)

    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Same helper as VirtualClock.run, so callers need not branch on the clock."""
        return await self.task(coro)


class VirtualClock:
    """Deterministic simulated clock (BUILD_PLAN §2.1, M1-T02).

    `deadlock_wall_s` is a wall-time watchdog, not a benchmark threshold: if no
    clock operation happens for that long while accounted tasks are alive and
    `_active > 0`, some task is blocked on an unwrapped await; every pending
    clock wait is then failed with `DeadlockError` and the blocked tasks are
    cancelled.
    """

    name = "virtual"

    def __init__(self, deadlock_wall_s: float = 5.0) -> None:
        self._now = 0
        self._heap: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()
        self._active = 0
        self._live: set[asyncio.Task] = set()
        self._idle_waiters: set[asyncio.Future] = set()
        self._ops = 0
        self._deadlock_wall_s = deadlock_wall_s
        self._watch_armed = False
        self._watch_ops = -1
        self._failed: DeadlockError | None = None
        self._advance_scheduled = False

    # -- protocol ---------------------------------------------------------
    def now_ns(self) -> int:
        return self._now

    async def sleep_until_ns(self, t_ns: int) -> None:
        self._check_failed()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        heapq.heappush(self._heap, (max(t_ns, self._now), next(self._seq), fut))
        self._dec()
        try:
            await fut                 # _maybe_advance re-increments _active on resolve
        except asyncio.CancelledError:
            if fut.cancelled():
                self._active += 1     # running again to handle the cancel
            raise
        except DeadlockError:
            self._active += 1
            raise

    async def idle(self, aw: Awaitable[T]) -> T:
        self._check_failed()
        loop = asyncio.get_running_loop()
        inner = asyncio.ensure_future(aw)
        outer: asyncio.Future = loop.create_future()
        state = {"counted": False}

        def _back() -> None:
            if not state["counted"]:
                state["counted"] = True
                self._active += 1
                self._ops += 1
            self._idle_waiters.discard(outer)

        def _done(f: asyncio.Future) -> None:
            _back()                   # runs before the waiting task resumes
            if not f.cancelled():
                f.exception()         # mark retrieved even if `outer` already failed
            if outer.done():
                return
            if f.cancelled():
                outer.cancel()
            elif f.exception() is not None:
                outer.set_exception(f.exception())
            else:
                outer.set_result(f.result())

        self._idle_waiters.add(outer)
        self._dec()
        inner.add_done_callback(_done)
        try:
            return await outer
        except BaseException:
            _back()
            if not inner.done():
                inner.cancel()
            raise

    def task(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        loop = asyncio.get_running_loop()
        self._active += 1
        t = loop.create_task(coro)
        self._live.add(t)
        t.add_done_callback(self._task_done)
        self._arm_watchdog()
        return t

    # -- convenience --------------------------------------------------------
    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run `coro` as an accounted task and return its result (tests, golden replays)."""
        return await self.task(coro)

    # -- internals ----------------------------------------------------------
    def _check_failed(self) -> None:
        if self._failed is not None:
            raise DeadlockError(str(self._failed))

    def _dec(self) -> None:
        self._active -= 1
        self._ops += 1
        self._schedule_advance()

    def _schedule_advance(self) -> None:
        if not self._advance_scheduled:
            self._advance_scheduled = True
            asyncio.get_running_loop().call_soon(self._maybe_advance)

    def _task_done(self, t: asyncio.Task) -> None:
        self._live.discard(t)
        self._dec()

    def _maybe_advance(self) -> None:
        self._advance_scheduled = False
        if self._active > 0 or self._failed is not None:
            return
        # Advance only when the loop is quiescent: completions still in flight
        # (a gather resolving, a queue getter waking) must land at the current
        # time before time moves or a deadlock is declared. `_ready` is the
        # CPython BaseEventLoop ready queue.
        # Advance callbacks of *any* VirtualClock are ignored: two clocks in one
        # loop would otherwise each see the other's callback and livelock.
        if not self._live and not self._heap and not self._idle_waiters:
            return                             # finished clock: nothing to advance
        if _loop_busy(asyncio.get_running_loop()):
            self._schedule_advance()
            return
        while self._heap and self._heap[0][2].done():
            heapq.heappop(self._heap)          # cancelled sleepers
        if not self._heap:
            if self._idle_waiters:
                self._fail("every accounted task is idle and no sleeper is scheduled")
            return
        t_ns = self._heap[0][0]
        self._now = t_ns
        self._ops += 1
        while self._heap and self._heap[0][0] == t_ns:
            _, _, fut = heapq.heappop(self._heap)
            if fut.done():
                continue
            self._active += 1
            fut.set_result(None)

    def _fail(self, why: str) -> None:
        msg = f"{why} (virtual t={self._now} ns)"
        self._failed = DeadlockError(msg)
        for _, _, fut in self._heap:
            if not fut.done():
                fut.set_exception(DeadlockError(msg))
        self._heap.clear()
        for fut in list(self._idle_waiters):
            if not fut.done():
                fut.set_exception(DeadlockError(msg))
        self._idle_waiters.clear()

    def _arm_watchdog(self) -> None:
        if self._watch_armed:
            return
        self._watch_armed = True
        self._watch_ops = self._ops
        asyncio.get_running_loop().call_later(self._deadlock_wall_s, self._watch)

    def _watch(self) -> None:
        self._watch_armed = False
        if not self._live or self._failed is not None:
            return
        if self._ops == self._watch_ops and self._active > 0:
            sleepers = {fut for _, _, fut in self._heap} | set(self._idle_waiters)
            self._fail("no clock progress: a task awaits something not wrapped in clock.idle")
            # Tasks blocked outside the clock cannot receive the exception; cancel
            # them so the program ends instead of hanging.
            if sleepers or self._live:
                for t in list(self._live):
                    if not _parked(t, sleepers):
                        t.cancel(msg="DeadlockError")
            return
        self._arm_watchdog()


def _loop_busy(loop: asyncio.AbstractEventLoop) -> bool:
    """True if the ready queue holds work other than VirtualClock advance callbacks."""
    ready = getattr(loop, "_ready", None)
    if not ready:
        return False
    for h in ready:
        if getattr(h, "_cancelled", False):
            continue
        cb = getattr(h, "_callback", None)
        if getattr(cb, "__func__", None) is VirtualClock._maybe_advance:
            continue
        return True
    return False


def _parked(task: asyncio.Task, waits: set[asyncio.Future]) -> bool:
    waiter = getattr(task, "_fut_waiter", None)
    return waiter in waits
