"""M2-T08: floor-control metrics on synthetic CallRecords (hand-computed expected values; spec §4.1, §3.4).

Timeline helpers: agent activity is a list of (start_ms, end_ms) active spans on a 20 ms
playout grid; an utterance ends at the first frame of a >= 500 ms inactive run.
Mock programs (BUILD_PLAN M2 table): (a) stops 300 ms after onset, (b) never stops,
(c) stops on everything incl. backchannels, (d) short reply ending 1.2 s after onset,
(e) cancel then restart 20 ms later (no 500 ms gap).
"""
from __future__ import annotations

import numpy as np
import pytest

from duplexbench.metrics import floor_control, latency
from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, Event, OverlayRow
from duplexbench.scenarios.schema import Scenario
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()
END_MS = 30_000


def _scenario():
    return Scenario.model_validate({
        "id": "fc", "version": "1", "split": "public-dev", "domain": "x", "track": "A",
        "source": "scripted", "caller_voice": "v",
        "steps": [{"id": "s1", "text": "a", "pause_ms": 900}, {"id": "s2", "text": "b"}],
        "overlays": [
            {"id": "y", "cls": "Y-INT", "anchor_step": "s1", "placement": "during_agent",
             "offset_ms": 500, "text": "no I meant Friday", "response_anchors": ["friday"]},
            {"id": "w", "cls": "Y-CMD", "anchor_step": "s2", "placement": "during_agent",
             "offset_ms": 500, "text": "hold on", "resumption_ms": 4000},
            {"id": "b", "cls": "H-BC", "anchor_step": "s2", "placement": "during_agent",
             "offset_ms": 500, "text": "uh-huh"},
            {"id": "p", "cls": "P-PAUSE", "anchor_step": "s1", "placement": "in_pause",
             "offset_ms": 0, "text": ""},
            {"id": "e", "cls": "E-END", "anchor_step": "s2", "placement": "during_agent",
             "offset_ms": 0, "text": "", "resumption_ms": 6000}]})


def _playout(spans):
    t = np.arange(0, END_MS, 20)
    act = np.zeros(len(t), bool)
    for a, b in spans:
        act[(t >= a) & (t < b)] = True
    arr = np.zeros(len(t), PLAYOUT_DTYPE)
    arr["t_ns"], arr["frame_idx"], arr["active"] = t * MS, np.arange(len(t)), act
    return arr


def _events(turns, extra=()):
    ev, seq = [], 0
    for sid, on, end in turns:
        ev.append(Event(on * MS, seq, "caller_turn_onset", "caller", {"step_id": sid}))
        ev.append(Event(end * MS, seq + 1, "caller_turn_end", "caller", {"step_id": sid}))
        seq += 2
    for k, (t, kind) in enumerate(extra):
        ev.append(Event(t * MS, 100 + k, kind, "agent", {}))
    return sorted(ev, key=lambda e: (e.t_ns, e.seq))


TURNS = [("s1", 0, 2000), ("s2", 10_000, 11_000)]


def _utt_events(spans):
    """agent_onset / agent_stop events matching spans (for control-twin alignment)."""
    out = []
    for i, (a, b) in enumerate(spans):
        out += [Event(a * MS, 500 + 2 * i, "agent_onset", "playout", {}),
                Event(b * MS, 501 + 2 * i, "agent_stop", "playout", {})]
    return out


def _rec(spans, rows, *, twin_spans=None, extra=(), twin=True):
    sc = _scenario()
    tw = None
    if twin:
        ts = twin_spans if twin_spans is not None else [(2500, 6000), (11_500, 15_500)]
        tw = CallRecord(sorted(_events(TURNS) + _utt_events(ts), key=lambda e: (e.t_ns, e.seq)),
                        _playout(ts), [], [], [], None, sc, None, call_id="ctl", is_control=True)
    ev = sorted(_events(TURNS, extra) + _utt_events(spans), key=lambda e: (e.t_ns, e.seq))
    return CallRecord(ev, _playout(spans), rows, [], [], None, sc, tw, call_id="ovl",
                      trial=0, condition="C0", system_id="mock")


# overlay rows (send-time onset/end); agent replies to s1 at 2.5 s and to s2 at 11.5 s
ROW_Y = OverlayRow("y", "Y-INT", 3000 * MS, 3000 * MS, 3000 * MS, 4200 * MS)
ROW_W = OverlayRow("w", "Y-CMD", 12_000 * MS, 12_000 * MS, 12_000 * MS, 12_400 * MS,
                   ("resume_ns:16400000000",))
ROW_B = OverlayRow("b", "H-BC", 12_000 * MS, 12_000 * MS, 12_000 * MS, 12_400 * MS)
ROW_P = OverlayRow("p", "P-PAUSE", 900 * MS, 900 * MS, 900 * MS, 1800 * MS, ("resume_ns:1800000000",))
ROW_E = OverlayRow("e", "E-END", 11_000 * MS, 11_000 * MS, None, None)


def _get(obs, metric, eid, variant=None):
    hit = [o for o in obs if o.metric == metric and o.event_id == eid
           and (variant is None or f"@{variant}" in o.tags)]
    assert len(hit) == 1, (metric, eid, variant, hit)
    return hit[0]


def test_a_stops_300ms():
    # s1 reply 2.5 s, stops 300 ms after the Y-INT at 3.0 s; s2 reply 11.5 s holds over the BC
    rec = _rec([(2500, 3300), (11_500, 15_500)], [ROW_Y, ROW_B, ROW_P, ROW_E])
    obs = floor_control.observe(rec, TH)
    for b in (500, 1000, 2000):
        o = _get(obs, "yield_rate", "y", b)
        assert (o.value, o.valid) == (1.0, True) and "cls:Y-INT" in o.tags
    assert _get(obs, "t_stop_ear", "y").value == 300.0
    assert _get(obs, "talk_over_share", "y").value == pytest.approx(300 / 1200)
    assert _get(obs, "hold_speaking_rate", "b", 2000).value == 1.0
    assert _get(obs, "hold_speaking_rate", "b", 1000).value == 1.0
    leak = _get(obs, "content_leakage_rate", "b")
    assert not leak.valid and "no_refasr" in leak.tags
    rr = _get(obs, "respond_resume", "y")
    assert not rr.valid and {"classifier", "no_refasr"} <= set(rr.tags)


def test_b_never_stops():
    rec = _rec([(2500, 6000), (11_500, 15_500)], [ROW_Y, ROW_B])
    obs = floor_control.observe(rec, TH)
    assert _get(obs, "yield_rate", "y", 1000).value == 0.0
    st = _get(obs, "t_stop_ear", "y")
    assert st.value is None and not st.valid and "not_stopped" in st.tags
    assert _get(obs, "hold_speaking_rate", "b", 2000).value == 1.0
    assert _get(obs, "talk_over_share", "y").value == 1.0


def test_c_stops_on_everything():
    rec = _rec([(2500, 3300), (11_500, 12_300)], [ROW_Y, ROW_B])
    obs = floor_control.observe(rec, TH)
    assert _get(obs, "yield_rate", "y", 1000).value == 1.0
    assert _get(obs, "hold_speaking_rate", "b", 2000).value == 0.0
    assert _get(obs, "hold_speaking_rate", "b", 1000).value == 0.0


def test_d_short_reply_ambiguous_end():
    # control twin's matched turns last 1.2 s < offset 0.5 + B
    rec = _rec([(2500, 3300), (11_500, 12_700)], [ROW_Y, ROW_B],
               twin_spans=[(2500, 3700), (11_500, 12_700)])
    obs = floor_control.observe(rec, TH)
    y = _get(obs, "yield_rate", "y", 500)            # 1200 >= 500 + 500: valid
    assert y.valid and y.value == 1.0
    for b in (1000, 2000):
        o = _get(obs, "yield_rate", "y", b)
        assert not o.valid and o.value is None and "ambiguous_end" in o.tags
    h = _get(obs, "hold_speaking_rate", "b", 2000)
    assert not h.valid and "ambiguous_end" in h.tags


def test_e_cancel_restart_earns_nothing():
    # cancel at 3.25 s, restart 20 ms later: only a 20 ms gap, never a 500 ms inactivity run
    rec = _rec([(2500, 3250), (3270, 6000), (11_500, 15_500)], [ROW_Y, ROW_B],
               extra=[(3250, "cancel_or_truncate")])
    obs = floor_control.observe(rec, TH)
    assert _get(obs, "yield_rate", "y", 1000).value == 0.0
    assert _get(obs, "hold_speaking_rate", "b", 2000).value == 1.0


def test_contested_and_not_injected_tags():
    rows = [OverlayRow("y", "Y-INT", 3000 * MS, 3400 * MS, 3400 * MS, 4600 * MS, ("contested",)),
            OverlayRow("b", "H-BC", None, None, None, None, ("not_injected",))]
    obs = floor_control.observe(_rec([(2500, 3700), (11_500, 15_500)], rows), TH)
    y = _get(obs, "yield_rate", "y", 1000)
    assert not y.valid and y.value == 1.0 and "contested" in y.tags   # value kept, excluded
    b = _get(obs, "hold_speaking_rate", "b", 2000)
    assert not b.valid and b.value is None and "not_injected" in b.tags


def test_no_twin_is_unmatched_control():
    obs = floor_control.observe(_rec([(2500, 3300), (11_500, 15_500)], [ROW_Y], twin=False), TH)
    o = _get(obs, "yield_rate", "y", 1000)
    assert not o.valid and "unmatched_control" in o.tags


def test_wait_compliance():
    ok = floor_control.observe(_rec([(2500, 6000), (11_500, 12_300)], [ROW_W]), TH)
    assert _get(ok, "wait_compliance", "w").value == 1.0
    assert _get(ok, "yield_rate", "w", 1000).value == 1.0
    # an idle prompt at 14 s, before the scripted resumption at 16.4 s
    bad = floor_control.observe(_rec([(2500, 6000), (11_500, 12_300), (14_000, 14_600)], [ROW_W]), TH)
    assert _get(bad, "wait_compliance", "w").value == 0.0


def test_hold_listening_pause():
    held = floor_control.observe(_rec([(2500, 6000)], [ROW_P]), TH)
    assert _get(held, "hold_listening_rate", "p").value == 1.0
    early = floor_control.observe(_rec([(1200, 1700), (2500, 6000)], [ROW_P]), TH)
    assert _get(early, "hold_listening_rate", "p").value == 0.0


def test_response_rate_and_turn_gap_eend_1300():
    rec = _rec([(2500, 6000), (12_300, 15_000)], [ROW_E])
    obs = floor_control.observe(rec, TH)
    assert _get(obs, "response_rate", "e", 2000).value == 1.0
    assert _get(obs, "response_rate", "e", 5000).value == 1.0
    gap = [o for o in latency.t_turn_gap_ear(rec, TH) if o.event_id == "s2"][0]
    assert gap.value == 1300.0
    late = floor_control.observe(_rec([(2500, 6000), (14_000, 15_000)], [ROW_E]), TH)
    assert _get(late, "response_rate", "e", 2000).value == 0.0
    assert _get(late, "response_rate", "e", 5000).value == 1.0


def test_control_calls_not_scored():
    rec = _rec([(2500, 3300)], [ROW_Y])
    rec.is_control = True
    assert floor_control.observe(rec, TH) == []
