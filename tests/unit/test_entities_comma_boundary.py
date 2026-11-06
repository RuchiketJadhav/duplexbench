"""fix-21: soft boundaries in the entity normalizer and the type-aware group matcher
(metrics/normalize.py + metrics/entities.contains_entity docstrings)."""
import pytest

from duplexbench.metrics.entities import allowed_cuts, contains_entity
from duplexbench.metrics.normalize import normalize, normalize_entity, normalize_entity_pieces
from duplexbench.refasr.floor_calibration import correct


def has(text, value, etype):
    return contains_entity(normalize_entity(text), value, etype)


# --- the normalizer no longer merges across a soft boundary ---------------------
@pytest.mark.parametrize("text,expected", [
    ("your order is 12, 34 items", "your order is 12 34 items"),
    ("twelve, thirty four items", "12 34 items"),
    ("one two; three four", "12 34"),
    ("555, 123, 4567", "555 123 4567"),
    ("nineteen eighty eight, forty two dollars", "1988 42"),
    ("zip nine four one zero five, forty two dollars and ten cents", "94105 42.10"),
])
def test_no_merge_across_soft_boundary(text, expected):
    assert normalize_entity(text) == expected


def test_written_separators_inside_tokens_are_not_boundaries():
    assert normalize_entity("$1,250.00") == "1250"
    assert normalize_entity("$0.50") == "0.50"
    assert normalize_entity("dana.ruiz@example.com") == "dana.ruiz@example.com"


def test_date_still_spans_its_comma():
    assert normalize_entity("March 3, 1990") == "1990-03-03"
    assert normalize_entity("the third of march, nineteen ninety") == "1990-03-03"


def test_pieces_and_leading_zero_group_before_comma():
    assert normalize_entity_pieces("415, 555, 0100, thanks") == [["415"], ["555"], ["0100"], ["thanks"]]


def test_plain_normalize_untouched():
    assert normalize("your order is 12, 34 items") == "your order is 1234 items"


# --- the five fixtures named in fix-21 -------------------------------------------
@pytest.mark.parametrize("text", [
    "555, 123, 4567",
    "five five five, one two three, four five six seven",
    "your number is 555, 123, 4567.",
])
def test_phone_in_3_3_4_comma_groups_matches(text):
    assert has(text, "5551234567", "phone")
    assert has(text, "(555) 123-4567", "phone")


@pytest.mark.parametrize("text,value,etype", [
    ("your order is 12, 34 items", "1234", "confirmation_code"),
    ("your order is 12, 34 items", "$1,234", "amount"),
    ("your order is 12, 34 items", "1234", "id"),
    ("room 941, 05 guests", "94105", "zip"),
    ("call 55, 51234567", "5551234567", "phone"),       # cut at 2
    ("4155, 550100", "4155550100", "phone"),            # cut at 4
    ("call 555, then extension 1234", "5551234", "phone"),
])
def test_separate_numbers_do_not_join(text, value, etype):
    assert not has(text, value, etype)


def test_zip_then_amount_neither_merges():
    for text in ("zip nine four one zero five, forty two dollars and ten cents",
                 "94105, $42.10"):
        assert has(text, "94105", "zip")
        assert has(text, "$42.10", "amount")
        assert not has(text, "9410542.10", "amount")
        assert not has(text, "9410542", "zip")


@pytest.mark.parametrize("text", [
    "A B C, 1 2 3",
    "alpha bravo charlie, one two three",
    "your code is alpha bravo charlie, one two three, thanks",
])
def test_spelled_id_nato_and_comma_groups(text):
    assert has(text, "ABC123", "confirmation_code")
    assert has(text, "ABC123", "id")
    assert not has(text, "ABC1234", "confirmation_code")


def test_phone_cut_at_allowed_subset_matches():
    assert has("555, 1234567", "5551234567", "phone")       # 3-7: every cut is a NANP cut


def test_id_groups_declared_by_written_separators():
    assert has("booking 4471, 2290", "4471-2290", "confirmation_code")
    assert not has("booking 44, 71, 22, 90", "4471-2290", "confirmation_code")


def test_email_with_digits_unchanged():
    said = "that is j dot smith underscore two at gmail dot com, correct"
    assert normalize_entity(said) == "that is j.smith_2@gmail.com correct"
    assert has(said, "j.smith_2@gmail.com", "email")
    assert not has(said, "j.smith_3@gmail.com", "email")


def test_zip_plus_four_in_two_groups():
    assert has("nine four one zero five, one two three four", "94105-1234", "zip")
    assert has("94105-1234", "94105-1234", "zip")


def test_allowed_cuts_table():
    assert allowed_cuts("phone", "5551234567", "5551234567") == {3, 6}
    assert allowed_cuts("phone", "5551234", "5551234") == {3}
    assert allowed_cuts("zip", "94105", "94105") == frozenset()
    assert allowed_cuts("amount", "$1,234", "1234") == frozenset()
    assert allowed_cuts("confirmation_code", "AB12X", "ab12x") == {2, 4}
    assert allowed_cuts(None, "5551234567", "5551234567") == frozenset()


def test_untyped_match_is_exact_only():
    assert not has("555, 123, 4567", "5551234567", None)
    assert has("5551234567", "5551234567", None)


def test_floor_calibration_correct_uses_type():
    assert correct("555-123-4567", "555, 123, 4567", "phone")
    assert not correct("555-123-4567", "555, 123, 4567")
    assert not correct("94105", "941, 05", "zip")
