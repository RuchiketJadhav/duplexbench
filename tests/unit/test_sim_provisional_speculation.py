"""fix-42: the speculative draft starts from provisional words; the transcript stays finals-only.

Real Playout + SimulatedCaller on a VirtualClock (helpers from test_sim_hears_refasr): the
agent plays 1,000 ms of speech from t=0; FakeASR releases each word when the ear audio
pushed reaches its end_ms, so a final with a large end_ms is a late final.

Timeline (ear ms): speech 0-1000; the segmenter reports the stop 500 ms later and the
policy is already past sim_speculate_ms (300), so the speculation starts at about 1500;
the floor is at 1000 + sim_eot_ms (700) + U(300, 800), i.e. between 2000 and 2500.
"""
from __future__ import annotations

import pytest

from duplexbench.refasr.engine import FakeASR, Word, merge_provisional
from duplexbench.simulator.simulated_caller import norm_text, norm_transcript
from test_sim_hears_refasr import _call

PROV = [Word("hello", 0, 300, 0.5, False), Word("there", 400, 800, 0.5, False)]
FIRST = "hi I need help"
FLOOR_MAX_MS = 1000 + 700 + 800


# -- helpers -------------------------------------------------------------------------------
def test_norm_text_rule():
    assert norm_text("Hello,  THERE.") == "hello there"
    assert norm_text("  it's  4:30 p.m.! ") == "its 430 pm"
    assert norm_text("...") == ""
    assert norm_transcript([("agent", "Hi, you.")]) == (("agent", "hi you"),)


def test_merge_provisional_latest_release_per_slot():
    p = merge_provisional([], Word("helo", 0, 250, 0.4, False))
    p = merge_provisional(p, Word("thar", 400, 700, 0.4, False))
    p = merge_provisional(p, Word("hello", 0, 300, 0.6, False))      # correction, same slot
    assert [w.text for w in p] == ["hello", "thar"]
    p = merge_provisional(p, Word("there", 380, 720, 0.6, False))    # overlaps "thar"
    assert [w.text for w in p] == ["hello", "there"]


# -- (a) provisional start, matching final before the floor -> used -------------------------
@pytest.mark.parametrize("threaded", [False, True])
def test_provisional_speculation_used_when_final_matches(threaded):
    finals = [Word("Hello,", 0, 1650), Word("there.", 400, 1700)]
    caller, llm = _call(FakeASR(PROV + finals), threaded=threaded, until_ms=4000)
    spoken = [r for r in caller.rows if not r["discarded"]]
    assert spoken and spoken[0]["text"] == FIRST
    r0 = spoken[0]
    assert r0["speculative"] == 1 and r0["speculative_source"] == "provisional"
    assert r0["provisional_reply"] == 0                    # the finals confirmed it
    assert r0["send_ns"] <= FLOOR_MAX_MS * 1_000_000 + 20 * 1_000_000
    assert not [r for r in caller.rows if r["discarded"]]
    # the speculation was generated from the provisional text, before the finals
    assert llm.contexts[0].transcript == [("agent", "hello there")]
    assert caller.flags == {"speculation_from_provisional": 1}
    assert caller.policy.discarded_speculations == 0
    # transcript and refasr_words stay finals-only
    assert caller.transcript[:2] == [("agent", "Hello, there."), ("caller", FIRST)]
    assert all(w.final for w in caller.refasr_words)
    assert [w.text for w in caller.refasr_provisional] == ["hello", "there"]


# -- (b) the final differs -> discarded, fresh generation, counters -----------------------
def test_provisional_speculation_discarded_when_final_differs():
    finals = [Word("Hello,", 0, 1650), Word("where?", 400, 1700)]
    caller, llm = _call(FakeASR(PROV + finals), until_ms=4000)
    disc = [r for r in caller.rows if r["discarded"]]
    spoken = [r for r in caller.rows if not r["discarded"]]
    assert len(disc) == 1 and disc[0]["speculative_source"] == "provisional"
    assert disc[0]["text"] == FIRST and disc[0]["send_ns"] is None
    assert spoken[0]["speculative"] == 0 and spoken[0]["speculative_source"] is None
    assert spoken[0]["provisional_reply"] == 0
    # one extra LLM call: the speculation (provisional) + the fresh one (finals)
    assert [c.transcript for c in llm.contexts[:2]] == \
        [[("agent", "hello there")], [("agent", "Hello, where?")]]
    assert caller.flags == {"speculation_from_provisional": 1, "provisional_mismatches": 1}
    assert caller.policy.discarded_speculations == 1
    assert caller.transcript[:2] == [("agent", "Hello, where?"), ("caller", FIRST)]


# -- (c) no final by the floor -> provisional reply; the final lands in the transcript later
@pytest.mark.parametrize("threaded", [False, True])
def test_no_final_by_floor_speaks_provisional_reply(threaded):
    finals = [Word("Hello", 0, 3200), Word("there", 400, 3300)]
    caller, llm = _call(FakeASR(PROV + finals), threaded=threaded, until_ms=5000)
    spoken = [r for r in caller.rows if not r["discarded"]]
    r0 = spoken[0]
    assert r0["text"] == FIRST and r0["speculative_source"] == "provisional"
    assert r0["provisional_reply"] == 1
    assert r0["send_ns"] < 3200 * 1_000_000                # before the final arrived
    assert caller.flags == {"speculation_from_provisional": 1, "provisional_replies": 1}
    # the late final words are written into the agent turn, before the caller's line
    assert caller.transcript == [("agent", "Hello there"), ("caller", FIRST)]
    assert [w.text for w in caller.refasr_words] == ["Hello", "there"]
    assert all(w.final for w in caller.refasr_words)
    assert caller.refasr_text == "Hello there"


def test_store_rows_keep_the_sim_turns_columns():
    finals = [Word("Hello", 0, 3200), Word("there", 400, 3300)]
    caller, _ = _call(FakeASR(PROV + finals), until_ms=5000)
    assert caller.rows and all("provisional_reply" in r for r in caller.rows)
    stored = caller.store.rows["sim_turns"]
    assert stored and all("provisional_reply" not in r and "speculative_source" not in r
                          for r in stored)


# -- (d) finals only: unchanged ------------------------------------------------------------
def test_finals_only_engine_keeps_final_source():
    caller, llm = _call(FakeASR([Word("hello", 0, 300), Word("there", 400, 800)]))
    spoken = [r for r in caller.rows if not r["discarded"]]
    assert spoken[0]["speculative_source"] == "final" and spoken[0]["provisional_reply"] == 0
    assert caller.flags == {}


# -- counters reach the report: aggregate "simulator_counts" + one caveat line each ---------
def test_counters_reach_aggregate_and_caveats(tmp_path):
    import json
    import sqlite3

    from _report_fixture import build
    from duplexbench.report.aggregate import aggregate
    from duplexbench.report.caveats import caveats
    from duplexbench.thresholds import Thresholds

    th = Thresholds()
    p = build(tmp_path / "e.sqlite")
    assert "simulator_counts" not in aggregate(p, "r1", th)        # zero -> absent
    conn = sqlite3.connect(p)
    ids = [r[0] for r in conn.execute(
        "SELECT call_id FROM calls WHERE system_id='sysA' AND is_control=0 ORDER BY call_id")]
    flags = [{"speculation_from_provisional": 2, "provisional_mismatches": 1},
             {"speculation_from_provisional": 1, "provisional_replies": 1}]
    for cid, f in zip(ids, flags):
        conn.execute("UPDATE calls SET flags_json=? WHERE call_id=?",
                     (json.dumps({"simulator": {"flags": f}}), cid))
    ctl = conn.execute("SELECT call_id FROM calls WHERE is_control=1").fetchone()
    if ctl:                                                         # control calls never count
        conn.execute("UPDATE calls SET flags_json=? WHERE call_id=?",
                     (json.dumps({"simulator": {"flags": {"provisional_replies": 9}}}), ctl[0]))
    conn.commit()
    conn.close()
    agg = aggregate(p, "r1", th)
    assert agg["simulator_counts"] == {"sysA": {"speculation_from_provisional": 3,
                                                "provisional_mismatches": 1,
                                                "provisional_replies": 1}}
    sim = [t for k, t in caveats(agg, th) if k == "simulator"]
    assert sum("(speculation_from_provisional)" in t and t.startswith("sysA: 3 ")
               for t in sim) == 1
    assert sum("(provisional_mismatches)" in t and t.startswith("sysA: 1 ") for t in sim) == 1
    assert sum("(provisional_replies)" in t and t.startswith("sysA: 1 ") for t in sim) == 1


# -- cost: the discarded (mismatched) call is in the actual usage; projection unchanged ---
def test_mismatch_usage_counts_the_discarded_call(monkeypatch):
    import test_sim_hears_refasr as hears

    from duplexbench.metrics import cost

    class UsageLLM(hears.CtxLLM):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.usage_log = []

        async def next_utterance(self, ctx):
            self.usage_log.append({"input_tokens": 100, "output_tokens": 10})
            return await super().next_utterance(ctx)

    monkeypatch.setattr(hears, "CtxLLM", UsageLLM)
    finals = [Word("Hello,", 0, 1650), Word("where?", 400, 1700)]
    caller, llm = hears._call(FakeASR(PROV + finals), until_ms=4000)
    assert caller.flags.get("provisional_mismatches") == 1
    tot = cost.sum_chat_usage(llm.usage_log)
    assert tot["responses"] == len(llm.contexts) >= 2       # speculation + fresh reply
    prices = {"projection": {"extra_turns_per_call": 2, "input_tokens_per_turn": 1,
                             "output_tokens_per_turn": 1}}
    assert cost.caller_llm_turns(3, prices) == 5             # projection: steps + extra only
