"""Regression tests for the entity-normalizer phone bugs found in M4-T02 (worklog):
Whisper deletes parenthesised text (the area code) and a trailing period hid the
leading-zero group ("0100." -> "100")."""
import pytest

from duplexbench.metrics.normalize import ENTITY_NORMALIZER_VERSION, normalize_entity


def test_paren_area_code_kept():
    assert normalize_entity("(415) 555-0100") == "4155550100"


def test_trailing_period_keeps_zeros():
    assert normalize_entity("415 555 0100.") == "4155550100"


@pytest.mark.parametrize("written", ["(415) 555-0100", "415-555-0100", "415.555.0100",
                                     "415 555 0100.", "(415)555-0100"])
def test_written_phone_forms_agree_with_spoken(written):
    spoken = "four one five five five five zero one zero zero"
    assert normalize_entity(written) == normalize_entity(spoken) == "4155550100"


def test_canonicalizer_case_from_m4_t02():
    assert normalize_entity("(555) 123-4567") == normalize_entity("5551234567")


def test_decimals_still_decimals():
    assert normalize_entity("$0.50") == normalize_entity("fifty cents")


def test_version_bumped():
    assert ENTITY_NORMALIZER_VERSION == "en-entity-3"
