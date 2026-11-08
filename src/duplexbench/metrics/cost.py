"""Cost projection and actual cost per call (spec §4.4 "Cost per connected minute", §9.3; BUILD_PLAN M3-T01).

    usd_per_min(prices)                         projected USD per connected minute
    project_call_usd(minutes, prices)           one call (None prices = free system -> 0.0)
    project(scenarios, system, prices, minutes) a whole run's calls -> USD
    price_per_call(usage, prices)               actual USD from provider usage, None if unpriceable
    est_call_minutes(scenario, eend_silence_ms) script length estimate for a scripted caller

Track C simulated-caller costs (fix-28), added to the agent's cost per call:

    refasr_call_usd(minutes, usd_per_audio_min) reference ASR (one stream per call, every
                                                played ear frame pushed: billed audio = call length)
    caller_llm_projected_usd(n_steps, prices, model)  caller LLM, [Assumption] tokens per turn
    caller_llm_actual_usd(usages, prices, model)      caller LLM, summed response `usage`

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


# -- Track C simulated caller (fix-28) -------------------------------------------------
def refasr_call_usd(minutes: float, usd_per_audio_min: float) -> float:
    """Reference-ASR USD of one simulated call. The caller opens one stream per call and
    pushes every played ear frame (silence included), so billed audio = call length."""
    return max(0.0, float(minutes)) * float(usd_per_audio_min)


def chat_rates(prices: Mapping[str, Any], model: str | None) -> Mapping[str, Any]:
    """`rates[model]` of a chat price file (configs/prices/openai_chat.yaml); ValueError
    when the model has no input_per_1m / output_per_1m."""
    r = (prices.get("rates") or {}).get(model) if model else None
    if not isinstance(r, Mapping) or "input_per_1m" not in r or "output_per_1m" not in r:
        raise ValueError(f"price file {prices.get('price_ref')!r} has no input_per_1m/"
                         f"output_per_1m rates for model {model!r}")
    return r


def _chat_projection(prices: Mapping[str, Any]) -> Mapping[str, Any]:
    p = prices.get("projection") or {}
    need = ("input_tokens_per_turn", "output_tokens_per_turn", "extra_turns_per_call")
    missing = [k for k in need if k not in p]
    if missing:
        raise ValueError(f"price file {prices.get('price_ref')!r} projection lacks {missing}")
    return p


def caller_llm_turns(n_steps: int, prices: Mapping[str, Any]) -> int:
    """[Assumption] caller turns per call = scenario steps + `extra_turns_per_call`."""
    return int(n_steps) + int(_chat_projection(prices)["extra_turns_per_call"])


def caller_llm_projected_usd(n_steps: int, prices: Mapping[str, Any] | None,
                             model: str | None) -> float:
    """Projected caller-LLM USD of one call: turns x (in tok x in rate + out tok x out rate)
    / 1e6, token counts per turn from the price file's [Assumption] projection block.
    No price file (self-hosted, billing external) -> 0.0."""
    if prices is None:
        return 0.0
    r, p = chat_rates(prices, model), _chat_projection(prices)
    per_turn = (float(p["input_tokens_per_turn"]) * float(r["input_per_1m"])
                + float(p["output_tokens_per_turn"]) * float(r["output_per_1m"]))
    return caller_llm_turns(n_steps, prices) * per_turn / _PER_M


def sum_chat_usage(usages: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """input/output tokens summed over chat responses (CallerLLM's Anthropic-shaped
    `input_tokens`/`output_tokens`, or raw OpenAI `prompt_tokens`/`completion_tokens`)."""
    tot = {"input_tokens": 0, "output_tokens": 0, "responses": 0}
    for u in usages:
        if not isinstance(u, Mapping):
            continue
        tot["input_tokens"] += int(u.get("input_tokens", u.get("prompt_tokens")) or 0)
        tot["output_tokens"] += int(u.get("output_tokens", u.get("completion_tokens")) or 0)
        tot["responses"] += 1
    return tot


def caller_llm_actual_usd(usages: Iterable[Mapping[str, Any]], prices: Mapping[str, Any] | None,
                          model: str | None) -> float:
    """Actual caller-LLM USD of one call from the summed response `usage`; 0.0 without a
    price file (self-hosted endpoint, billing external)."""
    if prices is None:
        return 0.0
    r, t = chat_rates(prices, model), sum_chat_usage(usages)
    return (t["input_tokens"] * float(r["input_per_1m"])
            + t["output_tokens"] * float(r["output_per_1m"])) / _PER_M


# BUILD_PLAN M3-T01 names
def actual_call_usd(usage_event: Any, prices: Mapping[str, Any] | None) -> float | None:
    return price_per_call(usage_event, prices)
