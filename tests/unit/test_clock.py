"""M1-T01 (RealClock) and M1-T02 (VirtualClock); BUILD_PLAN §2.1, spec §1.1."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from duplexbench import _loop
from duplexbench.clock import Clock, DeadlockError, RealClock, VirtualClock, clock_info

MS = 1_000_000
SRC = Path(__file__).resolve().parents[2] / "src"


# -- M1-T01 ---------------------------------------------------------------------
def test_realclock_non_decreasing():
    c = RealClock()
    vals = [c.now_ns() for _ in range(1_000)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    assert vals[0] >= 0


def test_clock_info_is_perf_counter():
    assert clock_info()["name"] == "perf_counter"


def test_both_clocks_satisfy_protocol():
    assert isinstance(RealClock(), Clock)
    assert isinstance(VirtualClock(), Clock)


def test_real_sleep_on_precise_loop():
    async def main():
        c = RealClock()
        t0 = c.now_ns()
        await c.sleep_until_ns(t0 + 20 * MS)
        return c.now_ns() - t0

    took = _loop.run(main())
    assert took >= 20 * MS - MS // 2     # lower bound (sub-ms rounding allowed)
    # Upper bound (<= 30 ms) skipped: shared CI/laptop schedulers are not a timing rig;
    # calibration (M1-T14) measures pacing on the real host.


def test_no_monotonic_under_src():
    needle = "time." + "monotonic"
    hits = [p for p in SRC.rglob("*.py") if needle in p.read_text(encoding="utf-8")]
    assert hits == []


# -- M1-T02 ---------------------------------------------------------------------
def test_two_sleepers_wake_in_time_order(run_virtual):
    async def main(clock):
        woke = []

        async def sleeper(t):
            await clock.sleep_until_ns(t)
            woke.append((t, clock.now_ns()))

        a = clock.task(sleeper(100 * MS))
        b = clock.task(sleeper(50 * MS))
        await clock.idle(asyncio.gather(a, b))
        return woke

    woke, _ = run_virtual(main)
    assert woke == [(50 * MS, 50 * MS), (100 * MS, 100 * MS)]


def test_same_time_sleepers_wake_in_registration_order(run_virtual):
    async def main(clock):
        woke = []

        async def sleeper(name):
            await clock.sleep_until_ns(10 * MS)
            woke.append(name)

        ts = [clock.task(sleeper(n)) for n in ("a", "b", "c")]
        await clock.idle(asyncio.gather(*ts))
        return woke

    woke, _ = run_virtual(main)
    assert woke == ["a", "b", "c"]


def test_idle_queue_get_lets_time_advance(run_virtual):
    async def main(clock):
        q: asyncio.Queue = asyncio.Queue()

        async def producer():
            await clock.sleep_until_ns(30 * MS)
            q.put_nowait("x")

        clock.task(producer())
        item = await clock.idle(q.get())
        return item, clock.now_ns()

    (item, t), _ = run_virtual(main)
    assert item == "x" and t == 30 * MS


def test_unwrapped_queue_get_raises_deadlock(run_virtual):
    async def main(clock):
        q: asyncio.Queue = asyncio.Queue()

        async def producer():
            await clock.sleep_until_ns(30 * MS)   # never wakes: the consumer holds time
            q.put_nowait("x")

        p = clock.task(producer())
        c = clock.task(q.get())                   # unwrapped: stays "active" forever
        with pytest.raises(DeadlockError):
            await clock.idle(asyncio.gather(p, c))
        await asyncio.sleep(0)
        return p, c

    t0 = time.perf_counter()
    (p, c), _ = run_virtual(main, deadlock_wall_s=0.2)
    assert time.perf_counter() - t0 < 5           # well inside the test timeout
    assert isinstance(p.exception(), DeadlockError)
    assert c.cancelled()


def test_idle_with_empty_heap_raises_deadlock(run_virtual):
    async def main(clock):
        q: asyncio.Queue = asyncio.Queue()
        with pytest.raises(DeadlockError):
            await clock.idle(q.get())             # nobody will ever put
        return True

    ok, _ = run_virtual(main)
    assert ok


def test_ten_minute_virtual_program_is_fast(run_virtual):
    frame = 20 * MS
    ten_min = 600_000 * MS

    async def main(clock):
        async def pacer():
            t = 0
            while t < ten_min:
                t += frame
                await clock.sleep_until_ns(t)

        await clock.idle(asyncio.gather(clock.task(pacer()), clock.task(pacer())))
        return clock.now_ns()

    t0 = time.perf_counter()
    now, _ = run_virtual(main)
    assert now == ten_min
    assert time.perf_counter() - t0 < 1.0


def test_cancel_registered_first_applies_before_frame(run_virtual):
    """Same t_ns: the earlier-registered event (cancel) runs first (BUILD_PLAN §2.1)."""
    async def main(clock):
        log = []

        async def at(t, what):
            await clock.sleep_until_ns(t)
            log.append(what)

        c = clock.task(at(40 * MS, "cancel"))
        f = clock.task(at(40 * MS, "frame"))
        await clock.idle(asyncio.gather(c, f))
        return log

    log, _ = run_virtual(main)
    assert log == ["cancel", "frame"]


def test_cancelled_sleeper_does_not_stall_clock(run_virtual):
    async def main(clock):
        async def long():
            await clock.sleep_until_ns(10_000 * MS)

        t = clock.task(long())
        await clock.sleep_until_ns(5 * MS)
        t.cancel()
        await clock.sleep_until_ns(7 * MS)
        return clock.now_ns(), t.cancelled()

    (now, cancelled), _ = run_virtual(main)
    assert now == 7 * MS and cancelled


def test_sleep_into_past_returns_at_now(run_virtual):
    async def main(clock):
        await clock.sleep_until_ns(10 * MS)
        await clock.sleep_until_ns(5 * MS)
        return clock.now_ns()

    now, _ = run_virtual(main)
    assert now == 10 * MS


def test_two_virtual_clocks_sequential_in_one_loop_no_livelock():
    """Regression (worklog M1-T07..T16): two VirtualClocks in one loop livelocked."""
    async def body(clock, t):
        await clock.sleep_until_ns(t)
        return clock.now_ns()

    async def main():
        a = VirtualClock(deadlock_wall_s=2.0)
        ra = await a.run(body(a, 10 * MS))
        b = VirtualClock(deadlock_wall_s=2.0)
        rb = await b.run(body(b, 20 * MS))
        return ra, rb

    t0 = time.perf_counter()
    assert asyncio.run(asyncio.wait_for(main(), timeout=10)) == (10 * MS, 20 * MS)
    assert time.perf_counter() - t0 < 5


def test_two_virtual_clocks_concurrent_in_one_loop():
    async def body(clock, t):
        for k in range(1, 4):
            await clock.sleep_until_ns(k * t)
        return clock.now_ns()

    async def main():
        a, b = VirtualClock(deadlock_wall_s=2.0), VirtualClock(deadlock_wall_s=2.0)
        return await asyncio.gather(a.run(body(a, 7 * MS)), b.run(body(b, 5 * MS)))

    assert asyncio.run(asyncio.wait_for(main(), timeout=10)) == [21 * MS, 15 * MS]
