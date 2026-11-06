"""Track D entity metrics (BUILD_PLAN M5-T02; spec §4.3)."""
from pathlib import Path

import numpy as np
import yaml

from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord
from duplexbench.metrics.entities import BELOW_FLOOR, NO_FLOOR, score
from duplexbench.refasr.engine import Word
from duplexbench.scenarios.schema import Scenario
from duplexbench.thresholds import Thresholds

TH = Thresholds()
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev" / "track_c_001.yaml"
ENTS = [
    {"id": "p", "type": "phone", "value": "(415) 555-0100"},
    {"id": "e", "type": "email", "value": "dana.ruiz@example.com"},
    {"id": "z", "type": "zip", "value": "94105"},
    {"id": "d", "type": "dob", "value": "1988-04-02"},
    {"id": "n", "type": "name", "value": "Dana Ruiz"},
    {"id": "a", "type": "amount", "value": "$42.10"},
    {"id": "c", "type": "confirmation_code", "value": "AB12X"},
    {"id": "b", "type": "booking_id", "value": "BK-4471"},       # not a scored type
]
ALL_FLOORS = {t: 0.95 for t in ("phone", "email", "zip", "dob", "name", "amount", "confirmation_code")}


def _scenario(ents=ENTS):
    raw = yaml.safe_load(FIX.read_text(encoding="utf-8"))
    raw["entities"] = ents
    return Scenario.model_validate(raw)


def _rec(sc):
    return CallRecord(events=[], playout=np.zeros(0, PLAYOUT_DTYPE), overlays=[], tool_calls=[],
                      sim_turns=[], wav_path=Path("x.wav"), scenario=sc, twin=None, call_id="c1",
                      trial=0, condition="C0", system_id="mock")


def _words(text):
    return [Word(w, i * 300, i * 300 + 250) for i, w in enumerate(text.split())]


SAID = ("Thanks Dana Ruiz. I have four one five five five five zero one zero zero, "
        "dana dot ruiz at example dot com, zip nine four one zero five, born april second "
        "nineteen eighty eight, and forty two dollars and ten cents, code a b one two x.")


# Fixed in fix-21 (soft boundaries): adjacent number phrases used to merge across a comma,
# "nineteen eighty eight, forty two dollars" -> "198842" (the "and" above predates the fix).


def by_id(obs):
    return {o.event_id: o for o in obs}


def test_all_correct_and_tagged():
    sc = _scenario()
    obs = by_id(score(_rec(sc), sc, _words(SAID), ALL_FLOORS, TH))
    assert set(obs) == {"p", "e", "z", "d", "n", "a", "c"}               # booking_id skipped
    for i, o in obs.items():
        assert o.metric == "readback_error_rate" and o.valid and o.value == 0.0, i
        assert f"type:{dict((e['id'], e['type']) for e in ENTS)[i]}" in o.tags
        assert "domain:scheduling" in o.tags and NO_FLOOR not in o.tags


def test_wrong_phone_and_code():
    sc = _scenario()
    said = SAID.replace("zero one zero zero", "zero one zero nine").replace("a b one two x", "a b one two k")
    obs = by_id(score(_rec(sc), sc, _words(said), ALL_FLOORS, TH))
    assert obs["p"].value == 1.0 and obs["c"].value == 1.0
    assert obs["e"].value == 0.0


def test_missing_readback_is_error():
    sc = _scenario([{"id": "p", "type": "phone", "value": "415-555-0100"}])
    obs = score(_rec(sc), sc, _words("okay thank you"), ALL_FLOORS, TH)
    assert obs[0].value == 1.0 and obs[0].valid


def test_below_floor_invalidates_type():
    sc = _scenario()
    fl = dict(ALL_FLOORS, phone=0.6)
    obs = by_id(score(_rec(sc), sc, _words(SAID), fl, TH, floor_min=0.8))
    assert not obs["p"].valid and obs["p"].value is None and BELOW_FLOOR in obs["p"].tags
    assert obs["e"].valid and BELOW_FLOOR not in obs["e"].tags


def test_no_minimum_no_invalidation_and_no_floor_tag():
    sc = _scenario()
    obs = by_id(score(_rec(sc), sc, _words(SAID), {"phone": 0.1}, TH))
    assert obs["p"].valid and obs["p"].value == 0.0
    assert NO_FLOOR in obs["e"].tags and NO_FLOOR not in obs["p"].tags
