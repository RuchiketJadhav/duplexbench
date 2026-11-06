"""The spec §1.5 latency taxonomy (the only latency names allowed; BUILD_PLAN M1-T11).

Every function reads a `CallRecord` and returns `list[Observation]`. Units: ms for
every `T_*` except `T_dead_air` and `dead_air_in_tool_s` (seconds, spec §1.5).

Anchors (spec §1.5), and where they come from in the record:

    caller turn onset / end   events `caller_turn_onset` / `caller_turn_end` (payload step_id),
                              logged by the runner at send time of the first / last speech frame
    overlay onset / end       `rec.overlays[].onset_ns / end_ns`
    agent onset               first active frame of an agent utterance at the ear: the
                              `playout` rows segmented by `metrics.activity.segment`
    first agent byte          event `first_audio_byte` (payload turn_end_ns), adapter base class
    provider cancel           event `cancel_or_truncate`
    tool start / result       `rec.tool_calls[].start_ns / result_ns` (else `tool_call` events)
    stop                      start of the first inactivity run at or after an overlay onset

`observe(rec, th)` returns all of them. Undefined values are `value=None,
valid=False` with a tag where the spec names one (`not_stopped`).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import numpy as np

from duplexbench.metrics.activity import run_frames, segment
from duplexbench.metrics.base import Observation
from duplexbench.metrics.names import Metric

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord
    from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000
_NS_PER_S = 1_000_000_000

# reference-ASR text of agent utterances: (onset_ns, end_ns) -> text (M4 supplies it)
UtteranceText = Callable[[int, int], str]


def _th(th: "Thresholds | None") -> "Thresholds":
    if th is None:
        from duplexbench.thresholds import Thresholds
        return Thresholds()
    return th


def _obs(rec: "CallRecord", metric: Metric, value: float | None, event_id: str | None = None,
         valid: bool | None = None, tags: tuple[str, ...] = ()) -> Observation:
    sc = getattr(rec, "scenario", None)
    return Observation(metric=str(metric), call_id=rec.call_id,
                       scenario_id=getattr(sc, "id", "") or "", trial=rec.trial,
                       condition=str(rec.condition), system_id=rec.system_id, event_id=event_id,
                       value=None if value is None else float(value),
                       valid=(value is not None) if valid is None else valid, tags=tags)


# Overlay classes each per-overlay latency applies to (spec §1.5, §4.1). "" is the
# runner's own row when no overlay engine ran (M1 calibration, golden M1).
YIELD_CLASSES = frozenset({"Y-INT", "Y-CMD", ""})      # T_stop_ear, T_stop_model, playout penalty
RESPOND_CLASSES = frozenset({"Y-INT", ""})              # T_respond_ear ("all stopped Y-INT events")
GEOMETRY_CLASSES = frozenset({"P-PAUSE", "E-END"})       # no injected audio: no backlog at onset


def _yield_rows(rec: "CallRecord") -> list:
    return [ov for ov in rec.overlays if ov.cls in YIELD_CLASSES and ov.onset_ns is not None]


def _ms(dt_ns: int) -> float:
    return dt_ns / _NS_PER_MS


# -- record views -------------------------------------------------------------------
def caller_turns(rec: "CallRecord") -> list[tuple[str, int | None, int]]:
    """(step_id, onset_ns or None, end_ns) per caller turn, in end order."""
    onsets: dict[str, int] = {}
    out = []
    for e in sorted(rec.events, key=lambda e: (e.t_ns, e.seq)):
        sid = str(e.payload.get("step_id", ""))
        if e.kind == "caller_turn_onset":
            onsets.setdefault(sid, e.t_ns)
        elif e.kind == "caller_turn_end":
            out.append((sid, onsets.get(sid), e.t_ns))
    return out


def agent_utterances(rec: "CallRecord", th: "Thresholds") -> list[tuple[int, int]]:
    """(onset_ns, end_ns) of agent utterances at the ear (spec §1.2); end = stop time."""
    p = rec.playout
    if p is None or len(p) == 0:
        return []
    t = np.asarray(p["t_ns"], dtype=np.int64)
    frame_ns = th.frame_ms * _NS_PER_MS
    out = []
    for a, b in segment(np.asarray(p["active"], dtype=bool), th):
        end = int(t[b]) if b < len(t) else int(t[-1]) + frame_ns
        out.append((int(t[a]), end))
    return out


def stop_after(rec: "CallRecord", th: "Thresholds", t0_ns: int) -> int | None:
    """Start of the first inactivity run (>= inactivity_run_ms of inactive frames)
    beginning at or after the frame containing `t0_ns` (spec §1.5 "stop")."""
    p = rec.playout
    if p is None or len(p) == 0:
        return None
    t = np.asarray(p["t_ns"], dtype=np.int64)
    act = np.asarray(p["active"], dtype=bool)
    frame_ns = th.frame_ms * _NS_PER_MS
    i0 = int(np.searchsorted(t, t0_ns - frame_ns + 1))     # frame whose span contains t0
    run = run_frames(th)
    streak = 0
    for i in range(i0, len(t)):
        if act[i]:
            streak = 0
            continue
        streak += 1
        if streak >= run:
            return int(t[i - run + 1])
    return None


def _cancels(rec: "CallRecord") -> list[int]:
    return sorted(e.t_ns for e in rec.events if e.kind == "cancel_or_truncate")


def _tools(rec: "CallRecord") -> list[tuple[str, int, int | None]]:
    """(tool_call_id, start_ns, result_ns)."""
    if rec.tool_calls:
        return [(r.tool_call_id, r.start_ns, r.result_ns) for r in rec.tool_calls]
    res = {str(e.payload.get("id")): e.t_ns for e in rec.events if e.kind == "tool_result"}
    return [(str(e.payload.get("id")), e.t_ns, res.get(str(e.payload.get("id"))))
            for e in rec.events if e.kind == "tool_call"]


# -- §1.5 names -----------------------------------------------------------------------
def t_ttfa_recv(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_ttfa_recv`: caller turn end -> first agent audio byte received (model-level diagnostic)."""
    fabs = [(int(e.payload.get("turn_end_ns", -1)), e.t_ns) for e in rec.events
            if e.kind == "first_audio_byte"]
    out = []
    for sid, _on, end in caller_turns(rec):
        hit = next((t for te, t in fabs if te == end), None)
        out.append(_obs(rec, Metric.t_ttfa_recv, None if hit is None else _ms(hit - end), sid))
    return out


def t_turn_gap_ear(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_turn_gap_ear`: caller turn end -> agent onset of the first agent utterance that
    begins after the caller's turn onset. Signed; tail audio of an utterance that began before
    the caller started is excluded."""
    th = _th(th)
    utts = agent_utterances(rec, th)
    out = []
    for sid, on, end in caller_turns(rec):
        ref = on if on is not None else end
        nxt = next((u for u, _ in utts if u > ref), None)
        out.append(_obs(rec, Metric.t_turn_gap_ear, None if nxt is None else _ms(nxt - end), sid))
    return out


def t_stop_ear(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_stop_ear`: overlay onset -> stop; defined only when the stop begins within
    `hold_window_ms` (2.0 s), else "not stopped"."""
    th = _th(th)
    win = th.hold_window_ms * _NS_PER_MS
    out = []
    for ov in _yield_rows(rec):
        s = stop_after(rec, th, ov.onset_ns)
        if s is None or s - ov.onset_ns > win:
            out.append(_obs(rec, Metric.t_stop_ear, None, ov.overlay_id, False, ("not_stopped",)))
        else:
            out.append(_obs(rec, Metric.t_stop_ear, _ms(max(0, s - ov.onset_ns)), ov.overlay_id))
    return out


def _first_cancel(rec: "CallRecord", t0: int) -> int | None:
    return next((t for t in _cancels(rec) if t >= t0), None)


def t_stop_model(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_stop_model`: overlay onset -> provider cancel/truncate (or cascade interruption)
    event. Diagnostic; absent (no observation) for a system without such an event."""
    out = []
    for ov in _yield_rows(rec):
        c = _first_cancel(rec, ov.onset_ns)
        if c is not None:
            out.append(_obs(rec, Metric.t_stop_model, _ms(c - ov.onset_ns), ov.overlay_id,
                            tags=("diagnostic",)))
    return out


def playout_penalty(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.3/§1.5 playout penalty = `T_stop_ear` − `T_stop_model` per overlay (how much of a
    stop latency is backlog rather than model); only where both are defined."""
    th = _th(th)
    stops = {o.event_id: o.value for o in t_stop_ear(rec, th) if o.valid}
    out = []
    for o in t_stop_model(rec, th):
        if o.event_id in stops:
            out.append(_obs(rec, Metric.playout_penalty, stops[o.event_id] - o.value, o.event_id,
                            tags=("diagnostic",)))
    return out


def backlog_ms(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.3 `backlog_ms` at each overlay onset (per-system diagnostic): the playout row at or
    before the onset."""
    p = rec.playout
    out = []
    if p is None or len(p) == 0:
        return out
    t = np.asarray(p["t_ns"], dtype=np.int64)
    for ov in rec.overlays:
        if ov.onset_ns is None or ov.cls in GEOMETRY_CLASSES:
            continue
        i = int(np.searchsorted(t, ov.onset_ns, side="right")) - 1
        v = float(p["backlog_ms"][i]) if i >= 0 else 0.0
        out.append(_obs(rec, Metric.backlog_ms, v, ov.overlay_id, tags=("diagnostic",)))
    return out


def t_respond_ear(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_respond_ear`: overlay end -> agent onset of the first agent utterance that begins
    after the stop. Undefined if no stop (within the hold window). Recording-only."""
    th = _th(th)
    utts = agent_utterances(rec, th)
    stops = {o.event_id for o in t_stop_ear(rec, th) if o.valid}
    out = []
    for ov in rec.overlays:
        if ov.cls not in RESPOND_CLASSES:
            continue
        if ov.onset_ns is None or ov.end_ns is None or ov.overlay_id not in stops:
            continue
        s = stop_after(rec, th, ov.onset_ns)
        nxt = next((u for u, _ in utts if u > s), None)
        out.append(_obs(rec, Metric.t_respond_ear, None if nxt is None else _ms(nxt - ov.end_ns),
                        ov.overlay_id))
    return out


def t_tool_offset(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_tool_offset`: end of the caller turn carrying the last required argument -> tool
    call start. Signed; negative = pre-emptive.

    M1: the anchor turn is the last caller turn ending at or before the call start.
    TODO(M4): take the turn from the scenario's argument-carrying step so a pre-emptive call
    (before that turn ends) comes out negative.
    """
    ends = [end for _sid, _on, end in caller_turns(rec)]
    out = []
    for tid, start, _res in _tools(rec):
        prev = [e for e in ends if e <= start]
        out.append(_obs(rec, Metric.t_tool_offset, _ms(start - prev[-1]) if prev else None, tid))
    return out


def t_tool_to_info_ear(rec: "CallRecord", th: "Thresholds | None" = None,
                       utterance_text: UtteranceText | None = None,
                       result_anchors: Sequence[str] | None = None) -> list[Observation]:
    """§1.5 `T_tool_to_info_ear`: tool result returned -> agent onset of the first agent
    utterance whose reference-ASR text contains a result anchor.

    Needs reference-ASR text (M4): without `utterance_text` it returns no observations.
    Anchors come from `result_anchors`, else the tool row's `result["anchors"]`.
    """
    if utterance_text is None:
        return []
    th = _th(th)
    utts = agent_utterances(rec, th)
    out = []
    rows = {r.tool_call_id: r for r in rec.tool_calls}
    for tid, _start, res in _tools(rec):
        if res is None:
            continue
        row = rows.get(tid)
        anchors = (list(result_anchors) if result_anchors
                   else list(((row.result or {}) if row else {}).get("anchors", [])))
        hit = None
        for on, end in utts:
            if on <= res:
                continue
            text = utterance_text(on, end).lower()
            if any(a.lower() in text for a in anchors):
                hit = on
                break
        out.append(_obs(rec, Metric.t_tool_to_info_ear, None if hit is None else _ms(hit - res), tid))
    return out


def _intervals_union(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _subtract(a: int, b: int, cuts: list[tuple[int, int]]) -> tuple[list[tuple[int, int]], int]:
    """Parts of [a, b) outside `cuts`, and the total length inside them."""
    parts, inside, cur = [], 0, a
    for c0, c1 in cuts:
        if c1 <= cur or c0 >= b:
            continue
        if c0 > cur:
            parts.append((cur, c0))
        inside += min(c1, b) - max(c0, cur)
        cur = max(cur, c1)
    if cur < b:
        parts.append((cur, b))
    return parts, inside


def t_dead_air(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_dead_air` (seconds): sum of gaps >= `dead_air_gap_ms` with neither channel active
    after the first caller turn, excluding time during a pending tool call. Also, separately:
    the maximum single such gap (event_id "max_gap", diagnostic) and `dead_air_in_tool_s`, the
    silence of gaps >= `dead_air_gap_ms` that falls during tool waits (§1.5, §4.2)."""
    th = _th(th)
    turns = caller_turns(rec)
    if not turns:
        return []
    frame_ns = th.frame_ms * _NS_PER_MS
    start = min((on if on is not None else end) for _s, on, end in turns)
    active: list[tuple[int, int]] = [((on if on is not None else end), end + frame_ns)
                                     for _s, on, end in turns]
    active += [(o.onset_ns, o.end_ns + frame_ns) for o in rec.overlays
               if o.onset_ns is not None and o.end_ns is not None]
    p = rec.playout
    if p is not None and len(p):
        t = np.asarray(p["t_ns"], dtype=np.int64)
        act = np.asarray(p["active"], dtype=bool)
        active += [(int(x), int(x) + frame_ns) for x in t[act]]
        call_end = int(t[-1]) + frame_ns
    else:
        call_end = max(b for _a, b in active)
    act_u = _intervals_union([iv for iv in active if iv[1] > start])
    tools = _intervals_union([(s, r if r is not None else call_end) for _i, s, r in _tools(rec)])
    gap_min = th.dead_air_gap_ms * _NS_PER_MS
    gaps, cur = [], start
    for a, b in act_u:
        if a > cur:
            gaps.append((cur, a))
        cur = max(cur, b)
    if cur < call_end:
        gaps.append((cur, call_end))
    total = in_tool = 0
    longest = 0
    for a, b in gaps:
        if b - a < gap_min:
            continue
        parts, inside = _subtract(a, b, tools)
        in_tool += inside
        for p0, p1 in parts:
            if p1 - p0 >= gap_min:
                total += p1 - p0
                longest = max(longest, p1 - p0)
    return [_obs(rec, Metric.t_dead_air, total / _NS_PER_S),
            _obs(rec, Metric.t_dead_air, longest / _NS_PER_S, "max_gap", tags=("diagnostic",)),
            _obs(rec, Metric.dead_air_in_tool_s, in_tool / _NS_PER_S)]


def t_caller_think(rec: "CallRecord", th: "Thresholds | None" = None) -> list[Observation]:
    """§1.5 `T_caller_think`: agent turn end (ear inactivity >= `sim_eot_ms`) -> caller audio
    onset. A simulator property logged for validity, never a system metric (tag diagnostic)."""
    th = _th(th)
    eot = th.sim_eot_ms * _NS_PER_MS
    utts = agent_utterances(rec, th)
    onsets = sorted(on for _s, on, _e in caller_turns(rec) if on is not None)
    out = []
    for i, (_on, end) in enumerate(utts):
        nxt_agent = utts[i + 1][0] if i + 1 < len(utts) else None
        nxt_caller = next((c for c in onsets if c >= end), None)
        if nxt_caller is None:
            continue
        if nxt_agent is not None and nxt_agent < nxt_caller:
            continue
        if nxt_agent is not None and nxt_agent - end < eot:
            continue                      # agent turn end needs >= sim_eot_ms of ear inactivity
        out.append(_obs(rec, Metric.t_caller_think, _ms(nxt_caller - end), f"utt{i}",
                        tags=("diagnostic",)))
    return out


def observe(rec: "CallRecord", th: "Thresholds") -> list[Observation]:
    """Every §1.5 latency name plus playout_penalty and backlog_ms (spec §1.3)."""
    out: list[Observation] = []
    for fn in (t_turn_gap_ear, t_ttfa_recv, t_stop_ear, t_stop_model, playout_penalty, backlog_ms,
               t_respond_ear, t_tool_offset, t_tool_to_info_ear, t_dead_air, t_caller_think):
        out += fn(rec, th)
    return out
