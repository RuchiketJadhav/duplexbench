"""Track C task metrics on synthetic timelines (BUILD_PLAN M4-T05; spec §4.2, D6)."""
from pathlib import Path

import numpy as np
import pytest
import yaml

from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord, Event, SimRow, ToolRow
from duplexbench.metrics.task import score_track_c
from duplexbench.refasr.engine import Word
from duplexbench.scenarios.schema import Scenario
from duplexbench.thresholds import Thresholds

TH = Thresholds()
NS = 1_000_000
FRAME = 20
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev" / "track_c_001.yaml"
GOOD_STATE = {"bookings": {"BK-4471": {"date": "2026-11-13", "time": "09:00", "status": "confirmed"}}}


def _scenario(**over):
    raw = yaml.safe_load(FIX.read_text(encoding="utf-8"))
    raw.update(over)
    return Scenario.model_validate(raw)


def playout(active_ms, total_ms):
    """active_ms: list of (on, off) ms spans of agent audio."""
    n = total_ms // FRAME
    p = np.zeros(n, PLAYOUT_DTYPE)
    p["t_ns"] = np.arange(n) * FRAME * NS
    p["frame_idx"] = np.arange(n)
    for on, off in active_ms:
        p["active"][on // FRAME:off // FRAME] = True
    return p


def rec_for(sc, pl=None, sim_turns=(), events=()):
    return CallRecord(events=list(events), playout=pl if pl is not None else np.zeros(0, PLAYOUT_DTYPE),
                      overlays=[], tool_calls=[], sim_turns=list(sim_turns), wav_path=Path("x.wav"),
                      scenario=sc, twin=None, call_id="c1", trial=0, condition="C0", system_id="mock")


def tool(tid, name, start_ms, result_ms, result=None):
    return ToolRow(tid, name, {}, start_ms * NS, None if result_ms is None else result_ms * NS,
                   None, None, result if result is not None else {"status": 200})


def words(spec):
    """'text@start' tokens, each word 250 ms long."""
    out = []
    for tok in spec.split():
        t, s = tok.split("@")
        out.append(Word(t, int(s), int(s) + 250))
    return out


def sim(turn_idx, send_ms, last_end_ms):
    return SimRow(turn_idx, "x", [{"text": "x", "start_ms": 0, "end_ms": last_end_ms}],
                  send_ms * NS, None, False, False)


CONFIRM = words("your@10000 booking@10300 is@10600 moved@10900 to@11200 friday@11500 "
                "november@11800 thirteenth@12100 at@12400 nine@12700 am@13000")


def get(obs, metric, event_id=None):
    hits = [o for o in obs if o.metric == metric and (event_id is None or o.event_id == event_id)]
    assert len(hits) == 1, (metric, event_id, hits)
    return hits[0]


# -- Pass@1 -------------------------------------------------------------------------------
def test_pass_at_1_all_conditions_met():
    sc = _scenario()
    obs = score_track_c(rec_for(sc), sc, [tool("t1", "reschedule_booking", 9000, 9500)], CONFIRM, TH,
                        final_state=GOOD_STATE)
    o = get(obs, "pass_at_1")
    assert o.value == 1.0 and o.valid and o.tags == ("domain:scheduling",)
    assert get(obs, "policy_violation_rate").value == 0.0


def test_pass_at_1_wrong_state():
    sc = _scenario()
    bad = {"bookings": {"BK-4471": {"date": "2026-11-12", "time": "15:00", "status": "confirmed"}}}
    obs = score_track_c(rec_for(sc), sc, [], CONFIRM, TH, final_state=bad)
    assert get(obs, "pass_at_1").value == 0.0


def test_pass_at_1_confirmation_not_spoken():
    sc = _scenario()
    obs = score_track_c(rec_for(sc), sc, [], words("all@10000 set@10300"), TH, final_state=GOOD_STATE)
    assert get(obs, "pass_at_1").value == 0.0


def test_pass_at_1_policy_violation_row():
    sc = _scenario()
    rows = [tool("t1", "reschedule_booking", 9000, 9100,
                 result={"status": 403, "error": {"code": "identity_not_verified"}})]
    obs = score_track_c(rec_for(sc), sc, rows, CONFIRM, TH, final_state=GOOD_STATE)
    assert get(obs, "pass_at_1").value == 0.0
    assert get(obs, "policy_violation_rate").value == 1.0


def test_pass_at_1_from_raw_rows_and_state_event():
    sc = _scenario()
    rows = [{"tool_call_id": "t1", "name": "reschedule_booking", "args_json": "{}", "start_ns": 9000 * NS,
             "result_ns": 9500 * NS, "result_json": '{"status": 200}'}]
    ev = [Event(20_000 * NS, 1, "final_state", "tools", {"state": GOOD_STATE})]
    obs = score_track_c(rec_for(sc, events=ev), sc, rows, CONFIRM, TH)
    assert get(obs, "pass_at_1").value == 1.0


def test_pass_at_1_without_state_is_invalid():
    sc = _scenario()
    o = get(score_track_c(rec_for(sc), sc, [], CONFIRM, TH), "pass_at_1")
    assert not o.valid and o.value is None and "no_state" in o.tags


# -- T_tool_offset -----------------------------------------------------------------------------
def test_tool_offset_from_last_caller_turn_and_preemptive():
    sc = _scenario()
    turns = [sim(0, 0, 2000), sim(1, 4000, 2500), sim(2, 8000, 1500)]    # ends 2000, 6500, 9500
    rows = [tool("r", "find_booking", 7000, 7100), tool("w", "reschedule_booking", 9200, 9300)]
    obs = score_track_c(rec_for(sc, sim_turns=turns), sc, rows, [], TH, final_state=GOOD_STATE)
    assert get(obs, "t_tool_offset", "r").value == 500.0                 # 7000 - 6500
    assert get(obs, "t_tool_offset", "w").value == 2700.0                # 9200 - 6500 (s3 not ended yet)
    assert get(obs, "preemptive_tool_rate", "w").value == 0.0
    assert not [o for o in obs if o.metric == "preemptive_tool_rate" and o.event_id == "r"]  # reads excluded


def test_tool_offset_steps_speech_end_and_no_turn():
    raw_steps = [{"id": "s1", "text": "a", "speech_end_ms": 1000},
                 {"id": "s2", "text": "b", "speech_end_ms": 3000}]
    sc = _scenario(steps=raw_steps)
    rows = [tool("early", "find_booking", 500, 600), tool("w", "reschedule_booking", 2500, 2600)]
    obs = score_track_c(rec_for(sc), sc, rows, [], TH, final_state=GOOD_STATE)
    assert not get(obs, "t_tool_offset", "early").valid
    assert get(obs, "t_tool_offset", "w").value == 1500.0


def test_tool_offset_anchor_step_preemptive():
    """A tool def's `anchor_step` names the step carrying the arguments; a write
    before that step ends is pre-emptive (FDB v3's 'Rome' call)."""
    sc = _scenario(steps=[{"id": "s1", "text": "a", "speech_end_ms": 1000},
                          {"id": "s2", "text": "b", "speech_end_ms": 4000}])
    sc = sc.model_copy(update={"tools": [d.model_copy(update={"anchor_step": "s2"})
                                         if d.name == "reschedule_booking" else d
                                         for d in sc.tools]})
    obs = score_track_c(rec_for(sc), sc, [tool("w", "reschedule_booking", 2000, 2100)], [], TH,
                        final_state=GOOD_STATE)
    assert get(obs, "t_tool_offset", "w").value == -2000.0
    assert get(obs, "preemptive_tool_rate", "w").value == 1.0


# -- T_tool_to_info_ear + anchor_offset_in_utt_ms (D6) ---------------------------------------------
def test_filler_then_anchor_700ms_into_utterance():
    """'one moment your slot is three pm': the utterance starts with filler at
    10,000 ms, the tool result arrives at 9,800 ms, the anchor word 'three' is
    heard at 10,700 ms. T_tool_to_info_ear uses the utterance onset (200 ms);
    the diagnostic is 700 ms."""
    sc = _scenario(tools=[{"name": "check_availability", "kind": "read", "result_anchors": ["three"]}])
    pl = playout([(10_000, 11_200)], 14_000)
    w = words("one@10000 moment@10200 your@10350 slot@10500 is@10600 three@10700 pm@10950")
    obs = score_track_c(rec_for(sc, pl), sc, [tool("t", "check_availability", 7000, 9800)], w, TH,
                        final_state={})
    assert get(obs, "t_tool_to_info_ear", "t").value == pytest.approx(200.0)
    d = get(obs, "anchor_offset_in_utt_ms", "t")
    assert d.value == pytest.approx(700.0) and "diagnostic" in d.tags and "domain:scheduling" in d.tags


def test_filler_during_wait_then_result_utterance():
    """Filler for the whole wait in its own utterance; the result is stated in a new
    utterance 400 ms after the result: T_tool_to_info_ear = 400, not ~0."""
    sc = _scenario(tools=[{"name": "check_availability", "kind": "read", "result_anchors": ["9:00", "nine"]}])
    pl = playout([(5_000, 9_000), (10_400, 12_000)], 14_000)
    w = words("still@5000 checking@5400 for@6000 you@6300 "
              "nine@10400 am@10700 works@11000")
    obs = score_track_c(rec_for(sc, pl), sc, [tool("t", "check_availability", 5000, 10_000)], w, TH,
                        final_state={})
    assert get(obs, "t_tool_to_info_ear", "t").value == pytest.approx(400.0)
    assert get(obs, "anchor_offset_in_utt_ms", "t").value == pytest.approx(0.0)


def test_anchor_before_result_is_ignored_and_missing_is_invalid():
    sc = _scenario(tools=[{"name": "check_availability", "kind": "read", "result_anchors": ["nine"]}])
    pl = playout([(5_000, 6_000)], 8_000)
    w = words("nine@5000")                                  # said before the result: a guess
    obs = score_track_c(rec_for(sc, pl), sc, [tool("t", "check_availability", 4000, 7000)], w, TH,
                        final_state={})
    o = get(obs, "t_tool_to_info_ear", "t")
    assert not o.valid and o.value is None
    assert not [x for x in obs if x.metric == "anchor_offset_in_utt_ms"]


def test_multiword_anchor_and_utterances_from_words():
    """No playout: utterances come from reference-ASR words; 'november 12' spans two words."""
    sc = _scenario(tools=[{"name": "find_booking", "kind": "read", "result_anchors": ["november 12"]}])
    w = words("okay@3000 found@3300 it@3600 november@4000 twelfth@4300")
    obs = score_track_c(rec_for(sc), sc, [tool("t", "find_booking", 1000, 2500)], w, TH, final_state={})
    assert get(obs, "t_tool_to_info_ear", "t").value == pytest.approx(500.0)     # onset 3000 - 2500
    assert get(obs, "anchor_offset_in_utt_ms", "t").value == pytest.approx(1000.0)


def test_no_judged_tags():
    sc = _scenario()
    obs = score_track_c(rec_for(sc), sc, [tool("t1", "reschedule_booking", 9000, 9500)], CONFIRM, TH,
                        final_state=GOOD_STATE)
    assert all("judged" not in o.tags and "classifier" not in o.tags for o in obs)
