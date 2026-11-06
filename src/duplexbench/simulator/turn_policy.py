"""Simulated-caller turn policy (BUILD_PLAN M4-T07; spec §7).

States and transitions (times from the start of agent ear inactivity, `silence_ns`):

    AGENT_SPEAKING --(sim_speculate_ms of inactivity)--> SPECULATING   (start generating)
    SPECULATING    --(sim_eot_ms of inactivity)-------> FLOOR_WAIT    (draw d ~ U(sim_floor_delay_ms))
    FLOOR_WAIT     --(floor time reached, reply ready)-> SPEAKING      (start_speaking)
    SPEAKING       --(finish_speaking)----------------> AGENT_SPEAKING (wait for the agent)

    floor_ns = max(silence_ns + sim_eot_ms + d, tool_release_ns)
    tool_release_ns = result time of the last tool + sim_patience_ms; while any tool is
    pending the floor is blocked (spec §7 tool-wait patience rule)

    on_agent_active() before SPEAKING  -> speculation discarded, back to AGENT_SPEAKING
    on_agent_active() while SPEAKING   -> `unscripted_overlap` recorded; the caller
                                          finishes its utterance (no spontaneous yield)

After the caller finishes, the policy waits for agent speech; only an agent
active -> inactive transition starts the next end-of-turn timer (if the agent
spoke during the caller's utterance and is silent at the end, its silence
start is used). Nothing else re-prompts a silent agent: the runner's
`stall_ms` ends such a call.

Times: every method takes an optional `t_ns`, defaulting to `clock.now_ns()`.
`think_ms` per turn = caller speech onset - agent silence start (the agent
turn end on the ear), i.e. T_caller_think; `late_ms` = onset - floor time
when the reply was not ready at the floor time (spec §7).

Driving it (simulator task, all waits on the clock):

    while True:
        a = policy.next_action()
        if isinstance(a, Speak) and reply_ready:
            rec = policy.start_speaking(); ...play...; policy.finish_speaking()
        elif isinstance(a, Wait) and a.until_ns is not None:
            await clock.sleep_until_ns(a.until_ns)      # or idle on an activity event
        else:
            await clock.idle(activity_event.wait())
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from duplexbench.clock import Clock
from duplexbench.thresholds import Thresholds

NS_PER_MS = 1_000_000
UNSCRIPTED_OVERLAP = "unscripted_overlap"


class TurnState(StrEnum):
    AGENT_SPEAKING = "AGENT_SPEAKING"
    SPECULATING = "SPECULATING"
    FLOOR_WAIT = "FLOOR_WAIT"
    SPEAKING = "SPEAKING"


@dataclass(frozen=True)
class Speak:
    floor_ns: int
    text_ready_required: bool = True
    speculative: bool = False      # generation started at SPECULATING and survived


@dataclass(frozen=True)
class Wait:
    until_ns: int | None           # next time the action may change; None = wait for an event


@dataclass
class TurnRecord:
    turn_idx: int
    silence_ns: int | None
    floor_ns: int
    start_ns: int
    think_ms: float | None
    late_ms: float
    speculative: bool
    floor_delay_ms: float
    end_ns: int | None = None


class TurnPolicy:
    def __init__(self, clock: Clock, th: Thresholds, rng_seed: int) -> None:
        self.clock = clock
        self.th = th
        self._rng = random.Random(rng_seed)
        self.state = TurnState.AGENT_SPEAKING
        self._agent_active = False
        self._silence_ns: int | None = None       # start of current agent ear inactivity
        self._delay_ms: float | None = None
        self._spec_started = False
        self._tools_pending = 0
        self._tool_release_ns = 0
        self._current: TurnRecord | None = None
        self.turns: list[TurnRecord] = []
        self.overlaps: list[dict] = []
        self.discarded_speculations = 0

    # -- time helpers ---------------------------------------------------------
    def _t(self, t_ns: int | None) -> int:
        return self.clock.now_ns() if t_ns is None else t_ns

    def _ms(self, ms: float) -> int:
        return int(round(ms * NS_PER_MS))

    @property
    def tool_pending_now(self) -> bool:
        return self._tools_pending > 0

    def floor_ns(self) -> int | None:
        """Earliest floor-taking time, or None while it is not yet determined/blocked."""
        if self._silence_ns is None or self._delay_ms is None or self._tools_pending:
            return None
        return max(self._silence_ns + self._ms(self.th.sim_eot_ms) + self._ms(self._delay_ms),
                   self._tool_release_ns)

    # -- inputs ---------------------------------------------------------------
    def on_agent_active(self, t_ns: int | None = None) -> str | None:
        """Agent speech on the ear. Returns "unscripted_overlap" when it overlaps the caller."""
        t = self._t(t_ns)
        self._agent_active = True
        if self.state is TurnState.SPEAKING:
            ev = {"t_ns": t, "turn_idx": self._current.turn_idx if self._current else None,
                  "tag": UNSCRIPTED_OVERLAP}
            self.overlaps.append(ev)
            self._silence_ns = None
            return UNSCRIPTED_OVERLAP
        if self.state in (TurnState.SPECULATING, TurnState.FLOOR_WAIT) and self._spec_started:
            self.discarded_speculations += 1
        self.state = TurnState.AGENT_SPEAKING
        self._silence_ns = None
        self._delay_ms = None
        self._spec_started = False
        return None

    def on_agent_inactive(self, t_ns: int | None = None) -> None:
        """Agent ear activity ended at `t_ns` (start of the inactivity run)."""
        if not self._agent_active:
            return
        self._agent_active = False
        self._silence_ns = self._t(t_ns)

    def tool_pending(self, pending: bool, t_ns: int | None = None) -> None:
        """A tool call started (True) or produced its result/failure (False)."""
        if pending:
            self._tools_pending += 1
            return
        self._tools_pending = max(0, self._tools_pending - 1)
        self._tool_release_ns = max(self._tool_release_ns,
                                    self._t(t_ns) + self._ms(self.th.sim_patience_ms))

    # -- state advance ----------------------------------------------------------
    def poll(self, t_ns: int | None = None) -> TurnState:
        t = self._t(t_ns)
        if self.state is TurnState.SPEAKING or self._agent_active or self._silence_ns is None:
            return self.state
        quiet = t - self._silence_ns
        if self.state is TurnState.AGENT_SPEAKING and quiet >= self._ms(self.th.sim_speculate_ms):
            self.state = TurnState.SPECULATING
            self._spec_started = True
        if self.state is TurnState.SPECULATING and quiet >= self._ms(self.th.sim_eot_ms):
            lo, hi = self.th.sim_floor_delay_ms
            self._delay_ms = self._rng.uniform(lo, hi)
            self.state = TurnState.FLOOR_WAIT
        return self.state

    def next_action(self, t_ns: int | None = None) -> Speak | Wait:
        t = self._t(t_ns)
        st = self.poll(t)
        if st is TurnState.SPEAKING or self._agent_active or self._silence_ns is None:
            return Wait(None)
        if st is TurnState.AGENT_SPEAKING:
            return Wait(self._silence_ns + self._ms(self.th.sim_speculate_ms))
        if st is TurnState.SPECULATING:
            return Wait(self._silence_ns + self._ms(self.th.sim_eot_ms))
        f = self.floor_ns()
        if f is None:
            return Wait(None)                       # blocked by a pending tool
        if t < f:
            return Wait(f)
        return Speak(floor_ns=f, speculative=self._spec_started)

    def speculation_active(self) -> bool:
        """True while a speculative reply may be generated (SPECULATING or FLOOR_WAIT)."""
        return self.state in (TurnState.SPECULATING, TurnState.FLOOR_WAIT)

    def start_speaking(self, t_ns: int | None = None) -> TurnRecord:
        """Caller audio onset. Allowed only when next_action() is Speak at `t_ns`."""
        t = self._t(t_ns)
        a = self.next_action(t)
        if not isinstance(a, Speak):
            raise RuntimeError(f"cannot take the floor in {self.state} at {t} ns ({a})")
        rec = TurnRecord(
            turn_idx=len(self.turns), silence_ns=self._silence_ns, floor_ns=a.floor_ns,
            start_ns=t, think_ms=(t - self._silence_ns) / NS_PER_MS,
            late_ms=max(0, t - a.floor_ns) / NS_PER_MS, speculative=a.speculative,
            floor_delay_ms=self._delay_ms or 0.0)
        self.turns.append(rec)
        self._current = rec
        self.state = TurnState.SPEAKING
        self._silence_ns = None
        self._delay_ms = None
        self._spec_started = False
        return rec

    def start_first_turn(self, t_ns: int | None = None) -> TurnRecord:
        """Caller-first call opening (no agent turn to wait for); think_ms is None."""
        t = self._t(t_ns)
        rec = TurnRecord(len(self.turns), None, t, t, None, 0.0, False, 0.0)
        self.turns.append(rec)
        self._current = rec
        self.state = TurnState.SPEAKING
        return rec

    def finish_speaking(self, t_ns: int | None = None) -> None:
        t = self._t(t_ns)
        if self.state is not TurnState.SPEAKING:
            raise RuntimeError(f"finish_speaking in {self.state}")
        if self._current is not None:
            self._current.end_ns = t
        self._current = None
        self.state = TurnState.AGENT_SPEAKING
        # agent spoke during our utterance and went quiet: its silence start already set
