"""M2-T07: control twins, caller-step alignment, per-metric validity (spec §3.4)."""
from pathlib import Path

import numpy as np
import pytest

from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, Event, OverlayRow
from duplexbench.scenarios.control_twin import (AgentTurn, agent_turns, agent_turns_from, align,
                                                make_twin, pair_by_scenario, split_pass_rates,
                                                validity)
from duplexbench.scenarios.loader import load_pack
from duplexbench.thresholds import Thresholds

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"
MS = 1_000_000
TH = Thresholds()


def _row(offset_ms=500, onset_ms=1000):
    return OverlayRow("o1", "Y-INT", scheduled_ns=(onset_ms + offset_ms) * MS, actual_ns=None,
                      onset_ns=onset_ms * MS, end_ns=None)


def test_make_twin_drops_injected_overlays_keeps_steps():
    s = next(x for x in load_pack(FIX).scenarios if x.id == "sched_001")
    t = make_twin(s)
    assert [x.cls for x in t.overlays] == ["P-PAUSE", "E-END"]
    assert t.steps == s.steps and t.id == s.id and t.control_twin is False
    assert len(s.overlays) == 4          # original untouched


def test_validity_budget_windows_on_a_1_2s_control_turn():
    ctrl = AgentTurn("s1", onset_ns=0, end_ns=1200 * MS)
    row = _row(offset_ms=500)
    assert validity(row, ctrl, "yield_rate", 500, TH) is None
    assert validity(row, ctrl, "yield_rate", 1000, TH) == "ambiguous_end"
    assert validity(row, ctrl, "yield_rate", 2000, TH) == "ambiguous_end"
    # T_stop_ear and Hold use the 2.0 s window regardless of budget
    assert validity(row, ctrl, "t_stop_ear", None, TH) == "ambiguous_end"
    assert validity(row, ctrl, "hold_speaking_rate", None, TH) == "ambiguous_end"
    long = AgentTurn("s1", 0, 3000 * MS)
    assert validity(row, long, "hold_speaking_rate", None, TH) is None


def test_validity_unmatched_offset_override_and_errors():
    assert validity(_row(), None, "yield_rate", 1000, TH) == "unmatched_control"
    ctrl = AgentTurn("s1", 0, 1200 * MS)
    no_off = OverlayRow("o1", "Y-INT", None, None, None, None)
    assert validity(no_off, ctrl, "yield_rate", 500, TH, offset_ms=500) is None
    with pytest.raises(ValueError):
        validity(no_off, ctrl, "yield_rate", 500, TH)
    with pytest.raises(ValueError):
        validity(_row(), ctrl, "yield_rate", None, TH)
    with pytest.raises(ValueError):
        validity(_row(), ctrl, "pass_at_1", None, TH)
    # still speaking when the recording ended -> long enough
    assert validity(_row(), AgentTurn("s1", 0, None), "t_stop_ear", None, TH) is None


def test_align_by_caller_step_marks_divergence_unmatched():
    o = agent_turns_from([("s1", 0), ("s2", 10_000 * MS), ("s3", 20_000 * MS)],
                         [(500 * MS, 3000 * MS), (10_400 * MS, 12_000 * MS), (20_300 * MS, None)])
    c = agent_turns_from([("s1", 0), ("s3", 20_000 * MS)],
                         [(600 * MS, 4000 * MS), (20_500 * MS, 22_000 * MS)])
    a = align(o, c)
    assert list(a) == ["s1", "s2", "s3"]
    assert a["s2"] is None
    assert a["s1"][0].onset_ns == 500 * MS and a["s1"][1].end_ns == 4000 * MS
    assert a["s3"][1].onset_ns == 20_500 * MS
    assert validity(_row(), a["s2"], "yield_rate", 1000, TH) == "unmatched_control"


def test_align_order_change_and_repeats():
    o = [("s1", None), ("s2", None), ("s3", None)]
    c = [("s1", None), ("s3", None), ("s2", None)]
    a = align(o, c)
    assert a["s1"] is not None and sum(v is None for v in a.values()) == 1
    assert align([("s1", None), ("s1", None)], [("s1", None)]) == {"s1": None}


def test_agent_turns_from_events_and_negative_gap():
    ev = [Event(0, 0, "caller_turn_end", "caller", {"step_id": "s1"}),
          Event(400 * MS, 1, "agent_onset", "playout"), Event(2000 * MS, 2, "agent_stop", "playout"),
          Event(5000 * MS, 3, "agent_onset", "playout"),        # starts before s2 ends: belongs to s1
          Event(5100 * MS, 4, "caller_turn_end", "caller", {"step_id": "s2"}),
          Event(6000 * MS, 5, "agent_stop", "playout")]
    rec = CallRecord(events=ev, playout=np.zeros(0, PLAYOUT_DTYPE), overlays=[], tool_calls=[],
                     sim_turns=[], wav_path=Path("x.wav"), scenario=None, twin=None)
    turns = agent_turns(rec)
    assert turns[0] == ("s1", AgentTurn("s1", 400 * MS, 2000 * MS))
    assert turns[1] == ("s2", None)


def test_track_c_pairs_at_scenario_level_without_alignment():
    calls = []
    for sid in ("c1", "c2", "c3", "c4"):
        for ctl in (0, 1):
            calls.append(dict(call_id=f"{sid}-{'c' if ctl else 'o'}", scenario_id=sid, condition="C0",
                              system_id="m", trial=0, is_control=ctl))
    calls.append(dict(call_id="c5-o", scenario_id="c5", condition="C0", system_id="m", trial=0,
                      is_control=0))                     # no twin: excluded from the split
    passed = {"c1-o": True, "c1-c": True, "c2-o": False, "c2-c": True, "c3-o": False,
              "c3-c": False, "c4-o": True, "c4-c": True, "c5-o": True}
    pairs = pair_by_scenario(calls)
    assert pairs[("c5", "C0", "m", 0)][1] is None
    assert split_pass_rates(pairs, passed) == {"n_pairs": 4, "interrupted": 0.5, "clean": 0.75}
    with pytest.raises(ValueError):
        pair_by_scenario(calls + [calls[0]])
