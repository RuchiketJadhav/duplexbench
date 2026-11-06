"""Recovery-request classifier (BUILD_PLAN M5-T03; spec §4.3 recovery request rate / success).

Rule-based: an agent utterance is a recovery request when any langpack
`recovery_patterns` regex (public set, plus the private held-out extension when
the pack was loaded with one) matches it, case-insensitively:

    classify("Sorry, could you spell that for me?", lp)  -> True
    classify("Thanks!", lp)                              -> False

Every metric built from it carries the tag `classifier` until validated (spec §8).
`observation(...)` builds that tagged `Observation`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from duplexbench.metrics.base import Observation
from duplexbench.metrics.names import Metric

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord
    from duplexbench.stimuli.langpack import LangPack

TAG = "classifier"
DEFAULT_LANGPACK = Path(__file__).resolve().parents[3] / "configs" / "langpacks" / "en-US"


@lru_cache(maxsize=64)
def _rx(pattern) -> "re.Pattern[str]":
    return pattern.compiled()


def _compiled(lp: "LangPack"):
    return tuple(_rx(p) for p in lp.recovery_patterns)


def default_langpack() -> "LangPack":
    from duplexbench.stimuli.langpack import LangPack
    return LangPack.load(DEFAULT_LANGPACK)


def matches(agent_text: str, langpack: "LangPack") -> list[str]:
    """Ids of the recovery patterns that match `agent_text`."""
    return [p.id for p, rx in zip(langpack.recovery_patterns, _compiled(langpack))
            if rx.search(agent_text or "")]


def classify(agent_text: str, langpack: "LangPack") -> bool:
    """True when the agent asks the caller to repeat, spell, confirm or clarify."""
    return any(rx.search(agent_text or "") for rx in _compiled(langpack))


def observation(rec: "CallRecord", metric: Metric | str, value: float | None, *,
                event_id: str | None = None, valid: bool = True,
                tags: tuple[str, ...] = ()) -> Observation:
    """Observation for a recovery metric, always tagged `classifier`."""
    m = Metric(metric)
    if m not in (Metric.recovery_request_rate, Metric.recovery_success):
        raise ValueError(f"not a recovery metric: {m}")
    return Observation(metric=m.value, call_id=rec.call_id, scenario_id=rec.scenario.id,
                       trial=rec.trial, condition=rec.condition, system_id=rec.system_id,
                       event_id=event_id, value=value, valid=valid,
                       tags=tuple(dict.fromkeys((TAG, *tags))))
