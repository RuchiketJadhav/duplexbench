"""Text normalization: plain-WER (ported) + entity mode (spec §4.3; BUILD_PLAN M5-T01).

Ported from asr-bench `src/asr_bench/metrics/normalize.py` (copied, not imported).
Everything down to `normalize()` is verbatim and keeps NORMALIZER_VERSION "en-1";
its tests (tests/unit/test_normalize.py) are the asr-bench tests unchanged.

Plain-WER text normalization, versioned.

    text ─► Whisper EnglishTextNormalizer      (case, punctuation, fillers, US spelling,
                                                number words -> digits, currency)
         ─► split alnum   "h2o" -> h 2 o       ┐ ported from dograh-experiments
         ─► merge digit runs "5 5 5" -> 555    │ benchmarks/tts-verbalization/canonicalize.py
         ─► collapse spelled letters u s a -> usa ┘ @ cd2ed09 [D10]

Changes from the source, per review [D22]:
  * non-Latin characters are kept, not deleted (the source regex removed them);
  * no stopword removal ("and" was dropped there; it is a real word for WER);
  * applied identically to reference and hypothesis.
Bump NORMALIZER_VERSION on any change: scores carry it and never mix versions.

Entity mode (new here, `normalize_entity`, ENTITY_NORMALIZER_VERSION): for
Track D read-backs and tool arguments, where spoken and written forms of IDs must
meet. Added on top of the plain pipeline, without changing it:

    text ─► written forms to spoken-safe forms  (emails, ISO / US numeric dates,
                                                 leading-zero digit groups "0123")
         ─► "j as in juliet" -> j ; NATO words in a spelling run -> letters
         ─► spelled-letter runs joined BEFORE Whisper ("o h n" would become "0 h n")
         ─► Whisper + split alnum (as plain)
         ─► dates -> ISO yyyy-mm-dd ; dollar amounts -> 42.10 ; ZIP+4 -> 94105-1234
         ─► merge digit runs, collapse letters (as plain)
         ─► email tokens: "john dot doe at example dot com" -> john.doe@example.com

Soft boundaries (fix-21). A comma, semicolon or period that is followed by
whitespace or ends the text ("12, 34", "0100.") is a soft boundary; one inside a
written token ("1,250", "0.5", "example.com") is not. The text is cut at soft
boundaries after the written-form rewrite and each piece goes through the
spelling pass and Whisper alone, so Whisper's number parser and the digit-run /
spelled-letter merges never join across a boundary: "12, 34" -> "12 34",
"555, 123, 4567" -> "555 123 4567", "a b c, 1 2 3" -> "abc 123". The token passes
after Whisper see all pieces with a BOUNDARY sentinel between them; only the date
pass steps over one ("March 3, 1990" -> 1990-03-03). Sentinels never reach the
output (`normalize_entity_pieces` keeps the pieces apart). Re-joining groups that
were read back separately is the matcher's job (`metrics/entities.contains_entity`),
because only the entity type says which groupings are a read-back.
"""
from __future__ import annotations

import re
from functools import lru_cache

NORMALIZER_VERSION = "en-1"
ENTITY_NORMALIZER_VERSION = "en-entity-3"   # 2: phone area code in parens, "0100." (M4-T02 bug); 3: comma boundary (fix-21)

_ORDINAL_DIGIT = re.compile(r"^\d+(st|nd|rd|th)$")
_MIXED = re.compile(r"^(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)[a-z0-9]+$")


@lru_cache(maxsize=1)
def _whisper():
    from whisper_normalizer.english import EnglishTextNormalizer
    return EnglishTextNormalizer()


def split_alnum(tok: str) -> list[str]:
    """'h2o' -> ['h','2','o']; ordinals ('20th') and long words ('covid19') kept."""
    if _ORDINAL_DIGIT.match(tok) or not _MIXED.match(tok):
        return [tok]
    out: list[str] = []
    for part in re.findall(r"\d+|[a-z]+", tok):
        if part.isalpha() and len(part) <= 4:
            out.extend(part)
        else:
            out.append(part)
    return out


def merge_digit_runs(toks: list[str]) -> list[str]:
    """'5 5 5 1 2' -> '55512': a phone number read digit by digit vs written."""
    out, run = [], []
    for t in toks:
        if t.isdigit():
            run.append(t)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(t)
    if run:
        out.append("".join(run))
    return out


def collapse_letters(toks: list[str]) -> list[str]:
    """'u s a' -> 'usa' so spelled initialisms match written ones."""
    out, run = [], []

    def close():
        if len(run) >= 2:
            out.append("".join(run))
        elif run:
            out.append(run[0])
        run.clear()
    for t in toks:
        if len(t) == 1 and t.isalpha() and t.isascii():
            run.append(t)
        else:
            close()
            out.append(t)
    close()
    return out


def normalize_tokens(text: str) -> list[str]:
    if not text:
        return []
    base = _whisper()(text)
    toks: list[str] = []
    for t in base.split():
        toks.extend(split_alnum(t))
    return collapse_letters(merge_digit_runs(toks))


def normalize(text: str) -> str:
    return " ".join(normalize_tokens(text))


# ============================ entity mode (new) ================================

NATO = {
    "alpha": "a", "alfa": "a", "bravo": "b", "charlie": "c", "delta": "d", "echo": "e",
    "foxtrot": "f", "golf": "g", "hotel": "h", "india": "i", "juliet": "j", "juliett": "j",
    "kilo": "k", "lima": "l", "mike": "m", "november": "n", "oscar": "o", "papa": "p",
    "quebec": "q", "romeo": "r", "sierra": "s", "tango": "t", "uniform": "u", "victor": "v",
    "whiskey": "w", "whisky": "w", "x-ray": "x", "xray": "x", "yankee": "y", "zulu": "z",
}
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]
_MONTH_NUM = {m: i + 1 for i, m in enumerate(_MONTHS)}
_MONTH_NUM.update({m[:3]: i + 1 for i, m in enumerate(_MONTHS)})
_MONTH_NUM["sept"] = 9
_EMAIL_SEP = {"dot": ".", "underscore": "_", "dash": "-", "hyphen": "-"}
_ZIP_CUES = {"zip", "zipcode", "postal", "postcode"}

_WRITTEN_EMAIL = re.compile(r"\b([a-z0-9._-]+)@([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_AS_IN = re.compile(r"\b([a-z])\s+(?:as\s+in|for)\s+[a-z-]+\b")
_HYPHEN_LETTERS = re.compile(r"\b[a-z](?:-[a-z])+\b")
# A trailing sentence period ("0100.") must not hide the group; a decimal ("0.5") must.
# A soft-boundary comma after the group ("0100, thanks") must not hide it either; "0,5" must.
_LEADING_ZERO_GROUP = re.compile(r"(?<![\w.,$])0\d*(?!\w|[.,]\d)")
# Whisper deletes anything in parentheses, so "(415) 555-0100" lost its area code.
_PAREN_DIGITS = re.compile(r"\(\s*(\d+)\s*\)")
# Dotted phone "415.555.0100" would otherwise read as a decimal.
_DOTTED_PHONE = re.compile(r"\b(\d{3})\.(\d{3})\.(\d{4})\b")
_DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_DAY = re.compile(r"^(\d{1,2})(st|nd|rd|th)?$")
_MONEY = re.compile(r"^\$(\d[\d,]*)(?:\.(\d{1,2}))?$")
_CENTS = re.compile(r"^¢(\d{1,2})$")          # Whisper writes "fifty cents" as "¢50"
# Soft boundary: , ; . followed by whitespace or the end of the text (fix-21).
_SOFT_BOUNDARY = re.compile(r"[,;.](?=\s|$)")
BOUNDARY = "|"        # sentinel token between pieces (Whisper never emits it: pieces are joined after Whisper)
_YEAR = re.compile(r"\d{4}")


def _ordinal(d: int) -> str:
    suf = "th" if 10 <= d % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return f"{d}{suf}"


def _spoken_email(m: re.Match) -> str:
    def say(part: str) -> str:
        part = part.replace(".", " dot ").replace("_", " underscore ").replace("-", " dash ")
        return " ".join(part.split())
    return f" {say(m.group(1))} at {say(m.group(2))} "


def _written_forms(t: str) -> str:
    """Rewrite written emails / numeric dates into forms Whisper leaves intact."""
    t = _WRITTEN_EMAIL.sub(_spoken_email, t)
    t = _PAREN_DIGITS.sub(r" \1 ", t)
    t = _DOTTED_PHONE.sub(r"\1 \2 \3", t)
    t = _ISO_DATE.sub(lambda m: f"{_MONTHS[int(m.group(2)) - 1]} {_ordinal(int(m.group(3)))} {m.group(1)}"
                      if 1 <= int(m.group(2)) <= 12 else m.group(0), t)
    t = _US_DATE.sub(lambda m: f"{_MONTHS[int(m.group(1)) - 1]} {_ordinal(int(m.group(2)))} {m.group(3)}"
                     if 1 <= int(m.group(1)) <= 12 else m.group(0), t)
    # Whisper parses "0123" as the number 123 and drops the leading zeros of
    # written IDs ("555-0123" -> "555 123"); spell such groups out as digit words.
    t = _LEADING_ZERO_GROUP.sub(lambda m: " " + " ".join(_DIGIT_WORDS[int(c)] for c in m.group(0)) + " ", t)
    return t


def _spelling_pass(t: str) -> list[str]:
    """'j as in juliet' -> j; hyphen-spelled letters split; NATO words that sit in
    a spelling run (next to another NATO word or a single letter) -> letters; then
    runs of >= 2 single letters joined into one word so Whisper cannot touch them."""
    t = _AS_IN.sub(r"\1", t)
    t = _HYPHEN_LETTERS.sub(lambda m: m.group(0).replace("-", " "), t)
    toks = t.split()

    def spell_like(i: int) -> bool:
        return 0 <= i < len(toks) and (toks[i] in NATO or (len(toks[i]) == 1 and toks[i].isalpha()))
    toks = [NATO[w] if w in NATO and (spell_like(i - 1) or spell_like(i + 1)) else w
            for i, w in enumerate(toks)]
    out, run = [], []
    for w in toks + [""]:
        if len(w) == 1 and w.isalpha() and w.isascii():
            run.append(w)
            continue
        out.append("".join(run)) if len(run) >= 2 else out.extend(run)
        run = []
        if w:
            out.append(w)
    # Runs of single written digits too (fix-21): Whisper turns a lone "1" into
    # "one", so "a b c, 1 2 3" lost its first digit; "123" passes intact.
    joined, drun = [], []
    for w in out + [""]:
        if len(w) == 1 and w.isdigit() and w.isascii():
            drun.append(w)
            continue
        joined.append("".join(drun)) if len(drun) >= 2 else joined.extend(drun)
        drun = []
        if w:
            joined.append(w)
    return joined


def _dates(toks: list[str]) -> list[str]:
    """'march 3rd 1990' / '3rd of march 1990' / 'march 3 1990' -> '1990-03-03'.
    A soft boundary may sit before the year ('March 3, 1990')."""
    def year_at(k: int) -> int | None:
        """Index of a 4-digit year at k, or at k + 1 after a BOUNDARY sentinel."""
        if k < len(toks) and toks[k] == BOUNDARY:
            k += 1
        return k if k < len(toks) and _YEAR.fullmatch(toks[k]) else None

    out, i = [], 0
    while i < len(toks):
        w = toks[i]
        # month day year
        y = year_at(i + 2) if w in _MONTH_NUM and i + 1 < len(toks) and _DAY.match(toks[i + 1]) else None
        if y is not None:
            d = int(_DAY.match(toks[i + 1]).group(1))
            if 1 <= d <= 31:
                out.append(f"{toks[y]}-{_MONTH_NUM[w]:02d}-{d:02d}")
                i = y + 1
                continue
        # day [of] month year
        m = _DAY.match(w)
        if m and m.group(2):
            j = i + 1 + (i + 1 < len(toks) and toks[i + 1] == "of")
            y = year_at(j + 1) if j < len(toks) and toks[j] in _MONTH_NUM else None
            if y is not None:
                d = int(m.group(1))
                if 1 <= d <= 31:
                    if out and out[-1] == "the":
                        out.pop()
                    out.append(f"{toks[y]}-{_MONTH_NUM[toks[j]]:02d}-{d:02d}")
                    i = y + 1
                    continue
        out.append(w)
        i += 1
    return out


def _money(toks: list[str]) -> list[str]:
    """'$42.10' -> '42.10'; '$1,250.00' -> '1250'; '$20' -> '20'; '¢50' -> '0.50'."""
    out = []
    for w in toks:
        c = _CENTS.match(w)
        if c:
            out.append(f"0.{int(c.group(1)):02d}")
            continue
        m = _MONEY.match(w)
        if m:
            whole = m.group(1).replace(",", "")
            cents = (m.group(2) or "").ljust(2, "0") if m.group(2) else ""
            out.append(f"{whole}.{cents}" if cents and cents != "00" else whole)
        else:
            out.append(w)
    return out


def _zip(toks: list[str]) -> list[str]:
    """ZIP+4: a 5-digit token followed by [dash] a 4-digit token -> '94105-1234'.
    The words 'zip code' / 'postal code' are dropped so only the value remains."""
    out, i = [], 0
    while i < len(toks):
        w = toks[i]
        if w in _ZIP_CUES:
            if i + 1 < len(toks) and toks[i + 1] == "code":
                i += 1
            i += 1
            continue
        if re.fullmatch(r"\d{5}", w):
            j = i + 1 + (i + 1 < len(toks) and toks[i + 1] in ("dash", "-"))
            if j < len(toks) and re.fullmatch(r"\d{4}", toks[j]):
                out.append(f"{w}-{toks[j]}")
                i = j + 1
                continue
        out.append(w)
        i += 1
    return out


def _emails(toks: list[str]) -> list[str]:
    """'john dot doe at example dot com' -> 'john.doe@example.com'. Needs a domain
    with at least one 'dot' after 'at'; the local part extends left over separators."""
    out = list(toks)
    i = 0
    while i < len(out):
        if out[i] == "at" and 0 < i and i + 3 < len(out) and out[i + 2] == "dot":
            # right: w (dot w)+
            j = i + 1
            dom = [out[j]]
            while j + 2 < len(out) and out[j + 1] in _EMAIL_SEP and out[j + 2] not in _EMAIL_SEP:
                dom += [_EMAIL_SEP[out[j + 1]], out[j + 2]]
                j += 2
            # left: (w sep)* w
            k = i - 1
            local = [out[k]]
            while k - 2 >= 0 and out[k - 1] in _EMAIL_SEP and out[k - 2] not in _EMAIL_SEP:
                local = [out[k - 2], _EMAIL_SEP[out[k - 1]]] + local
                k -= 2
            out[k:j + 1] = ["".join(local) + "@" + "".join(dom)]
            i = k + 1
            continue
        i += 1
    return out


def normalize_entity_pieces(text: str) -> list[list[str]]:
    """Entity-normalized tokens, one list per soft-boundary piece (module docstring)."""
    if not text:
        return []
    t = _written_forms(text.lower())
    toks: list[str] = []
    for seg in _SOFT_BOUNDARY.split(t):
        piece: list[str] = []
        for w in _whisper()(" ".join(_spelling_pass(seg))).split():
            piece.extend(split_alnum(w))
        if piece:
            if toks:
                toks.append(BOUNDARY)
            toks.extend(piece)
    toks = _zip(_money(_dates(toks)))
    toks = _emails(collapse_letters(merge_digit_runs(toks)))
    pieces: list[list[str]] = [[]]
    for w in toks:
        if w == BOUNDARY:
            pieces.append([])
        else:
            pieces[-1].append(w)
    return [p for p in pieces if p]


def normalize_entity_tokens(text: str) -> list[str]:
    return [w for p in normalize_entity_pieces(text) for w in p]


def normalize_entity(text: str) -> str:
    """Entity-mode normalization (spelled IDs, NATO, emails, ZIP, DOB, amounts)."""
    return " ".join(normalize_entity_tokens(text))
