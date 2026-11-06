from duplexbench.metrics.normalize import (collapse_letters, merge_digit_runs, normalize,
                                          normalize_tokens, split_alnum)


def test_ported_collapse_letters():
    assert collapse_letters(["the", "u", "s", "a", "is"]) == ["the", "usa", "is"]
    assert collapse_letters(["a", "dog"]) == ["a", "dog"]


def test_ported_merge_digit_runs():
    assert merge_digit_runs(["call", "5", "5", "5", "now"]) == ["call", "555", "now"]


def test_ported_split_alnum():
    assert split_alnum("h2o") == ["h", "2", "o"]
    assert split_alnum("20th") == ["20th"]
    assert split_alnum("covid19") == ["covid", "19"]


def test_phone_spoken_vs_written_match():
    assert normalize("five five five, one two three four") == normalize("555-1234")


def test_and_is_kept_not_a_stopword():
    # [D22] the source canonicalizer dropped "and"; for WER it is a real word
    assert "and" in normalize_tokens("salt and pepper")


def test_non_latin_kept_not_deleted():
    # [D22] the source regex deleted Devanagari outright
    toks = normalize_tokens("मेरा नाम Ram")
    assert "ram" in toks and len(toks) >= 2


def test_symmetry_fillers_and_spelling():
    assert normalize("Um, the colour is grey") == normalize("the color is gray")
