"""M2-T05: overlay engine under the VirtualClock with MockAgent + ScriptedCaller, fixture track_a_002."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from duplexbench.adapters.mock_agent import MockAgent, MockProgram
from duplexbench.calibrate import MemStore
from duplexbench.clock import VirtualClock
from duplexbench.overlays.engine import OverlayEngine
from duplexbench.overlays.scripted_run import run_scripted_call
from duplexbench.scenarios.schema import Scenario
from duplexbench.stimuli.scripted_caller import ScriptedCaller
from duplexbench.thresholds import Thresholds

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
import make_golden_m2 as g  # noqa: E402

MS = 1_000_000
TH = Thresholds()


class SlowCaller(ScriptedCaller):
    """Delivers every injection 350 ms late (a slow caller pipeline)."""

    def inject(self, clip_pcm, level_db, t_ns=None, *, overlay_id=None):
        return super().inject(clip_pcm, level_db, None if t_ns is None else t_ns + 350 * MS,
                              overlay_id=overlay_id)


def _run(tmp_path, behaviour="stops", slow=False):
    store = MemStore()
    sc, clips = g.scenario(), g.clips()

    async def main(clock):
        agent = MockAgent(clock, MockProgram(behaviour=behaviour, tool_at_ms=None), TH)
        caller = SlowCaller(sc, clips, clock, TH) if slow else None
        return await run_scripted_call(clock, store, sc, clips, agent, TH, tmp_path,
                                       run_id="r", condition="C0", caller=caller)

    clock = VirtualClock()
    res = asyncio.run(clock.run(main(clock)))
    rows = {r["overlay_id"]: r for r in store.rows["overlays"]}
    evs = store.rows["events"]
    return res, rows, evs


def _ev(evs, kind, oid):
    return [json.loads(e["payload_json"]) | {"t_ns": e["t_ns"]} for e in evs
            if e["kind"] == kind and json.loads(e["payload_json"]).get("overlay_id") == oid]


@pytest.fixture(scope="module")
def stops_run(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("stops"))


def test_injected_at_onset_plus_offset_within_one_frame(stops_run):
    res, rows, evs = stops_run
    assert res.outcome == "completed"
    inj = _ev(evs, "overlay_injected", "o1")[0]
    assert inj["scheduled_offset_ms"] == 500.0
    assert 500.0 <= inj["actual_offset_ms"] <= 520.0          # +- 1 frame (C0: no forward delay)
    r = rows["o1"]
    assert r["actual_ns"] - r["scheduled_ns"] <= 20 * MS and json.loads(r["tags_json"]) == []
    on = _ev(evs, "overlay_onset", "o1")[0]                    # runner's send-time anchor
    assert on["t_ns"] == r["onset_ns"]


def test_in_pause_fires_at_pause_start(stops_run):
    _, rows, _ = stops_run
    r = rows["o3"]                                             # P-PAUSE, offset 0
    # s1 clip 10,400 samples = 65 frames = 1.3 s; pause 900 ms; caller starts at t = 0
    assert r["scheduled_ns"] == 1300 * MS and r["onset_ns"] == 1300 * MS
    assert r["end_ns"] == 2200 * MS
    assert json.loads(r["tags_json"]) == ["resume_ns:2200000000"]


def test_wait_class_ycmd_resumption(stops_run):
    _, rows, evs = stops_run
    r = rows["o4"]
    tags = json.loads(r["tags_json"])
    resume = int(tags[0].split(":")[1])
    lead = 40 * MS                                             # a002_s2.wav: 2 silent lead frames
    assert abs(resume - (r["end_ns"] + 4000 * MS + lead)) <= 20 * MS
    on = _ev(evs, "overlay_onset", "o4/resumption")
    assert on and on[0]["t_ns"] == resume


def test_eend_row_is_turn_end(stops_run):
    _, rows, evs = stops_run
    te = [e["t_ns"] for e in evs if e["kind"] == "caller_turn_end"
          and json.loads(e["payload_json"])["step_id"] == "s2"]
    assert rows["o5"]["scheduled_ns"] == te[0] and rows["o5"]["onset_ns"] is None


def test_short_reply_offset_2s_not_injected(tmp_path):
    _, rows, evs = _run(tmp_path, behaviour="short_reply")
    assert json.loads(rows["o2"]["tags_json"]) == ["not_injected"]      # H-BC at 2.0 s
    assert rows["o2"]["actual_ns"] is None and rows["o2"]["onset_ns"] is None
    assert not _ev(evs, "overlay_onset", "o2")                          # no audio sent
    assert _ev(evs, "overlay_not_injected", "o2")[0]["reason"].startswith("agent utterance ended")
    assert json.loads(rows["o1"]["tags_json"]) == []                    # 0.5 s < 1.2 s: injected


def test_slow_injection_is_contested(tmp_path):
    _, rows, _ = _run(tmp_path, slow=True)
    r = rows["o1"]
    assert r["actual_ns"] - r["scheduled_ns"] >= 350 * MS
    assert "contested" in json.loads(r["tags_json"])


def _scenario(overlays, steps=None):
    return Scenario.model_validate({
        "id": "t", "version": "1", "split": "public-dev", "domain": "x", "track": "A",
        "source": "scripted", "caller_voice": "v",
        "steps": steps or [{"id": "s1", "text": "a", "pause_ms": 1600}],
        "overlays": overlays})


def test_contested_rules_one_word_yint_and_long_pause():
    sc = _scenario([
        {"id": "a", "cls": "Y-INT", "anchor_step": "s1", "placement": "during_agent",
         "offset_ms": 500, "text": "Tuesday"},
        {"id": "b", "cls": "Y-INT", "anchor_step": "s1", "placement": "during_agent",
         "offset_ms": 500, "text": "no wait it is Tuesday"},
        {"id": "p", "cls": "P-PAUSE", "anchor_step": "s1", "placement": "in_pause",
         "offset_ms": 0, "text": ""}])
    eng = OverlayEngine(VirtualClock(), sc, object(), object(), None, TH, clips={},
                        ycmd_tokens=("no", "stop", "wait"))
    rows = {r["overlay_id"]: json.loads(r["tags_json"]) for r in eng.finish()}
    assert rows["a"] == ["not_injected", "contested"]        # one word, not a Y-CMD token
    assert rows["b"] == ["not_injected"]
    assert "contested" in rows["p"]                           # pause 1.6 s > 1.5 s
    assert eng.finish() is eng.rows                           # idempotent
