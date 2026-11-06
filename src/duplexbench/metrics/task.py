"""Track C task metrics (BUILD_PLAN M4-T05; spec §4.2, §1.5, eng review D6).

    score_track_c(rec, scenario, tool_rows, refasr_words, th, *, final_state=None)
        -> list[Observation]

Time bases: tool rows and playout are ns on the call clock; `refasr_words`
(the agent's ear-channel reference-ASR words) are ms on the same timeline.

pass_at_1 (per trial)       canonical final state `expected_equals` scenario.expected_state
                            AND every `required_spoken` item found in normalize_entity(reference
                            transcript) (its normalized `content` as a phrase, or every one of its
                            `anchors` as a token; "13th" counts for anchor "13")
                            AND no policy-violation row (a tool row whose result JSON contains
                            `"code"`, the PolicyError shape). `final_state` defaults to the payload
                            of the last `final_state` event in `rec.events`; with neither, the
                            observation is invalid, tag `no_state`.
policy_violation_rate       1.0 when any violation row, else 0.0.
t_tool_offset (per tool)    tool start_ns - end of the caller step carrying its arguments: the
                            tool def's `anchor_step` when declared, else the last caller turn
                            ending at or before start_ns. Caller turn ends come from
                            `rec.sim_turns` (send_ns + last TTS word end), else scenario steps'
                            `speech_end_ms`. Writes only: preemptive_tool_rate = offset < 0.
t_tool_to_info_ear          onset of the agent utterance containing the first result-anchor word
                            heard after result_ns - result_ns (utterance-onset rule, spec §1.5).
anchor_offset_in_utt_ms     that anchor word's start - the same utterance's onset (diagnostic, D6).
mishearing_rate (per call)  spec §7 "Hears", §4.5: word error rate of the provider's output
                            transcript against the reference-ASR text of the ear, from the
                            `mishearing` event (src harness, payload refasr_text / provider_text)
                            the run logs per simulated call; tokens from `normalize.normalize`;
                            edit distance / reference token count. Tagged diagnostic. No
                            observation when the provider emitted no transcript; invalid (tag
                            `no_refasr_words`) when the reference ASR heard nothing.

Agent utterances come from `activity.segment(rec.playout["active"])`; with no
playout, from `refasr_words` split at gaps >= th.inactivity_run_ms. No metric
here uses a classifier, so none is tagged `judged`/`classifier`. Every
Observation is tagged `domain:<d>`.

Not here yet (BUILD_PLAN M4-T05 full list): stale_action_rate, auth_bypass_rate,
tool-wait filler/dead-air, repeated prompts, response rate per turn, call outcome,
and the "before the corresponding write" ordering of required_spoken.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Iterable

import numpy as np

from duplexbench.metrics.activity import segment
from duplexbench.metrics.base import Observation
from duplexbench.metrics.names import Metric
from duplexbench.metrics.normalize import normalize, normalize_entity
from duplexbench.tools.state import expected_equals

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord
    from duplexbench.refasr.engine import Word
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000
_MAX_ANCHOR_WORDS = 8          # longest spoken form an anchor may take ("b k four four seven one")
POLICY_MARK = '"code"'
MISHEARING_EVENT = "mishearing"
NO_REFASR_WORDS = "no_refasr_words"
NO_STATE = "no_state"
DIAGNOSTIC = "diagnostic"


# -- row access (ToolRow dataclass or a raw `tool_calls` dict) ----------------------------------
@dataclass(frozen=True)
class _Tool:
    id: str
    name: str
    args: dict
    start_ns: int
    result_ns: int | None
    result_text: str


def _tool(r: Any) -> _Tool:
    if isinstance(r, dict):
        def g(k, d=None):
            return r.get(k, d)
    else:
        def g(k, d=None):
            return getattr(r, k, d)
    res = g("result_json")
    if res is None:
        res = g("result")
        res = json.dumps(res, sort_keys=True) if res is not None else ""
    args = g("args")
    if args is None:
        aj = g("args_json")
        args = json.loads(aj) if aj else {}
    rn = g("result_ns")
    return _Tool(str(g("tool_call_id", "") or ""), g("name"), args, int(g("start_ns")),
                 None if rn is None else int(rn), str(res))


def is_policy_violation(row: Any) -> bool:
    return POLICY_MARK in _tool(row).result_text


# -- timelines ----------------------------------------------------------------------------------
def agent_utterances(rec: "CallRecord", refasr_words: list["Word"],
                     th: "Thresholds") -> list[tuple[float, float]]:
    """(onset_ms, end_ms) of agent utterances on the call timeline."""
    pl = rec.playout
    if pl is not None and len(pl):
        t_ms = np.asarray(pl["t_ns"], dtype=np.float64) / _NS_PER_MS
        out = []
        for on, end in segment(pl["active"], th):
            end_ms = t_ms[end] if end < len(t_ms) else t_ms[-1] + float(th.frame_ms)
            out.append((float(t_ms[on]), float(end_ms)))
        return out
    out2: list[tuple[float, float]] = []
    for w in sorted(refasr_words, key=lambda w: w.start_ms):
        if out2 and w.start_ms - out2[-1][1] < th.inactivity_run_ms:
            out2[-1] = (out2[-1][0], max(out2[-1][1], float(w.end_ms)))
        else:
            out2.append((float(w.start_ms), float(w.end_ms)))
    return out2


def caller_turn_ends(rec: "CallRecord", scenario: "Scenario") -> list[tuple[str | None, int]]:
    """(step_id, end_ns) per caller turn, in order."""
    steps = scenario.steps
    turns = [t for t in rec.sim_turns if not t.discarded]
    if turns:
        out: list[tuple[str | None, int]] = []
        for i, t in enumerate(turns):
            last = max((float(w.get("end_ms", 0)) for w in t.tts_words), default=0.0)
            out.append((steps[i].id if i < len(steps) else None, int(t.send_ns + last * _NS_PER_MS)))
        return out
    return [(s.id, int(s.speech_end_ms * _NS_PER_MS)) for s in steps if s.speech_end_ms is not None]


def caller_anchor_end_ns(tool: _Tool, tooldef: Any, ends: list[tuple[str | None, int]]) -> int | None:
    step = getattr(tooldef, "anchor_step", None) if tooldef is not None else None
    if step is not None:
        for sid, e in ends:
            if sid == step:
                return e
    before = [e for _, e in ends if e <= tool.start_ns]
    return before[-1] if before else None


# -- transcript matching ------------------------------------------------------------------------
@lru_cache(maxsize=4096)
def _n(text: str) -> str:
    return normalize_entity(text)


_ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")


def _tok_eq(tok: str, anchor: str) -> bool:
    """Equal after dropping ordinal suffixes on digits ("13th" == "13", "november 12th" == "november 12")."""
    return tok == anchor or _ORDINAL.sub(r"\1", tok) == _ORDINAL.sub(r"\1", anchor)


def spoken(norm_text: str, item: dict) -> bool:
    """A `required_spoken` item is present in already-normalized text.

    `content` matches as a whole phrase, or (fix-21) as an entity of the item's `type`
    read back in groups ("four one five, five five five, ..." for a phone), via
    `entities.contains_entity`. Each anchor matches a whole token or a typed group."""
    from duplexbench.metrics.entities import contains_entity
    etype = item.get("type")
    content = str(item.get("content", ""))
    if content.strip() and contains_entity(norm_text, content, etype):
        return True
    anchors = [str(x) for x in item.get("anchors", []) if _n(str(x))]
    toks = norm_text.split()
    return bool(anchors) and all(
        any(_tok_eq(t, _n(a)) for t in toks) or contains_entity(norm_text, a, etype)
        for a in anchors)


def first_anchor_word(words: list["Word"], anchors: Iterable[str], after_ms: float) -> "Word | None":
    """First word (start >= after_ms) that begins a span normalizing to one of `anchors`."""
    norm_anchors = {_n(str(a)) for a in anchors} - {""}
    ws = sorted((w for w in words if w.start_ms >= after_ms), key=lambda w: w.start_ms)
    for i, w in enumerate(ws):
        for L in range(1, min(_MAX_ANCHOR_WORDS, len(ws) - i) + 1):
            span = _n(" ".join(x.text for x in ws[i:i + L]))
            if any(_tok_eq(span, a) for a in norm_anchors):
                return w
    return None


def utterance_of(t_ms: float, utts: list[tuple[float, float]]) -> tuple[float, float] | None:
    """The utterance containing t_ms; else the latest one starting at or before it."""
    best = None
    for on, end in utts:
        if on <= t_ms <= end:
            return (on, end)
        if on <= t_ms:
            best = (on, end)
    return best


# -- scoring ------------------------------------------------------------------------------------
def _final_state(rec: "CallRecord") -> dict | None:
    st = None
    for e in rec.events:
        if e.kind == "final_state":
            st = e.payload.get("state", e.payload)
    return st


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Word-level Levenshtein distance (substitutions + deletions + insertions)."""
    d = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(hyp)]


def mishearing(refasr_text: str, provider_text: str) -> float | None:
    """WER of `provider_text` against `refasr_text` on normalized tokens; None when the
    reference is empty."""
    ref, hyp = normalize(refasr_text or "").split(), normalize(provider_text or "").split()
    if not ref:
        return None
    return edit_distance(ref, hyp) / len(ref)


def _mishearing_event(rec: "CallRecord") -> dict | None:
    ev = None
    for e in rec.events:
        if e.kind == MISHEARING_EVENT:
            ev = e.payload
    return ev


def score_track_c(rec: "CallRecord", scenario: "Scenario", tool_rows: list[Any],
                  refasr_words: list["Word"], th: "Thresholds", *,
                  final_state: dict | None = None) -> list[Observation]:
    dom = f"domain:{scenario.domain}"

    def obs(metric: Metric, value, event_id=None, valid=True, extra=()):
        return Observation(metric=metric.value, call_id=rec.call_id, scenario_id=scenario.id,
                           trial=rec.trial, condition=rec.condition, system_id=rec.system_id,
                           event_id=event_id, value=value, valid=valid, tags=(dom, *extra))

    tools = [_tool(r) for r in tool_rows]
    defs = {d.name: d for d in scenario.tools}
    norm = _n(" ".join(w.text for w in sorted(refasr_words, key=lambda w: w.start_ms)))
    out: list[Observation] = []

    violation = any(POLICY_MARK in t.result_text for t in tools)
    state = final_state if final_state is not None else _final_state(rec)
    if state is None:
        out.append(obs(Metric.pass_at_1, None, valid=False, extra=(NO_STATE,)))
    else:
        ok = (expected_equals(state, scenario.expected_state)
              and all(spoken(norm, it) for it in scenario.required_spoken)
              and not violation)
        out.append(obs(Metric.pass_at_1, 1.0 if ok else 0.0))
    out.append(obs(Metric.policy_violation_rate, 1.0 if violation else 0.0))

    ends = caller_turn_ends(rec, scenario)
    utts = agent_utterances(rec, refasr_words, th)
    for t in tools:
        d = defs.get(t.name)
        a_end = caller_anchor_end_ns(t, d, ends)
        if a_end is None:
            out.append(obs(Metric.t_tool_offset, None, t.id, valid=False))
        else:
            off = (t.start_ns - a_end) / _NS_PER_MS
            out.append(obs(Metric.t_tool_offset, off, t.id))
            if d is not None and d.kind == "write":
                out.append(obs(Metric.preemptive_tool_rate, 1.0 if off < 0 else 0.0, t.id))
        if d is None or not d.result_anchors or t.result_ns is None:
            continue
        res_ms = t.result_ns / _NS_PER_MS
        w = first_anchor_word(refasr_words, d.result_anchors, res_ms)
        u = utterance_of(w.start_ms, utts) if w is not None else None
        if u is None:
            out.append(obs(Metric.t_tool_to_info_ear, None, t.id, valid=False))
            continue
        out.append(obs(Metric.t_tool_to_info_ear, u[0] - res_ms, t.id))
        out.append(obs(Metric.anchor_offset_in_utt_ms, w.start_ms - u[0], t.id, extra=(DIAGNOSTIC,)))

    mh = _mishearing_event(rec)
    if mh is not None and normalize(mh.get("provider_text") or ""):
        v = mishearing(mh.get("refasr_text") or "", mh.get("provider_text") or "")
        if v is None:
            out.append(obs(Metric.mishearing_rate, None, valid=False,
                           extra=(DIAGNOSTIC, NO_REFASR_WORDS)))
        else:
            out.append(obs(Metric.mishearing_rate, v, extra=(DIAGNOSTIC,)))
    return out


def observe(rec: "CallRecord", th: "Thresholds", refasr_words: list["Word"] | None = None,
            final_state: dict | None = None) -> list[Observation]:
    """Module-contract entry point (metrics/base.py)."""
    return score_track_c(rec, rec.scenario, rec.tool_calls, refasr_words or [], th,
                         final_state=final_state)
