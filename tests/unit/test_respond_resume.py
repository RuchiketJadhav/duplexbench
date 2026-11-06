"""M2-T10: Respond / Resume / Uncertain (spec §4.1)."""
import pytest

from duplexbench.classifiers.respond_resume import content_words, jaccard, label, observation
from duplexbench.thresholds import Thresholds

TH = Thresholds()
PRIOR = "appointment tuesday morning dentist downtown clinic insurance copay"


def test_anchor_present_is_respond():
    assert label("Sure, I moved it to Friday.", ["friday"], PRIOR, TH) == "respond"
    assert label("Okay, next Friday works.", ["next friday"], PRIOR, TH) == "respond"


def test_resume_at_jaccard_0_6():
    u = "appointment tuesday morning dentist downtown clinic parking validated"   # 6 shared / 10
    assert jaccard(content_words(u), content_words(PRIOR)) == pytest.approx(0.6)
    assert label(u, ["friday"], PRIOR, TH) == "resume"


def test_uncertain_at_jaccard_0_3():
    prior = "appointment tuesday morning dentist insurance copay billing"
    u = "appointment tuesday morning parking validated garage"                    # 3 shared / 10
    assert jaccard(content_words(u), content_words(prior)) == pytest.approx(0.3)
    assert label(u, ["friday"], prior, TH) == "uncertain"


def test_empty_text_is_uncertain():
    assert label("", ["friday"], PRIOR, TH) == "uncertain"
    assert label(None, ["friday"], PRIOR, TH) == "uncertain"


def test_function_words_do_not_count():
    assert content_words("the and of to a") == set()


def test_observation_carries_classifier_tag():
    kw = dict(call_id="c", scenario_id="s", trial=0, condition="C0", system_id="m",
              anchors=["friday"], prior_utt_text=PRIOR, th=TH)
    o = observation(event_id="o1", utt_text="moved to friday", **kw)
    assert o.metric == "respond_resume" and o.value == 1.0 and o.valid
    assert "classifier" in o.tags and "label=respond" in o.tags
    o2 = observation(event_id="o2", utt_text="", **kw)
    assert o2.value == 0.0 and "classifier" in o2.tags and "label=uncertain" in o2.tags
