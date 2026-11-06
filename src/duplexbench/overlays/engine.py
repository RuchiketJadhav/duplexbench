"""Live overlay engine (BUILD_PLAN M2-T05; spec §3.2 triggering, §3.3 contested bucket, §1.5 anchors).

    playout.subscribe ──onset/stop──►  OverlayEngine.run()  (one accounted task, frame grid)
    ctx.turn_end_ns   ──turn ends───►      │ during_agent: agent onset of the turn after
    caller.segments   ──pause geometry─►   │   anchor_step + offset_ms  -> caller.inject(...)
                                           │ in_pause: pause start + offset_ms
                                           │ Y-CMD wait-class: resumption clip after resumption_ms
                                           └─► `overlays` rows + `overlay_*` events (on exit)

Rules
-----
* during_agent: the trigger is the first agent utterance (ear onset, spec §1.2) that
  begins at or after the anchor step's caller turn end and before the next caller
  turn end (the same pairing as `scenarios.control_twin.agent_turns_from`). The
  overlay is scheduled at `onset + offset_ms`. At that time, if the trigger
  utterance's stop has already been seen (the live segmenter reports a stop
  `inactivity_run_ms` late, carrying the run start) or the agent never started
  before the next caller turn, the overlay is `not_injected`: no audio, row tagged.
  An utterance whose inactivity run began less than `inactivity_run_ms` before the
  scheduled time is still injected (live detection cannot know yet); the control
  twin's `ambiguous_end` validity covers that case after the run (spec §3.4).
* in_pause: scheduled at the anchor step's pause start (send time) + `offset_ms`.
  P-PAUSE carries no clip: its row records the pause geometry (onset = pause start,
  end = resumption start). In-pause H-* clips are injected ahead of time.
* E-END: geometry only; its row records the anchor step's caller turn end.
* Y-CMD whose text is a wait-class token (langpack `wait_class`) with `resumption_ms`:
  the clip `clips["<overlay_id>/resumption"]` is injected `resumption_ms` after the
  command's end, as overlay id `<overlay_id>/resumption`.
* contested (spec §3.3, tagged after the run): |actual − scheduled| >
  `contested_offset_ms`; an H-BC injected when the agent's reference-ASR text in the
  1 s before injection ends in "?" (only when `refasr_text` is given; reference ASR
  arrives in M4); a pause longer than `contested_pause_ms`; a one-word Y-INT outside
  the Y-CMD list.

Row columns (`overlays` table): `scheduled_ns` (scheduled send time), `actual_ns`
(send time of the first frame carrying the clip; None when not injected),
`onset_ns` / `end_ns` (first / last+1 active frame of the clip at send time, spec
§1.5 "overlay onset / end"; the pause span for P-PAUSE; None when not injected),
`tags_json` (`not_injected`, `contested`, and `resume_ns:<int>` for the scripted
resumption of a wait-class Y-CMD or an in-pause overlay). The agent onset that
triggered a during_agent overlay and the actual-vs-scheduled offsets are in the
`overlay_injected` / `overlay_not_injected` events (payload `agent_onset_ns`,
`scheduled_offset_ms`, `actual_offset_ms`).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from duplexbench.metrics.activity import frame_rms_dbfs, is_active
from duplexbench.storage import canonical_json

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext
    from duplexbench.playout import Playout
    from duplexbench.scenarios.schema import Overlay, Scenario
    from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000
_FULL_SCALE = 32768.0
_REF_DBFS = -20.0             # same reference as ScriptedCaller.inject (clip RMS before level_db)
HBC_QUESTION_LOOKBACK_MS = 1000   # threshold-ok: spec §3.3 "in the 1 s before injection" (definition)
NOT_INJECTED = "not_injected"
CONTESTED = "contested"
RESUME_TAG = "resume_ns"

RefasrText = Callable[[int, int], str]     # (t0_ns, t1_ns) -> agent reference-ASR text


def resumption_clip_key(overlay_id: str) -> str:
    """Key of a wait-class Y-CMD's resumption clip in the engine's `clips` mapping."""
    return f"{overlay_id}/resumption"


@dataclass
class _Ov:
    ov: "Overlay"
    step_idx: int
    state: str = "pending"            # pending -> armed -> done
    scheduled_ns: int | None = None
    actual_ns: int | None = None
    onset_ns: int | None = None
    end_ns: int | None = None
    agent_onset_ns: int | None = None
    trigger: int | None = None        # index into the engine's utterance list
    resume_at: int | None = None      # wait-class resumption (send time) still to inject
    resume_ns: int | None = None
    tags: list[str] = field(default_factory=list)


class OverlayEngine:
    """One call's overlay scheduler; `run(ctx, caller, playout)` is the runner's `overlays` task."""

    def __init__(self, clock: "Clock", scenario: "Scenario", playout: "Playout", caller: Any,
                 store: Any, th: "Thresholds", *, clips: Mapping[str, np.ndarray],
                 ycmd_tokens: Sequence[str] = (), wait_class: Sequence[str] = (),
                 send_delay_ns: int = 0, refasr_text: RefasrText | None = None):
        self.clock, self.scenario, self.playout, self.caller = clock, scenario, playout, caller
        self.store, self.th, self.clips = store, th, clips
        self.ycmd = {_norm(t) for t in ycmd_tokens}
        self.wait = {_norm(t) for t in wait_class}
        self.send_delay_ns = int(send_delay_ns)
        self.refasr_text = refasr_text
        self.frame_ns = th.frame_ms * _NS_PER_MS
        steps = [st.id for st in scenario.steps]
        self.items = [_Ov(o, steps.index(o.anchor_step) if o.anchor_step in steps else -1)
                      for o in scenario.overlays]
        self.utts: list[list[int | None]] = []        # [onset_ns, stop_ns | None]
        self.ctx: "CallContext | None" = None
        self.rows: list[dict] = []
        self._geometry_done = False

    # -- live -------------------------------------------------------------------------
    def _on_activity(self, kind: str, frame_idx: int, t_ns: int) -> None:
        del frame_idx
        if kind == "onset":
            self.utts.append([t_ns, None])
        elif kind == "stop" and self.utts and self.utts[-1][1] is None:
            self.utts[-1][1] = t_ns

    async def run(self, ctx: "CallContext", caller: Any = None, playout: "Playout | None" = None) -> None:
        self.ctx = ctx
        if caller is not None:
            self.caller = caller
        if playout is not None:
            self.playout = playout
        self.playout.subscribe(self._on_activity)
        t0 = self.clock.now_ns()
        k = 0
        try:
            while True:
                await self.clock.sleep_until_ns(t0 + k * self.frame_ns)
                k += 1
                self._step(self.clock.now_ns())
        finally:
            self.finish()

    def _step(self, now: int) -> None:
        if not self._geometry_done and getattr(self.caller, "start_ns", None) is not None:
            self._geometry()
        ends = list(self.ctx.turn_end_ns) if self.ctx is not None else []
        for it in self.items:
            if it.state == "done" and it.resume_at is not None and now >= it.resume_at:
                self._inject_resumption(it)
            if it.ov.placement != "during_agent" or it.ov.cls in ("E-END", "P-PAUSE"):
                continue
            if it.state == "pending":
                self._arm(it, ends, now)
            if it.state == "armed" and now >= it.scheduled_ns:
                self._fire(it, now)

    def _arm(self, it: _Ov, ends: list[int], now: int) -> None:
        i = it.step_idx
        if i < 0 or len(ends) <= i:
            return
        te, nxt = ends[i], (ends[i + 1] if len(ends) > i + 1 else None)
        for j, (on, _stop) in enumerate(self.utts):
            if on >= te and (nxt is None or on < nxt):
                it.trigger, it.agent_onset_ns = j, on
                it.scheduled_ns = on + it.ov.offset_ms * _NS_PER_MS
                it.state = "armed"
                return
        if nxt is not None and now >= nxt:
            self._not_injected(it, "no agent onset before the next caller turn end")

    def _fire(self, it: _Ov, now: int) -> None:
        stop = self.utts[it.trigger][1]
        if stop is not None:
            self._not_injected(it, "agent utterance ended before the offset")
            return
        self._inject(it, it.scheduled_ns)

    def _inject(self, it: _Ov, at_send_ns: int) -> None:
        ov = it.ov
        clip = self.clips.get(ov.id)
        if clip is None:
            raise KeyError(f"{self.scenario.id}: no clip for overlay {ov.id!r}")
        oid = self.caller.inject(clip, ov.level_db, t_ns=at_send_ns - self.send_delay_ns,
                                 overlay_id=ov.id)
        src = getattr(self.caller, "injected_at", {}).get(oid, at_send_ns - self.send_delay_ns)
        it.actual_ns = src + self.send_delay_ns
        a, b = self._active_span(clip, ov.level_db)
        it.onset_ns, it.end_ns = it.actual_ns + a, it.actual_ns + b
        it.state = "done"
        if ov.cls == "Y-CMD" and _norm(ov.text) in self.wait and ov.resumption_ms is not None:
            it.resume_at = it.end_ns + ov.resumption_ms * _NS_PER_MS
        self._event(self.clock.now_ns(), "overlay_injected", it)

    def _inject_resumption(self, it: _Ov) -> None:
        key = resumption_clip_key(it.ov.id)
        at, it.resume_at = it.resume_at, None
        clip = self.clips.get(key)
        if clip is None:
            raise KeyError(f"{self.scenario.id}: wait-class {it.ov.id!r} needs clip {key!r}")
        oid = self.caller.inject(clip, 0.0, t_ns=at - self.send_delay_ns, overlay_id=key)
        src = getattr(self.caller, "injected_at", {}).get(oid, at - self.send_delay_ns)
        a, _ = self._active_span(clip, 0.0)
        it.resume_ns = src + self.send_delay_ns + a
        self._log(self.clock.now_ns(), "overlay_resumption",
                  {"overlay_id": it.ov.id, "resumption_id": key, "resume_ns": it.resume_ns})

    def _not_injected(self, it: _Ov, why: str) -> None:
        it.state = "done"
        if NOT_INJECTED not in it.tags:
            it.tags.append(NOT_INJECTED)
        self._event(self.clock.now_ns(), "overlay_not_injected", it, why)

    def _geometry(self) -> None:
        """Pause / end-of-turn geometry from the scripted caller's segments (send times)."""
        self._geometry_done = True
        segs = getattr(self.caller, "segments", None) or []
        start = self.caller.start_ns + self.send_delay_ns
        for it in self.items:
            ov = it.ov
            if ov.placement != "in_pause":
                continue
            pause = next((s for s in segs if s.kind == "pause" and s.step_id == ov.anchor_step), None)
            res = next((s for s in segs if s.kind == "resumption" and s.step_id == ov.anchor_step), None)
            if pause is None or res is None:
                self._not_injected(it, "anchor step has no pause")
                continue
            p0 = start + pause.start_frame * self.frame_ns
            r0 = start + res.start_frame * self.frame_ns
            it.scheduled_ns = p0 + ov.offset_ms * _NS_PER_MS
            it.resume_ns = r0
            if ov.cls == "P-PAUSE" or ov.id not in self.clips:
                it.actual_ns, it.onset_ns, it.end_ns = it.scheduled_ns, p0, r0
                it.state = "done"
            else:
                self._inject(it, it.scheduled_ns)

    def _active_span(self, clip: np.ndarray, level_db: float) -> tuple[int, int]:
        """(first active frame start, last active frame end) of the scaled clip, ns from its start."""
        x = np.asarray(clip, dtype=np.float64).ravel()
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        if rms <= 0:
            return 0, 0
        y = x * (_FULL_SCALE * 10 ** ((_REF_DBFS + level_db) / 20) / rms)
        spf = int(getattr(self.playout, "frame_len", 0)) or int(getattr(self.caller, "spf", 160))
        n = -(-len(y) // spf)
        act = [is_active(frame_rms_dbfs(y[i * spf:(i + 1) * spf]), self.th) for i in range(n)]
        if not any(act):
            return 0, n * self.frame_ns
        a = act.index(True)
        b = n - act[::-1].index(True)
        return a * self.frame_ns, b * self.frame_ns

    # -- after the call -------------------------------------------------------------------
    def finish(self) -> list[dict]:
        """Tag what never fired, apply the contested rules (spec §3.3), write the rows. Idempotent."""
        if self.rows:
            return self.rows
        ends = list(self.ctx.turn_end_ns) if self.ctx is not None else []
        for it in self.items:
            ov = it.ov
            if ov.cls == "E-END":
                if 0 <= it.step_idx < len(ends):
                    it.scheduled_ns = it.actual_ns = ends[it.step_idx]
                it.state = "done"
            elif it.state != "done":
                self._not_injected(it, "call ended before the overlay fired")
            self._contested(it)
            tags = list(it.tags)
            if it.resume_ns is not None:
                tags.append(f"{RESUME_TAG}:{it.resume_ns}")
            row = {"run_id": self._ids()[0], "call_id": self._ids()[1], "overlay_id": ov.id,
                   "cls": ov.cls, "scheduled_ns": it.scheduled_ns, "actual_ns": it.actual_ns,
                   "onset_ns": it.onset_ns, "end_ns": it.end_ns, "tags_json": canonical_json(tags)}
            self.rows.append(row)
            if self.store is not None:
                self.store.put("overlays", row)
        return self.rows

    def _contested(self, it: _Ov) -> None:
        ov, th = it.ov, self.th
        hit = False
        if NOT_INJECTED not in it.tags and it.actual_ns is not None and it.scheduled_ns is not None \
                and ov.cls not in ("P-PAUSE", "E-END") \
                and abs(it.actual_ns - it.scheduled_ns) > th.contested_offset_ms * _NS_PER_MS:
            hit = True
        if ov.cls == "H-BC" and self.refasr_text is not None and it.actual_ns is not None:
            txt = self.refasr_text(it.actual_ns - HBC_QUESTION_LOOKBACK_MS * _NS_PER_MS,
                                   it.actual_ns) or ""
            hit = hit or txt.rstrip().endswith("?")
        if ov.placement == "in_pause" or ov.cls == "P-PAUSE":
            step = next((s for s in self.scenario.steps if s.id == ov.anchor_step), None)
            if step is not None and step.pause_ms is not None and step.pause_ms > th.contested_pause_ms:
                hit = True
        if ov.cls == "Y-INT" and len(ov.text.split()) == 1 and _norm(ov.text) not in self.ycmd:
            hit = True
        if hit and CONTESTED not in it.tags:
            it.tags.append(CONTESTED)

    # -- logging ------------------------------------------------------------------------
    def _ids(self) -> tuple[str, str]:
        if self.ctx is not None:
            return self.ctx.run_id, self.ctx.call_id
        return getattr(self.playout, "run_id", ""), getattr(self.playout, "call_id", "")

    def _event(self, t_ns: int, kind: str, it: _Ov, why: str | None = None) -> None:
        p: dict[str, Any] = {"overlay_id": it.ov.id, "cls": it.ov.cls,
                             "scheduled_ns": it.scheduled_ns, "agent_onset_ns": it.agent_onset_ns,
                             "actual_ns": it.actual_ns}
        if it.agent_onset_ns is not None and it.scheduled_ns is not None:
            p["scheduled_offset_ms"] = (it.scheduled_ns - it.agent_onset_ns) / _NS_PER_MS
        if it.agent_onset_ns is not None and it.actual_ns is not None:
            p["actual_offset_ms"] = (it.actual_ns - it.agent_onset_ns) / _NS_PER_MS
        if why:
            p["reason"] = why
        self._log(t_ns, kind, p)

    def _log(self, t_ns: int, kind: str, payload: dict) -> None:
        if self.store is None:
            return
        run_id, call_id = self._ids()
        seq = self.playout.next_seq() if hasattr(self.playout, "next_seq") else 0
        self.store.put("events", {"run_id": run_id, "call_id": call_id, "t_ns": int(t_ns),
                                  "seq": seq, "kind": kind, "src": "overlay_engine",
                                  "payload_json": canonical_json(payload)})


def _norm(s: str) -> str:
    return " ".join(s.lower().strip().split())


def make_engine(clock: "Clock", scenario: "Scenario", playout: "Playout", caller: Any, store: Any,
                th: "Thresholds", clips: Mapping[str, np.ndarray], langpack: Any = None,
                **kw: Any) -> OverlayEngine:
    """OverlayEngine with the langpack's Y-CMD / wait-class lists (None -> spec defaults file)."""
    if langpack is None:
        from pathlib import Path

        from duplexbench.stimuli.langpack import LangPack
        root = Path(__file__).resolve().parents[3] / "configs" / "langpacks" / "en-US"
        langpack = LangPack.load(root)
    return OverlayEngine(clock, scenario, playout, caller, store, th, clips=clips,
                         ycmd_tokens=langpack.ycmd_tokens, wait_class=langpack.wait_class, **kw)
