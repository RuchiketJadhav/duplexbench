"""CallRunner: one call, four accounted tasks (BUILD_PLAN §2.7; spec §1.1, §1.3, §4.2 call outcome).

    run_call(ctx, adapter, caller, overlays, playout, channel, tools, budget) -> CallResult

Tasks (each spawned with `clock.task`; see src/duplexbench/README.md):

    _send_caller  pulls paced frames from `caller.frames()`, runs them through
                  `channel.forward`, sends them with `adapter.send_frame`, writes the
                  left channel, logs caller turn onsets/ends and overlay onsets/ends at
                  send time (the forward chain's fixed `delay_frames` is accounted for)
    _recv_agent   iterates `adapter.safe_events(ctx)`: audio -> channel.back ->
                  playout.enqueue; cancel_or_truncate -> playout.flush(t); tool_call ->
                  tools.call (or a stub result when `tools is None`) -> adapter.tool_result;
                  every event is logged
    _play         `playout.run()` (ear WAV, playout rows, onset/stop)
    _overlays     `overlays.run(ctx, caller, playout)` when an overlay engine is given (M2)

plus a small helper task `_turns` that consumes `caller.turn_ends()` and turns each
`(step_id, t_src)` into `ctx.turn_ended(t_send)` at the frame's send time.

The calling coroutine supervises on the frame grid and ends the call on the first of:

    adapter closed            -> completed (transport_error if the stream reported an error)
    caller script exhausted   -> completed
    E-END silence             -> completed: every caller turn delivered and neither channel
                                 active for `eend_silence_ms`
    call_timeout_ms           -> timeout
    stall_ms                  -> stalled: neither channel active for `stall_ms`, outside a tool wait
                                 and outside a scripted caller's agent_first lead-in (fix-34)
    any exception in any task -> transport_error

Budget refusal at admission (`budget.reserve` before `adapter.open`) returns
`budget_refused` and never starts the call. On every exit: an `error` event if one
occurred, the other tasks cancelled, `adapter.close()`, the WAV closed, the
`calls` row written and `await store.flush()` (directly, never through
`clock.idle`, see clock.py).

Overlay rows: when `overlays is None` the runner writes one `overlays` row per
injected clip it observed (onset/end at send time, cls ""); an overlay engine
(M2) writes its own rows.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from duplexbench.budget import BudgetExceeded
from duplexbench.storage import canonical_json

if TYPE_CHECKING:
    from duplexbench.adapters.base import Adapter, AdapterEvent
    from duplexbench.budget import Budget
    from duplexbench.channel.chain import ChannelChain
    from duplexbench.context import CallContext
    from duplexbench.playout import Playout
    from duplexbench.stimuli.caller_source import CallerSource, Frame
    from duplexbench.thresholds import Thresholds

_NS_PER_MS = 1_000_000
_P99 = 99


class ToolClientLike(Protocol):
    async def call(self, name: str, args: dict, tool_call_id: str | None = None) -> dict: ...


class OverlayEngineLike(Protocol):
    async def run(self, ctx: "CallContext", caller: "CallerSource", playout: "Playout") -> None: ...


@dataclass
class CallResult:
    outcome: str
    usd_actual: float | None
    flags: dict = field(default_factory=dict)


@dataclass
class _State:
    start: int = 0
    error: dict | None = None            # first exception / error event
    adapter_closed: bool = False
    caller_done: bool = False
    turns_done: bool = False
    last_activity: int = 0
    tools_pending: int = 0
    sent_at: dict[int, int] = field(default_factory=dict)       # source t_ns -> send t_ns
    turn_end_src: dict[int, str] = field(default_factory=dict)  # source t_ns -> step_id
    cur_step: str | None = None
    cur_overlay: str | None = None
    overlay_onset: int | None = None
    last_overlay_t: int | None = None
    overlay_rows: list[dict] = field(default_factory=list)
    slips_ns: list[int] = field(default_factory=list)
    frames_sent: int = 0
    events_seen: int = 0


class _Runner:
    def __init__(self, ctx, adapter, caller, overlays, playout, channel, tools, th):
        self.ctx, self.adapter, self.caller = ctx, adapter, caller
        self.overlays, self.playout, self.channel, self.tools = overlays, playout, channel, tools
        self.th = th
        self.clock = ctx.clock
        self.store = ctx.store
        self.wav = playout.wav
        self.frame_ns = th.frame_ms * _NS_PER_MS
        self.s = _State()
        self.delivery = channel.delivery(adapter.caps.native_mulaw)
        self.side_tasks: list[asyncio.Task] = []

    # -- logging --------------------------------------------------------------------
    def log(self, t_ns: int, kind: str, src: str, payload: dict | None = None) -> None:
        self.store.put("events", {
            "run_id": self.ctx.run_id, "call_id": self.ctx.call_id, "t_ns": int(t_ns),
            "seq": self.playout.next_seq(), "kind": kind, "src": src,
            "payload_json": canonical_json(payload or {})})

    def fail(self, where: str, e: BaseException) -> None:
        if self.s.error is None:
            self.s.error = {"type": type(e).__name__, "message": str(e), "task": where}
            self.log(self.clock.now_ns(), "error", "harness", self.s.error)

    async def guard(self, where: str, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001 - any task failure ends the call as transport_error
            self.fail(where, e)

    # -- tasks ------------------------------------------------------------------------
    async def send_caller(self) -> None:
        ch, s = self.channel, self.s
        delayq: deque["Frame"] = deque()
        async for fr in self.caller.frames():
            now = self.clock.now_ns()
            s.slips_ns.append(now - fr.t_ns)
            delayq.append(fr)
            for payload in ch.forward(fr.pcm, ch.rate, [fr.speech]):
                src = delayq.popleft()
                t = now
                if self.delivery == "mulaw":
                    await self.adapter.send_frame(payload, t)
                else:
                    await self.adapter.send_frame(ch.decode(payload).tobytes(), t)
                s.frames_sent += 1
                if self.wav is not None:
                    self.wav.write_left(ch.decode(payload), t)
                self._caller_truth(src, t)
        s.caller_done = True

    def _caller_truth(self, src: "Frame", t: int) -> None:
        s = self.s
        s.sent_at[src.t_ns] = t
        if src.speech:
            s.last_activity = max(s.last_activity, t)
        if src.speech and src.step_id is not None and src.step_id != s.cur_step:
            s.cur_step = src.step_id
            self.log(t, "caller_turn_onset", "caller", {"step_id": src.step_id})
        for t_src in [x for x in s.turn_end_src if src.t_ns <= x < src.t_ns + self.frame_ns]:
            self._turn_end(s.turn_end_src.pop(t_src), t)
        ov = src.overlay_id if src.speech else None
        if s.cur_overlay is not None and ov != s.cur_overlay:
            self._overlay_end()
        if ov is not None and ov != s.cur_overlay:
            s.cur_overlay, s.overlay_onset = ov, t
            self.log(t, "overlay_onset", "caller", {"overlay_id": ov})
        if ov is not None:
            s.last_overlay_t = t

    def _overlay_end(self) -> None:
        s = self.s
        self.log(s.last_overlay_t, "overlay_end", "caller", {"overlay_id": s.cur_overlay})
        s.overlay_rows.append({"overlay_id": s.cur_overlay, "onset_ns": s.overlay_onset,
                               "end_ns": s.last_overlay_t})
        s.cur_overlay = s.overlay_onset = s.last_overlay_t = None

    def _turn_end(self, step_id: str, t_send: int) -> None:
        self.ctx.turn_ended(t_send)
        self.log(t_send, "caller_turn_end", "caller", {"step_id": step_id})
        if self.s.cur_step == step_id:
            self.s.cur_step = None

    async def turns(self) -> None:
        s = self.s
        async for step_id, t_src in self.caller.turn_ends():
            # a turn end inside a frame's span (a clip's speech end) maps to that frame
            f_src = t_src if t_src in s.sent_at else None
            if f_src is None and s.sent_at:
                last = max(s.sent_at)
                if last <= t_src < last + self.frame_ns:
                    f_src = last
            if f_src is not None:                  # already on the line (no forward delay)
                self._turn_end(step_id, s.sent_at[f_src])
            else:
                s.turn_end_src[t_src] = step_id
        s.turns_done = True

    async def recv_agent(self) -> None:
        s = self.s
        async for ev in self.adapter.safe_events(self.ctx):
            s.events_seen += 1
            self._log_event(ev)
            if ev.kind == "audio":
                pcm, rate = ev.payload["pcm"], int(ev.payload["rate"])
                for fr in self.channel.back(pcm, rate):
                    self.playout.enqueue(fr, ev.t_ns)
            elif ev.kind == "cancel_or_truncate":
                self.playout.flush(ev.t_ns, "cancel_or_truncate")
            elif ev.kind == "tool_call":
                s.tools_pending += 1
                self.side_tasks.append(self.clock.task(self.guard("tool", self._tool(ev))))
            elif ev.kind == "error":
                if s.error is None:
                    s.error = dict(ev.payload, task="adapter")
            elif ev.kind == "closed":
                s.adapter_closed = True
                return

    def _log_event(self, ev: "AdapterEvent") -> None:
        p = ev.payload
        if ev.kind == "audio":
            p = {"samples": int(np.asarray(p["pcm"]).size), "rate": int(p["rate"])}
        self.log(ev.t_ns, ev.kind, "agent", dict(p, provider_event=ev.provider_event))

    async def _tool(self, ev: "AdapterEvent") -> None:
        tid, name = str(ev.payload.get("id")), str(ev.payload.get("name"))
        args = ev.payload.get("arguments") or {}
        try:
            if self.tools is None:
                result = {"status": "no_tool_server"}
            else:
                result = await self.tools.call(name, args, tid)
            t_res = self.clock.now_ns()
            self.log(t_res, "tool_result", "harness", {"id": tid, "name": name})
            # The ToolServer writes the `tool_calls` row of every call it served (one row,
            # same shape as the HTTP path); the runner writes one only when no server did.
            served = self.tools is not None and not (
                isinstance(result, dict) and result.get("error") == "unknown_tool")
            if not served:
                self.store.put("tool_calls", {
                    "run_id": self.ctx.run_id, "call_id": self.ctx.call_id, "tool_call_id": tid,
                    "name": name, "args_json": canonical_json(args), "start_ns": ev.t_ns,
                    "result_ns": t_res, "injected_latency_ms": None, "injected_failure": None,
                    "result_json": canonical_json(result)})
            await self.adapter.tool_result(tid, result)
        finally:
            self.s.tools_pending -= 1

    # -- supervision ---------------------------------------------------------------------
    def check(self, now: int) -> str | None:
        s, th = self.s, self.th
        if self.playout.last_active_ns is not None:
            s.last_activity = max(s.last_activity, self.playout.last_active_ns)
        if s.tools_pending > 0:
            s.last_activity = now
        if getattr(self.caller, "waiting_lead_in", False):   # fix-34: the lead-in has its own guard
            s.last_activity = now
        if s.error is not None:
            return "transport_error"
        if s.adapter_closed:
            return "completed"
        if s.caller_done:
            return "completed"
        if now - s.start >= th.call_timeout_ms * _NS_PER_MS:
            return "timeout"
        idle = now - s.last_activity
        if s.turns_done and not s.turn_end_src and idle >= th.eend_silence_ms * _NS_PER_MS:
            return "completed"
        if idle >= th.stall_ms * _NS_PER_MS:
            return "stalled"
        return None


async def run_call(ctx: "CallContext", adapter: "Adapter", caller: "CallerSource",
                   overlays: "OverlayEngineLike | None", playout: "Playout",
                   channel: "ChannelChain", tools: "ToolClientLike | None",
                   budget: "Budget | None", *, th: "Thresholds | None" = None,
                   projected_usd: float = 0.0) -> CallResult:
    """Run one call to its end (BUILD_PLAN §2.7). Never raises for call-level failures."""
    if th is None:
        th = playout.th
    r = _Runner(ctx, adapter, caller, overlays, playout, channel, tools, th)
    clock, s = ctx.clock, r.s
    s.start = s.last_activity = clock.now_ns()
    flags: dict[str, Any] = {"delivery": r.delivery, "forward_delay_frames": channel.delay_frames}
    reservation = None
    if budget is not None:
        try:
            reservation = budget.reserve(projected_usd)
        except BudgetExceeded as e:
            r.log(s.start, "budget_refused", "harness", {"message": str(e)})
            flags["budget"] = str(e)
            _calls_row(ctx, "budget_refused", s.start, s.start, None, projected_usd, None, flags)
            await ctx.store.flush()
            return CallResult("budget_refused", None, flags)
    r.log(s.start, "call_start", "harness", {"condition": str(ctx.condition),
                                            "delivery": r.delivery})
    tasks: list[asyncio.Task] = []
    outcome = "transport_error"
    try:
        await adapter.open(ctx)
        tasks = [clock.task(r.guard("play", playout.run())),
                 clock.task(r.guard("recv_agent", r.recv_agent())),
                 clock.task(r.guard("turns", r.turns())),
                 clock.task(r.guard("send_caller", r.send_caller()))]
        if overlays is not None:
            tasks.append(clock.task(r.guard("overlays", overlays.run(ctx, caller, playout))))
        k = 1
        while True:
            await clock.sleep_until_ns(s.start + k * r.frame_ns)
            k += 1
            res = r.check(clock.now_ns())
            if res is not None:
                outcome = res
                break
    except Exception as e:   # noqa: BLE001 - open() or supervision failure
        r.fail("run_call", e)
        outcome = "transport_error"
    end = clock.now_ns()
    if s.cur_overlay is not None:
        r._overlay_end()
    playout.stop()
    tasks += r.side_tasks
    for t in tasks:
        t.cancel()
    if tasks:
        await clock.idle(asyncio.gather(*tasks, return_exceptions=True))
    try:
        await adapter.close()
    except Exception as e:   # noqa: BLE001
        r.fail("close", e)
        outcome = "transport_error"
    if not s.adapter_closed and tasks:     # the harness ended the session: log its close
        r.log(clock.now_ns(), "closed", "harness", {"by": "harness"})
    if s.error is not None:
        outcome = "transport_error"
        flags["error"] = s.error
    wav_sha = playout.wav.close() if playout.wav is not None else None
    flags["wav_sha256"] = wav_sha
    flags["frames_sent"] = s.frames_sent
    flags["frames_played"] = playout.frames_played
    lead_in = getattr(caller, "lead_in", None)
    if lead_in is not None:                 # fix-34 agent_first: greeting disclosure
        flags.update(lead_in.flags(s.start))
    if s.slips_ns:
        flags["pacing_slip_p99_ms"] = float(np.percentile(s.slips_ns, _P99)) / _NS_PER_MS
    if overlays is None:
        for row in s.overlay_rows:
            ctx.store.put("overlays", {
                "run_id": ctx.run_id, "call_id": ctx.call_id, "overlay_id": row["overlay_id"],
                "cls": "", "scheduled_ns": None, "actual_ns": row["onset_ns"],
                "onset_ns": row["onset_ns"], "end_ns": row["end_ns"], "tags_json": "[]"})
    usd_actual = 0.0
    if budget is not None and reservation is not None:
        budget.settle(reservation, usd_actual)
    r.log(end, "call_end", "harness", {"outcome": outcome})
    wav_path = str(playout.wav.path) if playout.wav is not None else None
    _calls_row(ctx, outcome, s.start, end, wav_path, projected_usd, usd_actual, flags)
    await ctx.store.flush()
    return CallResult(outcome, usd_actual, flags)


def _calls_row(ctx: "CallContext", outcome: str, start: int, end: int, wav_path: str | None,
               usd_projected: float, usd_actual: float | None, flags: dict) -> None:
    ctx.store.put("calls", {
        "call_id": ctx.call_id, "run_id": ctx.run_id,
        "scenario_id": getattr(ctx.scenario, "id", None) or "", "trial": ctx.trial,
        "condition": str(ctx.condition), "system_id": ctx.system_id,
        "is_control": int(ctx.is_control), "outcome": outcome, "started_ns": start,
        "ended_ns": end, "wav_path": wav_path, "usd_projected": usd_projected,
        "usd_actual": usd_actual, "flags_json": canonical_json(flags)})
