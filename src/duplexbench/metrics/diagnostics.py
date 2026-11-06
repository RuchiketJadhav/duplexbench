"""TOR (take-over rate) diagnostic with Full-Duplex-Bench v1's exact rule (spec §4.5; BUILD_PLAN M2-T09).

Reported for cross-paper comparison only, never headline; every observation is
tagged `diagnostic`. FDB is CC-BY-NC-4.0 (docs/licenses.md): the rule below is
re-implemented from reading, not copied. Source (reference/Full-Duplex-Bench @ 3e799c4,
quoted in docs/reference-notes.md §1):

    v1_v1.5/evaluation/eval_pause_handling.py:7    turn_duration_threshold = 1
    v1_v1.5/evaluation/eval_pause_handling.py:8    turn_num_words_threshold = 3
    v1_v1.5/evaluation/eval_pause_handling.py:42-44
        # if no transcription from CrisperWhisper, means model does not take turn
        if len(segments_cw) == 0:  TOR = 0
    v1_v1.5/evaluation/eval_pause_handling.py:46-53
        duration = last_word.end (or last_word.start when end is None) - first_word.start
    v1_v1.5/evaluation/eval_pause_handling.py:54-60
        if duration < turn_duration_threshold:
            if len(segments_cw) <= turn_num_words_threshold: TOR = 0
            else: TOR = 1
        else: TOR = 1
    (identical in eval_smooth_turn_taking.py:7-8,63-73 and eval_user_interruption.py:7-8,100-112)

    Rule: no words -> 0; duration < 1 s AND words <= 3 -> 0; otherwise -> 1.

The spec's paraphrase ("a response under 1 s and fewer than two words is not a
takeover") does not match the code's `<= 3` words for these three tasks; the code
wins (CLAUDE.md §3: "the exact rule from the code, not the paper"). The paraphrase
is closer to FDB's separate backchannel rule, kept as `takeover_backchannel`:

    v1_v1.5/evaluation/eval_backchannel.py:14      time_threshold = 3
    v1_v1.5/evaluation/eval_backchannel.py:63-65   if duration > time_threshold: TOR = 1
    v1_v1.5/evaluation/eval_backchannel.py:101-111 if num_words > 3: TOR = 1
        else: if duration < 1: if len(curr_text) <= 2: TOR = 0 else: TOR = 1  else: TOR = 1

The constants are FDB's [Fact] values, held here as module constants (in seconds,
as FDB writes them) and overridable per call; they are not Thresholds fields
because they are not ours to tune.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from duplexbench.metrics.base import Observation
from duplexbench.metrics.names import Metric

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord
    from duplexbench.thresholds import Thresholds

# FDB eval_pause_handling.py:7-8 (also eval_smooth_turn_taking.py, eval_user_interruption.py)
FDB_TURN_DURATION_S = 1
FDB_TURN_NUM_WORDS = 3
# FDB eval_backchannel.py:14, :101-108
FDB_BC_TIME_S = 3
FDB_BC_MAX_WORDS = 3
FDB_BC_SHORT_S = 1
FDB_BC_SHORT_WORDS = 2

# Overlay classes whose FDB counterpart task uses the main rule: pause handling
# (P-PAUSE), smooth turn taking (E-END), user interruption (Y-INT, Y-CMD).
TOR_CLASSES: frozenset[str] = frozenset({"P-PAUSE", "E-END", "Y-INT", "Y-CMD"})


@dataclass(frozen=True)
class Word:
    """One time-aligned word of the agent's reference-ASR output (ms on the call clock)."""
    text: str
    start_ms: float
    end_ms: float | None = None


def _duration_s(words: Sequence[Word]) -> float:
    last = words[-1]
    end = last.end_ms if last.end_ms is not None else last.start_ms
    return (end - words[0].start_ms) / 1e3


def takeover(words: Sequence[Word], *, max_duration_s: float = FDB_TURN_DURATION_S,
             max_words: int = FDB_TURN_NUM_WORDS) -> bool:
    """FDB v1 TOR for one sample (eval_pause_handling.py:42-60). True = the agent took the turn."""
    if len(words) == 0:
        return False
    if _duration_s(words) < max_duration_s:
        return len(words) > max_words
    return True


def takeover_backchannel(segments: Sequence[tuple[float, float, Sequence[str]]]) -> bool:
    """FDB backchannel-task TOR (eval_backchannel.py:55-111) over VAD segments.

    `segments`: `(start_ms, end_ms, words_in_segment)` in time order. The first
    segment that is a takeover decides; FDB keeps the last segment's verdict when
    none breaks out, so a final short segment decides otherwise.
    """
    tor = False
    for start, end, words in segments:
        dur = (end - start) / 1e3
        if dur > FDB_BC_TIME_S:
            return True
        n = len(words)
        if n > FDB_BC_MAX_WORDS:
            tor = True
        elif dur < FDB_BC_SHORT_S:
            tor = n > FDB_BC_SHORT_WORDS
        else:
            tor = True
    return tor


def observe(rec: "CallRecord", th: "Thresholds",
            words_by_event: Mapping[str, Sequence[Word]] | None = None) -> list[Observation]:
    """One `tor` observation per TOR_CLASSES overlay event of the call.

    `words_by_event`: overlay_id -> the agent's reference-ASR words after the event
    (reference ASR arrives in M4). Without them the observation is invalid with tag
    `no_refasr`. Every row carries `diagnostic` and `cls:<class>`. `th` is unused
    (FDB's constants are not ours) but kept for the `observe(rec, th)` contract.
    """
    del th
    out: list[Observation] = []
    for row in rec.overlays:
        if row.cls not in TOR_CLASSES:
            continue
        base = dict(metric=str(Metric.tor), call_id=rec.call_id, scenario_id=rec.scenario.id,
                    trial=rec.trial, condition=rec.condition, system_id=rec.system_id,
                    event_id=row.overlay_id)
        tags = ("diagnostic", f"cls:{row.cls}")
        if "not_injected" in row.tags:
            out.append(Observation(**base, value=None, valid=False, tags=tags + ("not_injected",)))
            continue
        if words_by_event is None or row.overlay_id not in words_by_event:
            out.append(Observation(**base, value=None, valid=False, tags=tags + ("no_refasr",)))
            continue
        v = 1.0 if takeover(words_by_event[row.overlay_id]) else 0.0
        out.append(Observation(**base, value=v, valid=True, tags=tags))
    return out
