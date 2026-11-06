"""Cost projection and actual cost per call (spec §4.4 "Cost per connected minute", §9.3; BUILD_PLAN M3-T01).

    usd_per_min(prices)                         projected USD per connected minute
    project_call_usd(minutes, prices)           one call (None prices = free system -> 0.0)
    project(scenarios, system, prices, minutes) a whole run's calls -> USD
    price_per_call(usage, prices)               actual USD from provider usage, None if unpriceable
    est_call_minutes(scenario, eend_silence_ms) script length estimate for a scripted caller

Pricing inputs come only from the price file (`configs/prices/<ref>.yaml`), never
from thresholds: `rates` are [Fact] (with `source_url` and `checked`), `projection`
inputs are labelled [Assumption] in that file. Actual cost uses `usage` events
(OpenAI `response.done.response.usage`): audio/text input tokens (cached part at
the cached rate) and audio/text output tokens. Anything the price file cannot
price returns None, and `cli run` then stops before the next call (M3-T01).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_PER_M = 1_000_000.0          # prices are per 1M tokens
_S_PER_MIN = 60.0             # threshold-ok: unit conversion s per min
_MS_PER_S = 1e3               # threshold-ok: unit conversion
_WORDS_PER_MIN = 150.0        # threshold-ok: speech-rate fallback when a step has no clip timing


def usd_per_min(prices: Mapping[str, Any] | None) -> float:
    """Projected USD per connected minute; 0.0 for an unpriced (free) system."""
    if prices is None:
        return 0.0
    if "usd_per_min" in prices:                      # per-minute providers (Grok, GPT-Live)
        return float(prices["usd_per_min"])
    r, p = prices["rates"], prices["projection"]
    audio_in = p["audio_in_tokens_per_min"] * r["audio_in"] * p.get("context_rebill_factor", 1.0)
    audio_out = p["agent_talk_share"] * p["audio_out_tokens_per_min"] * r["audio_out"]
    return (audio_in + audio_out) / _PER_M


def default_minutes(prices: Mapping[str, Any] | None, track: str) -> float:
    if prices is None:
        return 0.0
    return float((prices.get("projection") or {}).get("default_call_minutes", {}).get(track, 1.0))


def project_call_usd(minutes: float, prices: Mapping[str, Any] | None) -> float:
    return float(minutes) * usd_per_min(prices)


def est_call_minutes(scenario: Any, eend_silence_ms: int) -> float:
    """Length of a scripted call: per step speech (speech_end_ms, else words at
    _WORDS_PER_MIN) + pause + resumption + post-turn silence (E-END resumption_ms
    on that step, else `eend_silence_ms`); same geometry as ScriptedCaller."""
    eend = {o.anchor_step: o.resumption_ms for o in scenario.overlays
            if o.cls == "E-END" and o.resumption_ms is not None}
    ms = 0.0
    for st in scenario.steps:
        words = len(st.text.split())
        ms += st.speech_end_ms if st.speech_end_ms is not None else \
            words / _WORDS_PER_MIN * _S_PER_MIN * _MS_PER_S
        if st.pause_ms is not None:
            ms += st.pause_ms + len((st.resumption_text or "").split()) / _WORDS_PER_MIN \
                * _S_PER_MIN * _MS_PER_S
        ms += eend.get(st.id, eend_silence_ms)
    return ms / _MS_PER_S / _S_PER_MIN


def project(scenarios: Iterable[Any], system: Any, prices: Mapping[str, Any] | None,
            minutes: Mapping[str, float] | None = None, *, calls_per_scenario: int = 1) -> float:
    """Projected USD for one call of each scenario (times `calls_per_scenario`) on `system`.
    `minutes` maps scenario id -> estimated minutes; missing ids use the price file's
    `default_call_minutes[track]`. `system` is accepted for signature symmetry (a
    per-system price override would live there); it is not read today."""
    total = 0.0
    for s in scenarios:
        m = (minutes or {}).get(s.id)
        if m is None:
            m = default_minutes(prices, s.track)
        total += project_call_usd(m, prices) * calls_per_scenario
    return total


def _get(d: Mapping | None, *path: str) -> float:
    cur: Any = d or {}
    for k in path:
        if not isinstance(cur, Mapping):
            return 0.0
        cur = cur.get(k)
    return float(cur or 0)


def sum_usage(usages: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Token classes summed over every `usage` payload of a call."""
    tot = dict.fromkeys(("audio_in", "audio_in_cached", "text_in", "text_in_cached",
                         "audio_out", "text_out", "other_in"), 0.0)
    for u in usages:
        u = u.get("usage", u) if isinstance(u, Mapping) else {}
        a_in = _get(u, "input_token_details", "audio_tokens")
        t_in = _get(u, "input_token_details", "text_tokens")
        cached = _get(u, "input_token_details", "cached_tokens")
        a_c = _get(u, "input_token_details", "cached_tokens_details", "audio_tokens")
        t_c = _get(u, "input_token_details", "cached_tokens_details", "text_tokens")
        if cached and not (a_c or t_c):           # no split given: attribute to text first
            t_c = min(cached, t_in)
            a_c = cached - t_c
        tot["audio_in"] += a_in - a_c
        tot["audio_in_cached"] += a_c
        tot["text_in"] += t_in - t_c
        tot["text_in_cached"] += t_c
        tot["audio_out"] += _get(u, "output_token_details", "audio_tokens")
        tot["text_out"] += _get(u, "output_token_details", "text_tokens")
        tot["other_in"] += max(0.0, _get(u, "input_tokens") - a_in - t_in)  # image etc.
    return tot


def price_per_call(usage: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
                   prices: Mapping[str, Any] | None, *, minutes: float | None = None) -> float | None:
    """Actual USD of one call. `usage` is the list of usage payloads (or one payload).
    Free system (prices None) -> 0.0. Per-minute providers need `minutes`.
    An empty usage list is 0.0 (no response was billed); `usage=None` (the adapter
    exposes no usage at all) is None, as is a non-zero token class without a rate."""
    if prices is None:
        return 0.0
    if "usd_per_min" in prices:
        return None if minutes is None else float(minutes) * float(prices["usd_per_min"])
    if usage is None:
        return None
    usages = [usage] if isinstance(usage, Mapping) else list(usage)
    if not usages:
        return 0.0       # [Assumption] token providers bill only through responses (usage events)
    rates = prices.get("rates") or {}
    tot = sum_usage(usages)
    if tot.pop("other_in"):
        return None                                  # unpriced input class (image, ...)
    usd = 0.0
    for k, n in tot.items():
        if not n:
            continue
        if k not in rates:
            return None
        usd += n * float(rates[k]) / _PER_M
    return usd


# BUILD_PLAN M3-T01 names
def actual_call_usd(usage_event: Any, prices: Mapping[str, Any] | None) -> float | None:
    return price_per_call(usage_event, prices)
