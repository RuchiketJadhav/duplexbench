"""M1-T15: CallRunner with the mock agent under the VirtualClock (BUILD_PLAN §2.7)."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import soundfile as sf

from duplexbench.adapters.mock_agent import MockAgent, MockProgram
from duplexbench.budget import Budget
from duplexbench.calibrate import MemStore
from duplexbench.channel.chain import ChannelChain, load_condition
from duplexbench.clock import VirtualClock
from duplexbench.context import CallContext
from duplexbench.playout import Playout
from duplexbench.recorder import Recorder
from duplexbench.runner import CallResult, run_call
from duplexbench.stimuli.caller_source import CallerSource, ToneCaller
from duplexbench.storage import Store
from duplexbench.thresholds import Thresholds

MS = 1_000_000


class RaisingAgent(MockAgent):
    def __init__(self, clock, n: int, **kw):
        super().__init__(clock, MockProgram(**kw))
        self.n = n

    async def send_frame(self, mulaw_bytes, t_ns):
        if self.received_frames + 1 == self.n:
            raise ConnectionResetError("socket died on frame %d" % self.n)
        await super().send_frame(mulaw_bytes, t_ns)


async def _call(clock, store, tmp_path, call_id, *, agent=None, th=None, turns=(("s1", 0, 1_000),),
                total_ms=10_000, cond="C0", budget=None, projected=0.0) -> CallResult:
    th = th or Thresholds()
    c = load_condition(cond, th)
    ctx = CallContext("run1", call_id, None, 0, c, "mock", False, clock, store, "tok")
    caller = ToneCaller(clock, list(turns), rate=c.rate, total_ms=total_ms)
    assert isinstance(caller, CallerSource)
    rec = Recorder(tmp_path / f"{call_id}.wav", c.rate, origin_ns=clock.now_ns())
    po = Playout(clock, store, rec, th, run_id="run1", call_id=call_id, rate=c.rate)
    return await run_call(ctx, agent or MockAgent(clock, MockProgram(), th), caller, None, po,
                          ChannelChain(c, 11, 12), None, budget, th=th, projected_usd=projected)


def _run(main):
    clock = VirtualClock()
    return asyncio.run(clock.run(main(clock))), clock


def _kinds(store, call_id):
    return [e["kind"] for e in store.rows["events"] if e["call_id"] == call_id]


def test_ten_second_scripted_call(tmp_path):
    store = MemStore()
    res, _ = _run(lambda clock: _call(clock, store, tmp_path, "c1"))
    assert res.outcome == "completed"
    data, rate = sf.read(str(tmp_path / "c1.wav"), dtype="int16", always_2d=True)
    assert rate == 8000 and abs(len(data) - 80_000) <= 160
    kinds = _kinds(store, "c1")
    for k in ("session_open", "first_audio_byte", "closed", "caller_turn_onset", "caller_turn_end",
              "agent_onset", "tool_call", "tool_result", "call_start", "call_end"):
        assert k in kinds, k
    assert abs(len(store.rows["playout"]) - 500) <= 2
    calls = store.rows["calls"]
    assert len(calls) == 1 and calls[0]["outcome"] == "completed"
    assert json.loads(calls[0]["flags_json"])["wav_sha256"] == res.flags["wav_sha256"]
    seqs = [e["seq"] for e in store.rows["events"]]
    assert len(set(seqs)) == len(seqs)                       # one sequence for runner + playout


def test_raise_on_100th_frame_is_transport_error_and_next_call_runs(tmp_path):
    store = Store(tmp_path / "events.sqlite")
    try:
        async def main(clock):
            r1 = await _call(clock, store, tmp_path, "bad", agent=RaisingAgent(clock, 100))
            r2 = await _call(clock, store, tmp_path, "good")
            return r1, r2
        (r1, r2), _ = _run(main)
    finally:
        store.close()
    assert r1.outcome == "transport_error" and r1.flags["error"]["type"] == "ConnectionResetError"
    assert r2.outcome == "completed"
    data, _ = sf.read(str(tmp_path / "bad.wav"), dtype="int16", always_2d=True)
    assert abs(len(data) - 99 * 160) <= 2 * 160              # closed after frame 99, readable
    con = sqlite3.connect(str(tmp_path / "events.sqlite"))
    outs = dict(con.execute("SELECT call_id, outcome FROM calls").fetchall())
    assert outs == {"bad": "transport_error", "good": "completed"}
    kinds = [k for (k,) in con.execute("SELECT kind FROM events WHERE call_id='bad'")]
    assert "error" in kinds and "call_end" in kinds
    assert con.execute("SELECT COUNT(*) FROM playout WHERE call_id='bad'").fetchone()[0] > 0


def test_adapter_error_event_is_transport_error(tmp_path):
    class Broken(MockAgent):
        async def _events(self):
            yield await anext(super()._events())       # session_open
            raise RuntimeError("provider closed the socket")

    store = MemStore()
    res, _ = _run(lambda clock: _call(clock, store, tmp_path, "e", agent=Broken(clock)))
    assert res.outcome == "transport_error" and res.flags["error"]["type"] == "RuntimeError"


def test_call_timeout(tmp_path):
    th = Thresholds(call_timeout_ms=3_000)
    store = MemStore()
    res, clock = _run(lambda clock: _call(clock, store, tmp_path, "t", th=th, total_ms=None))
    assert res.outcome == "timeout"
    assert store.rows["calls"][0]["ended_ns"] == 3_000 * MS


def test_stall(tmp_path):
    th = Thresholds(stall_ms=3_000, eend_silence_ms=60_000)
    agent_prog = MockProgram(reply_len_ms=200, tool_at_ms=None)
    store = MemStore()

    async def main(clock):
        return await _call(clock, store, tmp_path, "s", th=th, total_ms=None,
                           turns=[("s1", 0, 1_000), ("s2", 30_000, 1_000)],
                           agent=MockAgent(clock, agent_prog, th))
    res, _ = _run(main)
    assert res.outcome == "stalled"
    end = store.rows["calls"][0]["ended_ns"]
    # last activity = end of the ear reply (980 + 400 + 60 + 200 ms), then 3 s of silence
    assert 4_500 * MS <= end <= 4_800 * MS


def test_eend_silence_completes(tmp_path):
    th = Thresholds()
    store = MemStore()
    prog = MockProgram(reply_len_ms=1_000, tool_at_ms=None)

    async def main(clock):
        return await _call(clock, store, tmp_path, "x", th=th, total_ms=None,
                           agent=MockAgent(clock, prog, th))
    res, _ = _run(main)
    assert res.outcome == "completed"
    end = store.rows["calls"][0]["ended_ns"]
    last_ear = 980 + 400 + 60 + 1_000
    assert (last_ear + th.eend_silence_ms - 40) * MS <= end <= (last_ear + th.eend_silence_ms + 40) * MS


def test_budget_refusal_never_starts(tmp_path):
    store = MemStore()
    holder = {}

    async def main(clock):
        holder["agent"] = MockAgent(clock)
        return await _call(clock, store, tmp_path, "b", agent=holder["agent"],
                           budget=Budget(max_usd=0.5), projected=1.0)
    res, _ = _run(main)
    assert res.outcome == "budget_refused" and holder["agent"].call is None
    assert store.rows["calls"][0]["outcome"] == "budget_refused"
    assert not store.rows["playout"]


def test_budget_reserved_and_settled(tmp_path):
    store = MemStore()
    budget = Budget(max_usd=1.0)
    res, _ = _run(lambda clock: _call(clock, store, tmp_path, "ok", budget=budget, projected=0.4,
                                      total_ms=2_000))
    assert res.outcome == "completed" and budget.reserved == 0 and budget.settled == 0.0


def test_c1_turn_end_shifted_by_forward_delay(tmp_path):
    store = MemStore()
    res, _ = _run(lambda clock: _call(clock, store, tmp_path, "c", cond="C1", total_ms=3_000))
    ends = [e["t_ns"] for e in store.rows["events"] if e["kind"] == "caller_turn_end"]
    assert ends == [(980 + 2 * 20) * MS]                    # C1 holds back 2 frames
    assert res.flags["forward_delay_frames"] == 2


def test_overlay_rows_from_injected_clip(tmp_path):
    from duplexbench.stimuli.caller_source import tone
    store = MemStore()

    async def main(clock):
        th = Thresholds()
        c = load_condition("C0", th)
        ctx = CallContext("run1", "o", None, 0, c, "mock", False, clock, store, "tok")
        caller = ToneCaller(clock, [("s1", 0, 1_000)], total_ms=4_000)
        caller.inject(tone(8000, 2_400, 660, -20), 0.0, 2_000 * MS)
        po = Playout(clock, store, None, th, run_id="run1", call_id="o")
        return await run_call(ctx, MockAgent(clock), caller, None, po, ChannelChain(c, 1, 2), None,
                              None)
    res, _ = _run(main)
    ov = store.rows["overlays"]
    assert len(ov) == 1 and ov[0]["onset_ns"] == 2_000 * MS and ov[0]["end_ns"] == 2_280 * MS
    assert [e["kind"] for e in store.rows["events"] if e["kind"] == "cancel_or_truncate"]
