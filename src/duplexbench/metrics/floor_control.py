"""Track A floor-control metrics (spec §4.1; validity spec §3.4; BUILD_PLAN M2-T08).

    observe(rec, th, *, utt_text=None) -> list[Observation]      pure: CallRecord + overlay rows

One Observation per (overlay event, metric, variant), `event_id` = overlay id:

    metric               classes                                      variant tag
    yield_rate           Y-INT, Y-CMD                                 @<B>  B in th.yield_budgets_ms
    t_stop_ear           Y-INT, Y-CMD                                 -     (window th.hold_window_ms)
    talk_over_share      Y-INT, Y-CMD                                 -
    wait_compliance      wait-class Y-CMD (has a scripted resumption)  -
    hold_speaking_rate   H-BC, H-SIDE/H-BG/H-TIC during_agent          @<W>  W in (hold_window_ms, hold_also_ms)
    content_leakage_rate same events as hold_speaking_rate            -     (needs reference ASR: no_refasr)
    hold_listening_rate  P-PAUSE, H-SIDE/H-BG/H-TIC in_pause           -
    response_rate        E-END                                        @<W>  W in th.response_windows_ms
    respond_resume       Y-INT that stopped within the hold window    -     (classifier; no_refasr without text)

Tags (report/aggregate.py conventions, worklog M7-T03): `@<v>` variant, `cls:<class>`
breakdown, and the exclusion tags of metrics/base.TAGS (`not_injected`, `contested`
from the overlay row; `ambiguous_end`, `unmatched_control` from the control twin for
the windowed metrics yield_rate / t_stop_ear / hold_speaking_rate; `not_stopped`).
An excluded observation has `valid=False`; its value is kept only for `contested`
(the contested table reports it, spec §3.3), else None.

A stop is ear inactivity only (`latency.stop_after`, spec §1.5): a provider cancel
event never counts, so cancel-and-restart earns nothing (spec §4.1). The validity
offset is the scenario's pre-registered `Overlay.offset_ms` (spec §3.4).
Control-twin calls are not scored here (they feed validity only).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

from duplexbench.metrics.base import Observation
from duplexbench.metrics.latency import agent_utterances, caller_turns, stop_after
from duplexbench.metrics.names import Metric
from duplexbench.scenarios.control_twin import UNMATCHED, align, validity

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord, OverlayRow
    from duplexbench.scenarios.schema import Overlay
    from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000
YIELD_CLASSES = ("Y-INT", "Y-CMD")
HOLD_SPEAKING_CLASSES = ("H-BC", "H-SIDE", "H-BG", "H-TIC")
HOLD_LISTENING_CLASSES = ("P-PAUSE", "H-SIDE", "H-BG", "H-TIC")
ROW_EXCLUSIONS = ("not_injected", "contested")
RESUME_TAG = "resume_ns"

UttText = Callable[[int, int], str]        # (t0_ns, t1_ns) -> agent reference-ASR text


class _Ctx:
    def __init__(self, rec: "CallRecord", th: "Thresholds"):
        self.rec, self.th = rec, th
        self.utts = agent_utterances(rec, th)
        self.aligned = align(rec, rec.twin) if rec.twin is not None else None
        p = rec.playout
        self.t = np.asarray(p["t_ns"], dtype=np.int64) if p is not None and len(p) else np.zeros(0, np.int64)
        self.act = np.asarray(p["active"], dtype=bool) if p is not None and len(p) else np.zeros(0, bool)

    def obs(self, metric: Metric, row: "OverlayRow", value: float | None, tags: list[str],
            valid: bool | None = None) -> Observation:
        excl = [t for t in tags if t in ("not_injected", "ambiguous_end", "unmatched_control",
                                          "contested", "not_stopped", "no_refasr")]
        ok = (value is not None and not excl) if valid is None else valid
        keep = value if (ok or set(excl) <= {"contested"}) else None
        return Observation(metric=str(metric), call_id=self.rec.call_id,
                           scenario_id=getattr(self.rec.scenario, "id", "") or "",
                           trial=self.rec.trial, condition=str(self.rec.condition),
                           system_id=self.rec.system_id, event_id=row.overlay_id,
                           value=None if keep is None else float(keep), valid=ok,
                           tags=tuple(tags))

    def active_ns(self, t0: int, t1: int) -> int:
        """Agent active time at the ear in [t0, t1) (frames whose tick lies in the span)."""
        m = (self.t >= t0) & (self.t < t1) & self.act
        return int(m.sum()) * self.th.frame_ms * _NS_PER_MS

    def any_active(self, t0: int, t1: int) -> bool:
        return bool(((self.t >= t0) & (self.t < t1) & self.act).any())

    def twin_tag(self, row: "OverlayRow", ov: "Overlay", metric: Metric, budget_ms: int | None) -> str | None:
        if self.aligned is None:
            return UNMATCHED
        pair = self.aligned.get(ov.anchor_step)
        ctl = pair[1] if pair is not None else None
        return validity(row, ctl, str(metric), budget_ms, self.th, offset_ms=ov.offset_ms)


def _resume_ns(row: "OverlayRow") -> int | None:
    for t in row.tags:
        k, sep, v = t.partition(":")
        if sep and k == RESUME_TAG:
            return int(v)
    return None


def _base_tags(row: "OverlayRow") -> list[str]:
    return [f"cls:{row.cls}"] + [t for t in row.tags if t in ROW_EXCLUSIONS]


def _with(tags: list[str], *extra: str | None) -> list[str]:
    out = list(tags)
    for e in extra:
        if e and e not in out:
            out.append(e)
    return out


def observe(rec: "CallRecord", th: "Thresholds", *, utt_text: UttText | None = None) -> list[Observation]:
    """Every spec §4.1 floor-control observation of one overlay call (see module docstring)."""
    if rec.is_control:
        return []
    c = _Ctx(rec, th)
    by_id = {o.id: o for o in getattr(rec.scenario, "overlays", [])}
    out: list[Observation] = []
    for row in sorted(rec.overlays, key=lambda r: r.overlay_id):
        ov = by_id.get(row.overlay_id)
        if ov is None:
            continue                                     # e.g. "<id>/resumption" or a runner row
        injected = "not_injected" not in row.tags and row.onset_ns is not None
        base = _base_tags(row)
        if not injected and "not_injected" not in base and row.cls not in ("E-END", "P-PAUSE"):
            base.append("not_injected")
        if row.cls in YIELD_CLASSES:
            out += _yield(c, row, ov, base, injected, utt_text)
        elif row.cls in HOLD_SPEAKING_CLASSES and ov.placement == "during_agent":
            out += _hold_speaking(c, row, ov, base, injected)
        if row.cls in HOLD_LISTENING_CLASSES and (ov.placement == "in_pause" or row.cls == "P-PAUSE"):
            out += _hold_listening(c, row, base, injected)
        if row.cls == "E-END":
            out += _response(c, row, ov, base)
    return out


def _yield(c: _Ctx, row: "OverlayRow", ov: "Overlay", base: list[str], injected: bool,
           utt_text: UttText | None) -> list[Observation]:
    th, out = c.th, []
    s = stop_after(c.rec, th, row.onset_ns) if injected else None
    for b in th.yield_budgets_ms:
        tags = _with([f"@{b}"] + base, c.twin_tag(row, ov, Metric.yield_rate, b) if injected else None)
        v = None if not injected else float(s is not None and s - row.onset_ns <= b * _NS_PER_MS)
        out.append(c.obs(Metric.yield_rate, row, v, tags))
    win = th.hold_window_ms * _NS_PER_MS
    stopped = injected and s is not None and s - row.onset_ns <= win
    tags = _with(base, c.twin_tag(row, ov, Metric.t_stop_ear, None) if injected else None)
    if injected and not stopped:
        tags = _with(tags, "not_stopped")
    v = (max(0, s - row.onset_ns) / _NS_PER_MS) if stopped else None
    out.append(c.obs(Metric.t_stop_ear, row, v, tags))
    # talk-over share: agent active seconds during the overlay / overlay duration
    if injected and row.end_ns is not None and row.end_ns > row.onset_ns:
        share = c.active_ns(row.onset_ns, row.end_ns) / (row.end_ns - row.onset_ns)
        out.append(c.obs(Metric.talk_over_share, row, share, list(base)))
    else:
        out.append(c.obs(Metric.talk_over_share, row, None, list(base)))
    # wait-compliance: wait-class Y-CMD (scripted resumption), no agent frame end -> resumption
    if row.cls == "Y-CMD" and (ov.resumption_ms is not None or _resume_ns(row) is not None):
        r = _resume_ns(row)
        if injected and r is not None and row.end_ns is not None:
            out.append(c.obs(Metric.wait_compliance, row,
                             0.0 if c.any_active(row.end_ns, r) else 1.0, list(base)))
        else:
            out.append(c.obs(Metric.wait_compliance, row, None, list(base)))
    if row.cls == "Y-INT" and stopped:
        out.append(_respond_resume(c, row, ov, s, base, utt_text))
    return out


def _respond_resume(c: _Ctx, row: "OverlayRow", ov: "Overlay", s: int, base: list[str],
                    utt_text: UttText | None) -> Observation:
    nxt = next(((u, e) for u, e in c.utts if u > s), None)
    prior = next(((u, e) for u, e in c.utts if u <= row.onset_ns < e), None)
    if utt_text is None or nxt is None:
        tags = _with(["classifier"] + base, "no_refasr" if utt_text is None else None)
        return c.obs(Metric.respond_resume, row, None, tags, valid=False)
    from duplexbench.classifiers.respond_resume import observation
    o = observation(call_id=c.rec.call_id, scenario_id=c.rec.scenario.id, trial=c.rec.trial,
                    condition=str(c.rec.condition), system_id=c.rec.system_id,
                    event_id=row.overlay_id, utt_text=utt_text(*nxt), anchors=ov.response_anchors,
                    prior_utt_text=utt_text(*prior) if prior else None, th=c.th)
    tags = tuple(o.tags) + tuple(t for t in base if t not in o.tags)
    contested = "contested" in base
    return Observation(**{**o.__dict__, "tags": tags, "valid": o.valid and not contested})


def _hold_speaking(c: _Ctx, row: "OverlayRow", ov: "Overlay", base: list[str],
                   injected: bool) -> list[Observation]:
    th, out = c.th, []
    s = stop_after(c.rec, th, row.onset_ns) if injected else None
    tag = c.twin_tag(row, ov, Metric.hold_speaking_rate, None) if injected else None
    for w in (th.hold_window_ms, th.hold_also_ms):
        v = None if not injected else float(s is None or s - row.onset_ns > w * _NS_PER_MS)
        out.append(c.obs(Metric.hold_speaking_rate, row, v, _with([f"@{w}"] + base, tag)))
    out.append(c.obs(Metric.content_leakage_rate, row, None, _with(base, "no_refasr"), valid=False))
    return out


def _hold_listening(c: _Ctx, row: "OverlayRow", base: list[str], injected: bool) -> list[Observation]:
    r = _resume_ns(row)
    if r is None and row.cls == "P-PAUSE":
        r = row.end_ns
    if not injected or r is None:
        return [c.obs(Metric.hold_listening_rate, row, None, _with(base, "not_injected"))]
    started = any(row.onset_ns <= u < r for u, _ in c.utts)
    return [c.obs(Metric.hold_listening_rate, row, 0.0 if started else 1.0, list(base))]


def _response(c: _Ctx, row: "OverlayRow", ov: "Overlay", base: list[str]) -> list[Observation]:
    turn = next(((on, end) for sid, on, end in caller_turns(c.rec) if sid == ov.anchor_step), None)
    out = []
    for w in c.th.response_windows_ms:
        tags = [f"@{w}"] + base
        if turn is None:
            out.append(c.obs(Metric.response_rate, row, None, tags, valid=False))
            continue
        on, end = turn
        ref = on if on is not None else end
        nxt = next((u for u, _ in c.utts if u > ref), None)
        out.append(c.obs(Metric.response_rate, row,
                         float(nxt is not None and nxt - end <= w * _NS_PER_MS), tags))
    return out
