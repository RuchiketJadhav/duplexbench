"""M1-T11: spec §1.5 latency taxonomy on hand-built CallRecords."""
from __future__ import annotations

import inspect
from pathlib import Path

import numpy as np
import pytest

from duplexbench.metrics import latency as L
from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, Event, OverlayRow, ToolRow
from duplexbench.metrics.names import Metric
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()


def _playout(total_s: float, active: list[tuple[float, float]]) -> np.ndarray:
    n = int(round(total_s * 50))
    p = np.zeros(n, dtype=PLAYOUT_DTYPE)
    p["t_ns"] = np.arange(n) * 20 * MS
    p["frame_idx"] = np.arange(n)
    p["rms_dbfs"] = -np.inf
    for a, b in active:
        i, j = int(round(a * 50)), int(round(b * 50))
        p["active"][i:j] = True
        p["rms_dbfs"][i:j] = -20.0
    return p


def _rec(events=(), playout=None, overlays=(), tools=(), total_s=30.0, active=()) -> CallRecord:
    ev = [Event(int(round(t * 1e9)), i, k, "x", pl) for i, (t, k, pl) in enumerate(events)]
    return CallRecord(events=ev, playout=playout if playout is not None else _playout(total_s, list(active)),
                      overlays=list(overlays), tool_calls=list(tools), sim_turns=[],
                      wav_path=Path(), scenario=None, twin=None, call_id="c1", system_id="s",
                      condition="C0")


def _turn(on: float, end: float, sid: str = "s1"):
    return [(on, "caller_turn_onset", {"step_id": sid}), (end, "caller_turn_end", {"step_id": sid})]


def _ov(on: float, end: float, oid: str = "o1") -> OverlayRow:
    return OverlayRow(oid, "Y-INT", None, int(on * 1e9), int(on * 1e9), int(end * 1e9))


def _one(obs, metric=None):
    obs = [o for o in obs if metric is None or o.metric == metric]
    assert len(obs) == 1, obs
    return obs[0]


def test_ttfa_recv_420():
    r = _rec(_turn(9.0, 10.0) + [(10.42, "first_audio_byte", {"turn_end_ns": 10_000 * MS})])
    o = _one(L.t_ttfa_recv(r, TH))
    assert o.metric == "t_ttfa_recv" and o.value == pytest.approx(420) and o.valid and o.event_id == "s1"


def test_turn_gap_500():
    r = _rec(_turn(9.0, 10.0), active=[(10.5, 12.0)])
    assert _one(L.t_turn_gap_ear(r, TH)).value == pytest.approx(500)


def test_turn_gap_excludes_utterance_begun_before_caller_onset():
    r = _rec(_turn(9.0, 10.0), active=[(8.5, 9.3), (10.5, 12.0)])
    assert _one(L.t_turn_gap_ear(r, TH)).value == pytest.approx(500)


def test_turn_gap_signed_negative_when_agent_starts_during_turn():
    r = _rec(_turn(9.0, 10.0), active=[(9.6, 12.0)])
    assert _one(L.t_turn_gap_ear(r, TH)).value == pytest.approx(-400)


def test_stop_ear_300_stop_model_80_penalty_220():
    # frames are on a 20 ms grid: overlay end 21.010, next onset 21.360 -> 350 ms
    r = _rec([(20.08, "cancel_or_truncate", {})], overlays=[_ov(20.0, 21.01)],
             active=[(19.0, 20.3), (21.36, 23.0)])
    assert _one(L.t_stop_ear(r, TH)).value == pytest.approx(300)
    sm = _one(L.t_stop_model(r, TH))
    assert sm.value == pytest.approx(80) and "diagnostic" in sm.tags
    assert _one(L.playout_penalty(r, TH)).value == pytest.approx(220)
    assert _one(L.t_respond_ear(r, TH)).value == pytest.approx(350)


def test_not_stopped_within_hold_window():
    r = _rec(overlays=[_ov(20.0, 21.0)], active=[(19.0, 23.0)])
    o = _one(L.t_stop_ear(r, TH))
    assert o.value is None and not o.valid and o.tags == ("not_stopped",)
    assert L.t_respond_ear(r, TH) == []            # undefined without a stop
    assert L.t_stop_model(r, TH) == []             # no cancel event: absent, not invented


def test_stop_needs_a_full_inactivity_run():
    # a 480 ms gap is not a stop; the stop is the start of the later 500 ms run
    r = _rec(overlays=[_ov(20.0, 20.5)], active=[(19.0, 20.3), (20.78, 21.0)])
    assert _one(L.t_stop_ear(r, TH)).value == pytest.approx(1000)


def test_backlog_at_overlay_onset():
    p = _playout(30, [(19.0, 21.0)])
    p["backlog_ms"] = 60.0
    p["backlog_ms"][1000] = 940.0                  # row at 20.000 s
    r = _rec(overlays=[_ov(20.0, 21.0)], playout=p)
    assert _one(L.backlog_ms(r, TH)).value == 940.0


def test_tool_offset_1200():
    tools = [ToolRow("t1", "book", {}, 11_200 * MS, 11_300 * MS, None, None, {})]
    r = _rec(_turn(9.0, 10.0), tools=tools)
    o = _one(L.t_tool_offset(r, TH))
    assert o.value == pytest.approx(1200) and o.event_id == "t1"


def test_tool_offset_from_events_when_no_rows():
    r = _rec(_turn(9.0, 10.0) + [(11.2, "tool_call", {"id": "a"}), (11.3, "tool_result", {"id": "a"})])
    assert _one(L.t_tool_offset(r, TH)).value == pytest.approx(1200)


def test_tool_to_info_needs_reference_text():
    tools = [ToolRow("t1", "book", {}, 11_000 * MS, 12_000 * MS, None, None,
                     {"anchors": ["three pm"]})]
    r = _rec(_turn(9.0, 10.0), tools=tools, active=[(12.2, 13.0), (13.6, 15.0)])
    assert L.t_tool_to_info_ear(r, TH) == []

    def text(on, end):
        return "let me check" if on < 13 * 1e9 else "you are booked at three PM"
    assert _one(L.t_tool_to_info_ear(r, TH, utterance_text=text)).value == pytest.approx(1600)


def test_dead_air_sums_gaps_and_splits_tool_wait():
    # caller 0-1 s; agent 1-2; gap 2.0-4.5 (2.5 s); agent 4.5-5.5; gap 5.5-7.4 (1.9 s, below 2 s);
    # agent 7.4-8.0; tool pending 8.0-12.0 with silence (4 s); agent 12.0-13.0; end 13.0
    tools = [ToolRow("t1", "lookup", {}, 8_000 * MS, 12_000 * MS, None, None, {})]
    r = _rec(_turn(0.0, 0.98), tools=tools, total_s=13.0,
             active=[(1.0, 2.0), (4.5, 5.5), (7.4, 8.0), (12.0, 13.0)])
    obs = L.t_dead_air(r, TH)
    main = next(o for o in obs if o.metric == "t_dead_air" and o.event_id is None)
    mx = next(o for o in obs if o.event_id == "max_gap")
    tool = next(o for o in obs if o.metric == "dead_air_in_tool_s")
    assert main.value == pytest.approx(2.5) and mx.value == pytest.approx(2.5)
    assert tool.value == pytest.approx(4.0) and "diagnostic" in mx.tags


def test_caller_think():
    r = _rec(_turn(0.0, 1.0) + _turn(4.0, 5.0, "s2"), active=[(1.5, 3.0)])
    o = _one(L.t_caller_think(r, TH))
    assert o.value == pytest.approx(1000) and "diagnostic" in o.tags


def test_observe_covers_every_name_and_docstrings_cite_1_5():
    tools = [ToolRow("t1", "x", {}, 11_200 * MS, 11_300 * MS, None, None, {})]
    r = _rec(_turn(9.0, 10.0) + [(10.42, "first_audio_byte", {"turn_end_ns": 10_000 * MS}),
                                 (20.08, "cancel_or_truncate", {}), (14.0, "caller_turn_onset",
                                                                     {"step_id": "s2"}),
                                 (14.5, "caller_turn_end", {"step_id": "s2"})],
             overlays=[_ov(20.0, 21.0)], tools=tools, active=[(10.5, 12.0), (19.0, 20.3), (21.35, 23.0)])
    names = {o.metric for o in L.observe(r, TH)}
    assert {"t_ttfa_recv", "t_turn_gap_ear", "t_stop_ear", "t_stop_model", "t_respond_ear",
            "t_tool_offset", "t_dead_air", "t_caller_think", "playout_penalty", "backlog_ms",
            "dead_air_in_tool_s"} <= names
    assert names <= {m.value for m in Metric}
    for fn in (L.t_ttfa_recv, L.t_turn_gap_ear, L.t_stop_ear, L.t_stop_model, L.t_respond_ear,
               L.t_tool_offset, L.t_tool_to_info_ear, L.t_dead_air, L.t_caller_think,
               L.playout_penalty, L.backlog_ms):
        assert "§1.5" in inspect.getdoc(fn) or "§1.3" in inspect.getdoc(fn), fn.__name__
