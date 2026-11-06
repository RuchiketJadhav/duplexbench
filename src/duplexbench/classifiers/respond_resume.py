"""Respond / Resume / Uncertain classifier (spec §4.1 row "Respond / Resume / Uncertain"; BUILD_PLAN M2-T10).

    utterance that defines T_respond_ear
        ├── contains >= 1 response anchor (after the normalizer)      -> respond
        ├── content-word Jaccard with the utterance in progress at
        │   overlay onset >= th.respond_jaccard, and no anchor          -> resume
        └── otherwise (incl. empty text)                                -> uncertain

Rule-based; it stays marked `classifier` until validated on 200 double-labeled
items at kappa >= th.kappa_min (spec §4.1, §8), so every Observation it produces
carries the `classifier` tag. Text goes through `metrics.normalize.normalize_tokens`
(the versioned normalizer) on both sides.

Content words: the normalized tokens minus a closed English function-word list
(`FUNCTION_WORDS`). The spec says "content-word" without a list; this one is the
smallest list that removes articles, pronouns, auxiliaries, prepositions and
conjunctions. TODO(language pack): move it to configs/langpacks/en-US once
`stimuli/langpack.py` carries a stopword list, and version it there.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from duplexbench.metrics.base import Observation
from duplexbench.metrics.names import Metric
from duplexbench.metrics.normalize import normalize_tokens

if TYPE_CHECKING:
    from duplexbench.thresholds import Thresholds

Label = Literal["respond", "resume", "uncertain"]

FUNCTION_WORDS: frozenset[str] = frozenset("""
a an the this that these those
i me my mine you your yours he him his she her hers it its we us our ours they them their theirs
am is are was were be been being do does did have has had will would shall should can could may might must
to of in on at by for with from up down out over into onto about as than then so if or and but nor not no
there here what which who whom whose when where why how all any some just also very too
""".split())


def content_words(text: str | None) -> set[str]:
    return {t for t in normalize_tokens(text or "") if t not in FUNCTION_WORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _contains(tokens: list[str], phrase: list[str]) -> bool:
    k = len(phrase)
    return k > 0 and any(tokens[i:i + k] == phrase for i in range(len(tokens) - k + 1))


def label(utt_text: str | None, anchors: Sequence[str], prior_utt_text: str | None,
          th: "Thresholds") -> Label:
    """Label one agent utterance (spec §4.1). Anchors may be multi-word phrases."""
    toks = normalize_tokens(utt_text or "")
    if not toks:
        return "uncertain"
    if any(_contains(toks, normalize_tokens(a)) for a in anchors):
        return "respond"
    if jaccard(content_words(utt_text), content_words(prior_utt_text)) >= th.respond_jaccard:
        return "resume"
    return "uncertain"


def observation(*, call_id: str, scenario_id: str, trial: int, condition: str, system_id: str,
                event_id: str, utt_text: str | None, anchors: Sequence[str],
                prior_utt_text: str | None, th: "Thresholds") -> Observation:
    """A `respond_resume` Observation: value 1.0 when Respond, else 0.0; tags `classifier`, `label=<l>`.

    Uncertain stays in the denominator (it is a reported share, spec §4.1), so the
    value is the Respond share and the label tag lets the report split all three.
    """
    lab = label(utt_text, anchors, prior_utt_text, th)
    return Observation(metric=str(Metric.respond_resume), call_id=call_id, scenario_id=scenario_id,
                       trial=trial, condition=condition, system_id=system_id, event_id=event_id,
                       value=1.0 if lab == "respond" else 0.0, valid=True,
                       tags=("classifier", f"label={lab}"))
