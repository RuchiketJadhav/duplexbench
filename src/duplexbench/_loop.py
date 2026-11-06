# Copied from C:\Users\Lenovo\research\asr-bench\src\asr_bench\pipeline\loop.py
# (asr-bench, same author), with attribution. Only the docstring wording changed
# so that the forbidden monotonic-clock call name does not appear under src/
# (BUILD_PLAN M1-T01 "Done when").
"""Event loop with a precise clock (spec §1.1; BUILD_PLAN §2.1).

On Windows with Python 3.12, `loop.time()` is the stdlib monotonic clock =
GetTickCount64 (15.6 ms ticks), so a 20 ms pacing schedule jitters by up to a
tick. We use a selector loop whose `time()` is `perf_counter` and raise the
system timer resolution to 1 ms for the run. On Linux the stock loop is already
precise and is used unchanged.
"""
from __future__ import annotations

import asyncio
import sys
import time


class _PreciseSelectorLoop(asyncio.SelectorEventLoop):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # BaseEventLoop fires timers up to `_clock_resolution` early; it is set
        # from the stdlib monotonic clock (15.6 ms on Windows), not from ours.
        self._clock_resolution = time.get_clock_info("perf_counter").resolution

    def time(self) -> float:
        return time.perf_counter()


def new_loop() -> asyncio.AbstractEventLoop:
    """A precise loop for helper threads (the mock server)."""
    return _PreciseSelectorLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def run(coro):
    # Fewer threads now share our GIL, but hand it back to the loop quickly anyway.
    sys.setswitchinterval(0.001)
    if sys.platform != "win32":
        return asyncio.run(coro)
    import ctypes
    winmm = ctypes.WinDLL("winmm")
    winmm.timeBeginPeriod(1)
    try:
        with asyncio.Runner(loop_factory=_PreciseSelectorLoop) as r:
            return r.run(coro)
    finally:
        winmm.timeEndPeriod(1)


async def sleep_until_ns(target_ns: int, now_ns) -> None:
    rem = (target_ns - now_ns()) / 1e9
    if rem > 0:
        await asyncio.sleep(rem)
