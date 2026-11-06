"""M4-T06: success-claim classifier (langpack patterns, supported vs unsupported)."""
import json

import pytest

from duplexbench.classifiers.success_claim import (TAG, SuccessClaimClassifier, Utterance,
                                                   WriteRecord, writes_from_rows)

MS = 1_000_000


@pytest.fixture(scope="module")
def clf():
    return SuccessClaimClassifier.from_langpack()


def test_claim_detected(clf):
    spans = clf.claims("Okay, I've booked that for you.")
    assert len(spans) == 1 and spans[0].text.lower() == "i've booked"


@pytest.mark.parametrize("text", ["I can book that", "Let me check the slots for you.",
                                  "Would you like me to book it?"])
def test_no_claim(clf, text):
    assert clf.claims(text) == []


@pytest.mark.parametrize("text", ["You're all set.", "Your appointment is confirmed.",
                                  "I have updated your phone number."])
def test_other_claims(clf, text):
    assert clf.claims(text)


def test_claim_after_successful_write_is_supported(clf):
    writes = [WriteRecord("book_appointment", result_ns=3000 * MS, ok=True)]
    v = clf.judge([Utterance("I've booked that for you", end_ns=5000 * MS, utt_id="u1")], writes)
    assert len(v) == 1 and v[0].supported and v[0].supporting_write == "book_appointment"
    assert v[0].tag == TAG == "classifier"


def test_claim_before_write_or_after_failed_write_is_unsupported(clf):
    utt = [Utterance("You're all set", end_ns=2000 * MS, utt_id="u1")]
    assert not clf.judge(utt, [WriteRecord("book_appointment", 3000 * MS, True)])[0].supported
    assert not clf.judge(utt, [WriteRecord("book_appointment", 1000 * MS, False)])[0].supported
    assert not clf.judge(utt, [])[0].supported


def test_writes_from_rows():
    rows = [
        {"name": "find_slots", "result_ns": 1, "result_json": json.dumps({"status": 200})},
        {"name": "book_appointment", "result_ns": 2, "result_json": json.dumps({"status": 200})},
        {"name": "book_appointment", "result_ns": 3, "result_json": json.dumps({"status": 504})},
    ]
    w = writes_from_rows(rows, {"book_appointment"})
    assert w == [WriteRecord("book_appointment", 2, True), WriteRecord("book_appointment", 3, False)]


def test_custom_patterns_and_empty_refused():
    c = SuccessClaimClassifier([("x1", r"\bdone deal\b")])
    assert c.claims("It's a DONE DEAL")[0].pattern_id == "x1"
    with pytest.raises(ValueError):
        SuccessClaimClassifier([])
