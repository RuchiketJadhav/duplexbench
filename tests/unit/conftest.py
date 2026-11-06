"""Shared unit-test fixtures: a temp Store and a VirtualClock program runner."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest

from duplexbench.clock import VirtualClock
from duplexbench.storage import Store

T = TypeVar("T")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "events.sqlite")
    yield s
    s.close()


@pytest.fixture
def run_virtual() -> Callable[..., object]:
    """run_virtual(main, **clock_kwargs) -> (result, clock).

    `main(clock)` returns a coroutine; it runs as an accounted task on a fresh
    VirtualClock in a fresh event loop. Use it from sync tests only.
    """
    def _run(main: Callable[[VirtualClock], Awaitable[T]], **kw) -> tuple[T, VirtualClock]:
        clock = VirtualClock(**kw)

        async def _top():
            return await clock.run(main(clock))

        return asyncio.run(_top()), clock

    return _run
