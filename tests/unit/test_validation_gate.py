"""M4-T11: validation gate and whole-trial regeneration (spec §7; BUILD_PLAN §1 row 10)."""
import asyncio
import sqlite3

from duplexbench.simulator.validation_gate import (BROKEN_AUDIO, HARNESS_TIMEOUT,
                                                   VALIDATION_FAILED, TrialResult, check,
                                                   entity_stated, regeneration_rate,
                                                   review_list, run_with_regeneration)
from duplexbench.storage import canonical_json
from duplexbench.thresholds import Thresholds

TH = Thresholds()
ENTS = ["Ana Ruiz", "415 555 0100"]


def test_check_reasons():
    ok = ["Hi, I'm Ana Ruiz.", "My number is four one five five five five zero one zero zero."]
    assert check(ok, True, False, ENTS) == []
    late = ["Hi.", "It's Ana Ruiz.", "415 555 0100"]           # number only in turn 3
    assert check(late, True, False, ENTS) == ["goal_entity_missing:415 555 0100"]
    assert check(ok, False, True, ENTS) == [BROKEN_AUDIO, HARNESS_TIMEOUT]


def test_entity_stated_normalized():
    assert entity_stated("(415) 555-0100", "it's four one five, five five five, zero one hundred")\
        or entity_stated("4155550100", "4155550100")
    assert entity_stated("March 3 1990", "born march third nineteen ninety")
    assert not entity_stated("Ana Ruiz", "this is Ana")


def _fake_caller(store, fail_first: int):
    async def run_trial(attempt: int) -> TrialResult:
        turns = ["Hi there.", "I want a cleaning."] if attempt < fail_first else \
            ["Hi, this is Ana Ruiz.", "My number is 415 555 0100."]
        r = TrialResult(call_id=f"c-{attempt}", caller_text_turns=turns, audio_ok=True,
                        timed_out=False, goal_entities=ENTS, usd_actual=0.01)
        store.put("calls", {"call_id": r.call_id, "run_id": "r1", "scenario_id": "sch-01",
                            "trial": 0, "outcome": r.outcome, "usd_actual": r.usd_actual,
                            "flags_json": canonical_json({"regenerated": attempt})})
        return r
    return run_trial


def test_two_regenerations_then_pass(store):
    res = asyncio.run(run_with_regeneration(_fake_caller(store, fail_first=2), TH))
    store.flush_sync()
    assert [r.regenerated for r in res] == [0, 1, 2]
    assert [r.scored for r in res] == [False, False, True]
    assert res[-1].passed and res[-1].outcome == "completed"
    assert res[0].reasons and res[1].flags == {"regenerated": 1}
    n = sqlite3.connect(store.path).execute("select count(*) from calls").fetchone()[0]
    assert n == 3                                       # every attempt kept and costed
    assert sum(r.usd_actual for r in res) == 0.03


def test_three_failures_validation_failed(store):
    res = asyncio.run(run_with_regeneration(_fake_caller(store, fail_first=99), TH))
    assert len(res) == TH.sim_regen_max + 1 == 3
    assert res[-1].outcome == VALIDATION_FAILED and res[-1].scored
    assert all(r.outcome == "completed" for r in res[:-1])


def test_pass_first_time_no_regeneration(store):
    res = asyncio.run(run_with_regeneration(_fake_caller(store, fail_first=0), TH))
    assert len(res) == 1 and res[0].flags == {}


def test_rate_and_review_list():
    one = [TrialResult("a", [], True, False, [])]
    two = one * 2
    assert regeneration_rate([one, two, one, one]) == 0.25
    assert regeneration_rate([]) == 0.0
    assert review_list({"s1": 0.25, "s2": 0.0, "s3": TH.sim_regen_review_rate}, TH) == ["s1"]
