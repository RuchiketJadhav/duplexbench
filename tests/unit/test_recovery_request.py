"""Recovery-request classifier (BUILD_PLAN M5-T03; spec §4.3)."""
from pathlib import Path

import numpy as np
import pytest

from duplexbench.classifiers.recovery_request import (TAG, classify, default_langpack, matches,
                                                       observation)
from duplexbench.metrics.base import PLAYOUT_DTYPE, CallRecord
from duplexbench.metrics.names import Metric
from duplexbench.scenarios.schema import Scenario

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev" / "track_c_001.yaml"


@pytest.fixture(scope="module")
def lp():
    return default_langpack()


@pytest.mark.parametrize("text", ["could you spell that", "Could you spell that for me, please?",
                                  "Sorry, I didn't catch that.", "Can you say that again?",
                                  "How do you spell your last name?"])
def test_requests(lp, text):
    assert classify(text, lp)


@pytest.mark.parametrize("text", ["thanks", "Your appointment is booked for Friday.", "", "I can spell it"])
def test_non_requests(lp, text):
    assert not classify(text, lp)


def test_matches_returns_pattern_ids(lp):
    assert matches("could you spell that", lp) and matches("thanks", lp) == []


def _rec():
    import yaml
    sc = Scenario.model_validate(yaml.safe_load(FIX.read_text(encoding="utf-8")))
    return CallRecord(events=[], playout=np.zeros(0, PLAYOUT_DTYPE), overlays=[], tool_calls=[],
                      sim_turns=[], wav_path=Path("x.wav"), scenario=sc, twin=None, call_id="c1",
                      trial=0, condition="C0", system_id="mock")


def test_observation_tagged_classifier():
    o = observation(_rec(), Metric.recovery_request_rate, 1.0, tags=("type:phone",))
    assert o.metric == "recovery_request_rate" and o.tags == (TAG, "type:phone")
    assert o.scenario_id == "track_c_001"


def test_observation_rejects_other_metrics():
    with pytest.raises(ValueError):
        observation(_rec(), Metric.pass_at_1, 1.0)
