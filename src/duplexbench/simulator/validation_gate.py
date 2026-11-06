"""Simulator validation gate and whole-trial regeneration (BUILD_PLAN M4-T11, §1 row 10; spec §7).

After a trial, on the caller's OWN text (never the agent's):

    check(trial_text_turns, audio_ok, timed_out, goal_entities) -> reasons
      "goal_entity_missing:<entity>"   an entity not stated within the first two caller turns
      "broken_audio"                   caller TTS audio broken (e.g. all-silence)
      "harness_timeout"                the harness timed the trial out

A failing trial is rerun as a whole new call (`run_trial(attempt)` with
attempt = 0, 1, ...), up to `th.sim_regen_max` regenerations. Every attempt is
kept and costed: each result carries `regenerated = attempt` and its gate
reasons; only the last attempt is `scored`; if the last attempt still fails,
its outcome becomes `validation_failed`.

`regeneration_rate(trials)` = share of trials that needed at least one
regeneration; `review_list(rates, th)` names scenarios above
`th.sim_regen_review_rate`, which a human reviews before any paid run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Sequence

from duplexbench.metrics.normalize import normalize_entity
from duplexbench.thresholds import Thresholds

GOAL_TURNS = 2                       # spec §7: "within its first two turns" (fixed, not [Proposed])
VALIDATION_FAILED = "validation_failed"
BROKEN_AUDIO = "broken_audio"
HARNESS_TIMEOUT = "harness_timeout"
GOAL_MISSING = "goal_entity_missing"


@dataclass
class TrialResult:
    call_id: str
    caller_text_turns: list[str]
    audio_ok: bool
    timed_out: bool
    goal_entities: list[str]
    outcome: str = "completed"
    usd_actual: float = 0.0
    flags: dict = field(default_factory=dict)
    regenerated: int = 0
    reasons: list[str] = field(default_factory=list)
    scored: bool = False

    @property
    def passed(self) -> bool:
        return not self.reasons


def _contains(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def entity_stated(entity: str, text: str) -> bool:
    """Is `entity` stated in `text`?

    1. normalized token-sequence match (`normalize_entity` on both sides), so
       'four one five ...' matches '415...' and 'march third nineteen ninety'
       matches 'March 3 1990';
    2. the same with separators squashed ('ana.ruiz' vs 'ana ruiz' spellings);
    3. for entities with digits: the entity's digit string inside the digits of
       the raw text or of the normalized text. This covers written numbers that
       `normalize_entity` mangles ('415 555 0100.' -> '415555100', '(415) 555-0100'
       -> '5550100'; reported, normalize.py is not changed here).
    """
    ent = normalize_entity(entity).split()
    norm = normalize_entity(text)
    hay = norm.split()
    if _contains(hay, ent):
        return True
    squash = lambda toks: re.sub(r"[^0-9a-z@.]", "", "".join(toks))   # noqa: E731
    if ent and squash(ent) in squash(hay):
        return True
    d = _digits(entity)
    return bool(d) and (d in _digits(text) or d in _digits(norm))


def check(trial_text_turns: Sequence[str], audio_ok: bool, timed_out: bool,
          goal_entities: Sequence[str]) -> list[str]:
    early = " ".join(trial_text_turns[:GOAL_TURNS])
    reasons = [f"{GOAL_MISSING}:{e}" for e in goal_entities if not entity_stated(e, early)]
    if not audio_ok:
        reasons.append(BROKEN_AUDIO)
    if timed_out:
        reasons.append(HARNESS_TIMEOUT)
    return reasons


async def run_with_regeneration(run_trial: Callable[[int], Awaitable[TrialResult]],
                                th: Thresholds) -> list[TrialResult]:
    attempts: list[TrialResult] = []
    for attempt in range(th.sim_regen_max + 1):
        r = await run_trial(attempt)
        r.regenerated = attempt
        if attempt:
            r.flags = {**r.flags, "regenerated": attempt}
        r.reasons = check(r.caller_text_turns, r.audio_ok, r.timed_out, r.goal_entities)
        attempts.append(r)
        if r.passed:
            break
    last = attempts[-1]
    last.scored = True
    if not last.passed:
        last.outcome = VALIDATION_FAILED
        last.flags = {**last.flags, VALIDATION_FAILED: True}
    return attempts


def regeneration_rate(trials: Sequence[Sequence[TrialResult]]) -> float:
    """Share of trials (each a list of attempts) that needed >= 1 regeneration."""
    if not trials:
        return 0.0
    return sum(1 for t in trials if len(t) > 1) / len(trials)


def review_list(rates: Mapping[str, float], th: Thresholds) -> list[str]:
    """Scenario ids whose regeneration rate exceeds th.sim_regen_review_rate."""
    return sorted(s for s, r in rates.items() if r > th.sim_regen_review_rate)
