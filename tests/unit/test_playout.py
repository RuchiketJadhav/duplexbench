"""M1-T09: reference playout model (spec §1.3) under the VirtualClock."""
from __future__ import annotations

import asyncio
import json

import numpy as np
import soundfile as sf

from duplexbench.calibrate import MemStore
from duplexbench.clock import VirtualClock
from duplexbench.playout import Playout
from duplexbench.recorder import Recorder
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()


def _frame(v: int = 3000) -> np.ndarray:
    return np.full(160, v, np.int16)


def _run(tmp_path, setup, until_ms: int):
    """setup(clock, po) -> list of coroutines spawned as accounted tasks BEFORE po.run()."""
    clock = VirtualClock()
    store = MemStore()
    rec = Recorder(tmp_path / "ear.wav", 8000)
    po = Playout(clock, store, rec, TH, run_id="r", call_id="c")

    async def main():
        side = [clock.task(c) for c in setup(clock, po)]
        t = clock.task(po.run())
        await clock.sleep_until_ns(until_ms * MS)
        po.stop()
        t.cancel()
        await clock.idle(asyncio.gather(t, *side, return_exceptions=True))
        rec.close()

    asyncio.run(clock.run(main()))
    data, _ = sf.read(str(tmp_path / "ear.wav"), dtype="int16", always_2d=True)
    return po, store, data[:, 1]


def _at(clock, t_ms, fn):
    async def go():
        await clock.sleep_until_ns(t_ms * MS)
        fn()
    return go()


def _played(store) -> list[dict]:
    return store.rows["playout"]


def test_three_frames_at_zero_play_at_zero(tmp_path):
    def setup(clock, po):
        for _ in range(3):
            po.enqueue(_frame(), 0)
        return []
    po, store, ear = _run(tmp_path, setup, 100)
    rows = _played(store)
    assert rows[0]["t_ns"] == 0 and rows[0]["queue_depth"] == 3 and rows[0]["active"] == 1
    assert rows[0]["backlog_ms"] == 60.0                      # 3 frames x 20 ms = jitter buffer
    assert [r["active"] for r in rows[:4]] == [1, 1, 1, 0]


def test_one_frame_waits_for_the_third(tmp_path):
    def setup(clock, po):
        po.enqueue(_frame(), 0)
        return [_at(clock, 20, lambda: po.enqueue(_frame(), 20 * MS)),
                _at(clock, 40, lambda: po.enqueue(_frame(), 40 * MS))]
    po, store, ear = _run(tmp_path, setup, 200)
    rows = _played(store)
    assert [r["active"] for r in rows[:6]] == [0, 0, 1, 1, 1, 0]
    assert rows[2]["t_ns"] == 40 * MS and rows[2]["queue_depth"] == 3


def test_lone_frame_plays_after_the_buffer_time(tmp_path):
    def setup(clock, po):
        po.enqueue(_frame(), 0)
        return []
    po, store, ear = _run(tmp_path, setup, 200)
    act = [r["active"] for r in _played(store)]
    assert act.index(1) == TH.jitter_buffer_ms // TH.frame_ms     # tick at 60 ms
    assert sum(act) == 1


def test_fifty_frames_backlog_and_last_play(tmp_path):
    def setup(clock, po):
        for _ in range(50):
            po.enqueue(_frame(), 0)
        return []
    po, store, ear = _run(tmp_path, setup, 1_200)
    # Plan said 940 (= 47 x 20); backlog_ms = queue_depth x frame_ms with the depth logged
    # at the tick including the frame being played: 50 x 20 = 1000 ms (see report).
    assert po.backlog_ms_at(0) == 1_000.0
    assert po.backlog_ms_at(19 * MS) == 1_000.0
    assert po.backlog_ms_at(20 * MS) == 980.0
    rows = _played(store)
    active_t = [r["t_ns"] for r in rows if r["active"]]
    assert active_t[-1] == 980 * MS and len(active_t) == 50
    assert all("queue_depth" in r and "backlog_ms" in r for r in rows)   # logged every frame
    assert po.backlog_ms_at(-1) == 0.0


def test_flush_at_200_with_40_queued(tmp_path):
    def setup(clock, po):
        for _ in range(50):
            po.enqueue(_frame(), 0)
        return [_at(clock, 200, lambda: po.flush(200 * MS, "cancel_or_truncate"))]
    po, store, ear = _run(tmp_path, setup, 400)
    rows = _played(store)
    r200 = next(r for r in rows if r["t_ns"] == 200 * MS)
    assert r200["flushed"] == 1 and r200["queue_depth"] == 0 and r200["active"] == 0
    assert sum(r["flushed"] for r in rows) == 1
    ev = [e for e in store.rows["events"] if e["kind"] == "playout_flush"]
    assert len(ev) == 1 and json.loads(ev[0]["payload_json"])["dropped_frames"] == 40
    assert (ear[:1600] == 3000).all() and (ear[1600:] == 0).all()     # silence from 200 ms


def test_two_flushes_same_t_one_event(tmp_path):
    def setup(clock, po):
        for _ in range(20):
            po.enqueue(_frame(), 0)

        def twice():
            po.flush(100 * MS, "a")
            po.flush(100 * MS, "b")
        return [_at(clock, 100, twice)]
    po, store, ear = _run(tmp_path, setup, 300)
    assert len([e for e in store.rows["events"] if e["kind"] == "playout_flush"]) == 1


def test_cancel_registered_first_applies_before_the_frame(tmp_path):
    def setup(clock, po):
        for _ in range(50):
            po.enqueue(_frame(), 0)
        # registered at t=0, before playout's own wait for the 200 ms tick (registered at 180)
        return [_at(clock, 200, lambda: po.flush(200 * MS, "cancel_or_truncate"))]
    po, store, ear = _run(tmp_path, setup, 300)
    r200 = next(r for r in _played(store) if r["t_ns"] == 200 * MS)
    assert r200["active"] == 0 and r200["flushed"] == 1          # the 200 ms frame is not played


def test_underrun_plays_silence_and_logs_once(tmp_path):
    def setup(clock, po):
        for _ in range(3):
            po.enqueue(_frame(), 0)
        return []
    po, store, ear = _run(tmp_path, setup, 200)
    ev = [e for e in store.rows["events"] if e["kind"] == "playout_underrun"]
    assert len(ev) == 1 and ev[0]["t_ns"] == 60 * MS
    assert len(_played(store)) == 10                               # one row per tick, 0..180 ms


def test_subscribers_get_onset_and_stop(tmp_path):
    got = []

    def setup(clock, po):
        po.subscribe(lambda kind, idx, t: got.append((kind, idx, t)))
        for _ in range(10):
            po.enqueue(_frame(), 0)
        return []
    _run(tmp_path, setup, 1_000)
    assert got == [("onset", 0, 0), ("stop", 10, 200 * MS)]
