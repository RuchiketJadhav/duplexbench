"""BUILD_PLAN §2.3: base-class first_audio_byte emission and never-raise wrapping."""
from __future__ import annotations

import asyncio

import numpy as np

from duplexbench.adapters.base import Adapter, AdapterCaps, AdapterEvent
from duplexbench.context import CallContext

MS = 1_000_000


class FakeClock:
    def __init__(self):
        self.t = 0

    def now_ns(self):
        return self.t


def _ctx(clock) -> CallContext:
    return CallContext(run_id="r", call_id="c", scenario=None, trial=0, condition="C0",
                       system_id="fake", is_control=False, clock=clock, store=None,
                       tool_token="tok")


def _audio(t):
    return AdapterEvent("audio", t, "response.audio.delta",
                        {"pcm": np.zeros(160, np.int16), "rate": 8000})


class ScriptAdapter(Adapter):
    """Yields a script; before each event, runs an optional hook such as a turn end."""
    name = "script"
    caps = AdapterCaps(cancel_event=True, native_mulaw=True, output_transcript=False,
                       usage_fields=False)
    config: dict = {}

    def __init__(self, script, raise_after: int | None = None):
        self.script = script
        self.raise_after = raise_after

    async def open(self, call): ...
    async def send_frame(self, mulaw, t_ns): ...
    async def tool_result(self, tool_call_id, result): ...
    async def close(self): ...

    async def events(self):
        for i, (hook, ev) in enumerate(self.script):
            if self.raise_after is not None and i == self.raise_after:
                raise ConnectionResetError("socket died")
            if hook:
                hook()
            yield ev


def _collect(adapter, ctx):
    async def main():
        return [e async for e in adapter.safe_events(ctx)]
    return asyncio.run(main())


def test_first_audio_byte_after_each_turn_end():
    clock = FakeClock()
    ctx = _ctx(clock)
    script = [
        (None, AdapterEvent("session_open", 0, "session.created", {})),
        (None, _audio(1 * MS)),                                   # before any turn end
        (lambda: ctx.turn_ended(10 * MS), _audio(14 * MS)),       # first after turn 1
        (None, _audio(15 * MS)),
        (lambda: ctx.turn_ended(30 * MS), _audio(42 * MS)),       # first after turn 2
        (None, _audio(43 * MS)),
        (None, AdapterEvent("closed", 50 * MS, "session.closed", {})),
    ]
    evs = _collect(ScriptAdapter(script), ctx)
    kinds = [e.kind for e in evs]
    assert kinds == ["session_open", "audio", "first_audio_byte", "audio", "audio",
                     "first_audio_byte", "audio", "audio", "closed"]
    fabs = [e for e in evs if e.kind == "first_audio_byte"]
    assert [e.t_ns for e in fabs] == [14 * MS, 42 * MS]
    assert [e.payload["turn_end_ns"] for e in fabs] == [10 * MS, 30 * MS]
    assert all(e.provider_event == "" for e in fabs)


def test_audio_stamped_before_turn_end_does_not_count():
    ctx = _ctx(FakeClock())
    script = [
        (lambda: ctx.turn_ended(10 * MS), _audio(9 * MS)),   # observed before the turn end
        (None, _audio(12 * MS)),
    ]
    evs = _collect(ScriptAdapter(script), ctx)
    fab = [e for e in evs if e.kind == "first_audio_byte"]
    assert [e.t_ns for e in fab] == [12 * MS]


def test_adapter_emitted_first_audio_byte_is_dropped():
    ctx = _ctx(FakeClock())
    script = [(None, AdapterEvent("first_audio_byte", 1, "x", {})), (None, _audio(2))]
    evs = _collect(ScriptAdapter(script), ctx)
    assert [e.kind for e in evs] == ["audio", "closed"]


def test_raise_inside_events_yields_error_then_closed():
    clock = FakeClock()
    clock.t = 77
    ctx = _ctx(clock)
    script = [(None, _audio(1)), (None, _audio(2)), (None, _audio(3))]
    evs = _collect(ScriptAdapter(script, raise_after=2), ctx)
    assert [e.kind for e in evs] == ["audio", "audio", "error", "closed"]
    err = evs[2]
    assert err.payload["type"] == "ConnectionResetError" and err.t_ns == 77
    assert evs[-1].provider_event == ""


def test_stream_without_closed_gets_exactly_one():
    ctx = _ctx(FakeClock())
    evs = _collect(ScriptAdapter([(None, _audio(1))]), ctx)
    assert [e.kind for e in evs] == ["audio", "closed"]
    script = [(None, AdapterEvent("closed", 5, "bye", {})), (None, _audio(6))]
    evs = _collect(ScriptAdapter(script), ctx)
    assert [e.kind for e in evs] == ["closed"]
