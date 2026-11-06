"""Entity-mode normalizer extensions (BUILD_PLAN M5-T01). The plain `normalize` is
pinned separately by the unchanged asr-bench tests in test_normalize.py."""
import pytest

from duplexbench.metrics.normalize import (NORMALIZER_VERSION, normalize, normalize_entity,
                                           normalize_entity_tokens)


# --- the five cases named in M5-T01 -------------------------------------------
def test_spelled_letters_with_as_in():
    assert normalize_entity("j as in juliet o h n") == "john"


def test_phone_digits():
    assert normalize_entity("five five five one two three four") == "5551234"


def test_email_tokens():
    assert normalize_entity("john dot doe at example dot com") == "john.doe@example.com"


def test_dob_to_iso():
    assert normalize_entity("march third nineteen ninety") == "1990-03-03"


def test_dollar_amount():
    assert normalize_entity("forty two dollars and ten cents") == "42.10"


# --- spoken vs written agreement ----------------------------------------------
@pytest.mark.parametrize("spoken,written", [
    ("alpha bravo", "AB"),
    ("sierra mike india tango hotel", "Smith"),
    ("j-o-h-n", "John"),
    ("john dot doe at example dot com", "john.doe@example.com"),
    ("j dot smith underscore two at gmail dot com", "j.smith_2@gmail.com"),
    ("the third of march nineteen ninety", "1990-03-03"),
    ("march third nineteen ninety", "03/03/1990"),
    ("March 3, 1990", "1990-03-03"),
    ("nine four one oh five", "94105"),
    ("nine four one oh five dash one two three four", "94105-1234"),
    ("twenty dollars", "$20.00"),
    ("one thousand two hundred fifty dollars", "$1,250"),
    ("five five five oh one two three", "555-0123"),
    ("fifty cents", "$0.50"),
    ("account zero zero zero four two", "account 00042"),
])
def test_spoken_matches_written(spoken, written):
    assert normalize_entity(spoken) == normalize_entity(written)


def test_zip_cue_words_dropped():
    assert normalize_entity("zip code nine four one oh five") == "94105"


def test_nato_words_alone_are_not_letters():
    assert normalize_entity("golf club") == "golf club"
    assert normalize_entity("my name is mike") == "my name is mike"


def test_at_without_domain_is_not_an_email():
    assert normalize_entity("meet at noon") == "meet at noon"


def test_entity_tokens_and_empty():
    assert normalize_entity_tokens("") == []
    assert normalize_entity_tokens("order a b c one two three") == ["order", "abc", "123"]


def test_plain_normalize_unchanged():
    assert NORMALIZER_VERSION == "en-1"
    assert normalize("alpha bravo") == "alpha bravo"
    assert normalize("john dot doe at example dot com") == "john dot doe at example dot com"
