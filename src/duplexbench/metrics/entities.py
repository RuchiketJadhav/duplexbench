"""Track D entity metrics on the reference-ASR transcript (BUILD_PLAN M5-T02; spec §4.3).

    score(rec, scenario, refasr_words, floors, th) -> list[Observation]

For each scenario entity of a scored type (phone, email, zip, dob, name, amount,
confirmation_code), the entity is *correct* when its `normalize_entity` form
appears, on token boundaries, in `normalize_entity` of the agent's reference-ASR
transcript (`refasr_words`, ear channel). One `readback_error_rate` Observation
per entity: 0.0 correct, 1.0 not found; tagged `type:<t>` and `domain:<d>`.

Floors: `floors` maps type -> reference-ASR accuracy on known-text read-backs
through the same channel (`refasr/floor_calibration.floors`). When a type's
floor is below the minimum (`th.entity_floor_min` if Thresholds has the field,
else the `floor_min` argument), the ASR cannot be trusted for that type: its
observations are `valid=False`, value None, tag `below_floor`. With no minimum
available nothing is invalidated. A type with no measured floor is tagged
`no_floor`. Entities of other types are skipped.

Matching rule (`contains_entity`, fix-21). `normalize_entity` never merges
numbers or letter runs across a soft boundary (a , ; . followed by a space), so
a read-back in groups ("555, 123, 4567") arrives as separate tokens. An entity
matches when (1) its normalized tokens occur as a whole-token sequence in the
text, or (2) its type declares a group read-back and a run of consecutive whole
tokens concatenates to the entity's alphanumeric key, with every cut between
those tokens at an allowed group position:

    phone              7 digits {3}; 10 digits {3, 6}; 11 digits {1, 4, 7}   (US NANP 3-4, 3-3-4, 1-3-3-4)
    zip                9 digits (ZIP+4) {5}; 5 digits: none
    confirmation_code  the cuts the written value itself declares: its separators
    / id               ("4471-2290" -> {4}) and letter/digit changes ("ABC123" -> {3})
    every other type   none (exact whole-token match only; amounts and dates are
                       never read in comma groups, the normalizer joins dates itself)

A match never starts or ends inside a token. So "12, 34" is not "1234" for any
type (no type allows a cut at 2 of 4), "941, 05" is not ZIP 94105, while
"555, 123, 4567" is phone 5551234567 and "A B C, 1 2 3" is ID ABC123. The group
positions are the US formats of spec §4.3 (format facts, not tunable thresholds).
With no type given only rule (1) applies.

Entity step (fix-32): an entity with `step: <caller step id>` counts only agent words whose
start_ms >= the end of that caller step on the call clock (the first `caller_turn_end`
event with that step_id; for simulated calls without such events, metrics/task
caller_turn_ends over sim_turns). A step that never ended (the caller never said the
value) gives an invalid observation tagged `step_not_reached`. Without `step` the whole
transcript counts, as before.

Not here yet (BUILD_PLAN M5-T02 full list): capture_first / capture_final from
the tool log, CER for IDs and phones, codec_penalty (paired, in stats/).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from duplexbench.metrics.base import Observation
from duplexbench.metrics.names import Metric
from duplexbench.metrics.normalize import normalize_entity

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord
    from duplexbench.refasr.engine import Word
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.thresholds import Thresholds

ENTITY_TYPES = ("phone", "email", "zip", "dob", "name", "amount", "confirmation_code")
BELOW_FLOOR = "below_floor"
NO_FLOOR = "no_floor"
STEP_NOT_REACHED = "step_not_reached"
_NS_PER_MS = 1_000_000


def transcript(words: Iterable["Word"]) -> str:
    return " ".join(w.text for w in words)


# Allowed cut positions for a group read-back, by type and key length (US formats, spec §4.3).
_FIXED_CUTS: dict[str, dict[int, frozenset[int]]] = {
    "phone": {7: frozenset({3}), 10: frozenset({3, 6}), 11: frozenset({1, 4, 7})},
    "zip": {9: frozenset({5})},
}
_DECLARED_CUT_TYPES = frozenset({"confirmation_code", "id"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ALNUM_RUN = re.compile(r"[a-z]+|\d+")


def _key(norm_value: str) -> str:
    return _NON_ALNUM.sub("", norm_value)


def _declared_cuts(value: str) -> frozenset[int]:
    """Cuts the written value declares: separators and letter/digit changes."""
    cuts, pos = set(), 0
    for chunk in _NON_ALNUM.split(str(value).lower()):
        for run in _ALNUM_RUN.findall(chunk):
            if pos:
                cuts.add(pos)
            pos += len(run)
    return frozenset(cuts)


def allowed_cuts(etype: str | None, value: Any, key: str) -> frozenset[int]:
    """Positions in `key` where a group read-back of this entity may be cut."""
    if etype in _FIXED_CUTS:
        return _FIXED_CUTS[etype].get(len(key), frozenset()) if key.isdigit() else frozenset()
    if etype in _DECLARED_CUT_TYPES:
        return _declared_cuts(str(value))
    return frozenset()


def _grouped(tokens: list[str], key: str, cuts: frozenset[int]) -> bool:
    """A run of >= 2 consecutive whole tokens concatenates to `key`, cut only at `cuts`."""
    for i in range(len(tokens)):
        acc = ""
        for j in range(i, len(tokens)):
            if acc and len(acc) not in cuts:
                break
            acc += tokens[j]
            if not key.startswith(acc):
                break
            if acc == key:
                return j > i
    return False


def contains_entity(norm_text: str, value: Any, etype: str | None = None) -> bool:
    """`value` is read back in already-normalized text (rule in the module docstring)."""
    ent = normalize_entity(str(value))
    if not ent:
        return False
    if f" {ent} " in f" {norm_text} ":
        return True
    key = _key(ent)
    cuts = allowed_cuts(etype, value, key)
    return bool(cuts) and _grouped(norm_text.split(), key, cuts)


def floor_min_of(th: "Thresholds", floor_min: float | None = None) -> float | None:
    v = getattr(th, "entity_floor_min", None)
    return v if v is not None else floor_min


def step_end_ms(rec: "CallRecord", scenario: "Scenario", step: str) -> float | None:
    """End of caller step `step` on the call clock (ms), or None when it never ended."""
    for e in sorted(rec.events, key=lambda e: (e.t_ns, e.seq)):
        if e.kind == "caller_turn_end" and str(e.payload.get("step_id", "")) == str(step):
            return e.t_ns / _NS_PER_MS
    if rec.sim_turns:
        from duplexbench.metrics.task import caller_turn_ends
        for sid, end_ns in caller_turn_ends(rec, scenario):
            if sid == step:
                return end_ns / _NS_PER_MS
    return None


def score(rec: "CallRecord", scenario: "Scenario", refasr_words: list["Word"],
          floors: Mapping[str, float] | None, th: "Thresholds", *,
          floor_min: float | None = None) -> list[Observation]:
    words = sorted(refasr_words, key=lambda w: (w.start_ms, w.end_ms))
    norm = normalize_entity(transcript(words))
    by_step: dict[str, str | None] = {}

    def norm_after(step: str) -> str | None:
        if step not in by_step:
            end = step_end_ms(rec, scenario, step)
            by_step[step] = None if end is None else normalize_entity(
                transcript(w for w in words if w.start_ms >= end))
        return by_step[step]
    fmin = floor_min_of(th, floor_min)
    floors = floors or {}
    out: list[Observation] = []
    for ent in scenario.entities:
        t = ent.get("type")
        if t not in ENTITY_TYPES:
            continue
        tags = [f"type:{t}", f"domain:{scenario.domain}"]
        fl = floors.get(t)
        if fl is None:
            tags.append(NO_FLOOR)
        below = fl is not None and fmin is not None and fl < fmin
        step = ent.get("step")
        text = norm if step is None else norm_after(str(step))
        if below:
            tags.append(BELOW_FLOOR)
            value, valid = None, False
        elif text is None:
            tags.append(STEP_NOT_REACHED)
            value, valid = None, False
        else:
            value, valid = (0.0 if contains_entity(text, ent.get("value", ""), t) else 1.0), True
        out.append(Observation(metric=Metric.readback_error_rate.value, call_id=rec.call_id,
                               scenario_id=scenario.id, trial=rec.trial, condition=rec.condition,
                               system_id=rec.system_id, event_id=ent.get("id"), value=value,
                               valid=valid, tags=tuple(tags)))
    return out


def observe(rec: "CallRecord", th: "Thresholds", refasr_words: list["Word"] | None = None,
            floors: Mapping[str, float] | None = None) -> list[Observation]:
    """Module-contract entry point (metrics/base.py)."""
    return score(rec, rec.scenario, refasr_words or [], floors, th)
