"""Control twins, turn alignment by caller step, and per-metric event validity (spec §3.4; BUILD_PLAN M2-T07).

    overlay run                       control run (same caller audio, no injected overlays)
    caller steps  s1 ── s2 ── s3      caller steps  s1 ── s2 ── s3
    agent turns   a1    a2    a3      agent turns   c1    c2    c3
                   \\    \\    \\                     /     /     /
                    align by caller step (never by raw turn index)

    validity(overlay_row, control_turn, metric, budget_ms, th)
        control_turn is None                                   -> "unmatched_control"
        control turn lasted < offset + window                  -> "ambiguous_end"
        else                                                   -> None (valid)
    window = budget_ms for yield_rate (Yield@B),
             th.hold_window_ms (2.0 s) for t_stop_ear and hold_speaking_rate.

An agent turn is the first agent utterance (ear onset -> ear stop, spec §1.2) that
starts after the caller step's turn end and before the next caller turn end. The
offset is measured from that onset, the same anchor the overlay engine schedules
from (spec §3.2), so "the control's matched turn continued at least offset + window"
compares like with like.

Track C (spec §3.4 last bullet): the interrupted-vs-clean split pairs calls at the
scenario level (`pair_by_scenario`, `split_pass_rates`) and needs no turn alignment.

`agent_turns(rec)` reads the runner's `caller_turn_end` (payload `step_id`) and the
playout's `agent_onset` / `agent_stop` events; the pure core
`agent_turns_from` takes plain tuples so floor_control can feed it from any source.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from duplexbench.metrics.names import Metric

if TYPE_CHECKING:
    from duplexbench.metrics.base import CallRecord, OverlayRow
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.thresholds import Thresholds

# Overlay classes that inject audio into the caller channel (spec §3.2 "Injected" column).
# P-PAUSE and E-END are geometry of the caller's own turn (a pause, the post-turn
# silence), not injected sound, so the twin keeps them: dropping them would change
# the caller audio, and the twin must have the same caller audio (spec §3.4).
INJECTED_CLASSES: frozenset[str] = frozenset({"Y-INT", "Y-CMD", "H-BC", "H-SIDE", "H-BG", "H-TIC"})

# Event kinds read by `agent_turns`: what runner.py (caller_turn_end, src caller) and
# playout.py (agent_onset / agent_stop, src playout) log.
EV_TURN_END = "caller_turn_end"
EV_ONSET = "agent_onset"
EV_STOP = "agent_stop"

UNMATCHED = "unmatched_control"
AMBIGUOUS = "ambiguous_end"

# Metrics with a validity window (spec §3.4 "Validity window per metric").
WINDOWED_METRICS: frozenset[str] = frozenset({Metric.yield_rate, Metric.t_stop_ear,
                                              Metric.hold_speaking_rate})


def make_twin(scenario: "Scenario") -> "Scenario":
    """The control twin: same id, steps and caller audio, no injected overlays.

    `is_control=True` lives on the call (CallContext / `calls.is_control`), not on
    the Scenario model, whose schema forbids extra fields. `control_twin` is False
    on the twin: a twin has no twin of its own.
    """
    kept = [o for o in scenario.overlays if o.cls not in INJECTED_CLASSES]
    return scenario.model_copy(update={"overlays": kept, "control_twin": False}, deep=True)


@dataclass(frozen=True)
class AgentTurn:
    """The agent utterance that replies to caller step `step_id` (ear onset -> ear stop)."""
    step_id: str
    onset_ns: int
    end_ns: int | None          # None: still speaking when the recording ended

    @property
    def duration_ms(self) -> float:
        return float("inf") if self.end_ns is None else (self.end_ns - self.onset_ns) / 1e6


def agent_turns_from(turn_ends: Sequence[tuple[str, int]],
                     utterances: Sequence[tuple[int, int | None]]) -> list[tuple[str, AgentTurn | None]]:
    """Pair each caller step with the first agent utterance after its turn end.

    `turn_ends`: `(step_id, t_ns)` in call order. `utterances`: `(onset_ns, stop_ns|None)`.
    Returns `(step_id, AgentTurn | None)` per caller step, in caller order; None when
    the agent did not start before the next caller turn end. An utterance that starts
    before a step's turn end (a negative gap) belongs to the previous step.
    """
    ends = list(turn_ends)
    utts = sorted(utterances, key=lambda u: u[0])
    out: list[tuple[str, AgentTurn | None]] = []
    for i, (step_id, t_end) in enumerate(ends):
        t_next = ends[i + 1][1] if i + 1 < len(ends) else None
        turn = None
        for onset, stop in utts:
            if onset >= t_end and (t_next is None or onset < t_next):
                turn = AgentTurn(step_id, onset, stop)
                break
        out.append((step_id, turn))
    return out


def agent_turns(rec: "CallRecord") -> list[tuple[str, AgentTurn | None]]:
    """`agent_turns_from` fed from a CallRecord's `turn_end` / `onset` / `stop` events."""
    turn_ends: list[tuple[str, int]] = []
    utts: list[tuple[int, int | None]] = []
    open_onset: int | None = None
    for ev in sorted(rec.events, key=lambda e: (e.t_ns, e.seq)):
        if ev.kind == EV_TURN_END:
            turn_ends.append((str(ev.payload.get("step_id")), ev.t_ns))
        elif ev.kind == EV_ONSET and open_onset is None:
            open_onset = ev.t_ns
        elif ev.kind == EV_STOP and open_onset is not None:
            utts.append((open_onset, ev.t_ns))
            open_onset = None
    if open_onset is not None:
        utts.append((open_onset, None))
    return agent_turns_from(turn_ends, utts)


TurnSeq = Sequence[tuple[str, "AgentTurn | None"]]


def _as_turns(x: "CallRecord | TurnSeq") -> list[tuple[str, AgentTurn | None]]:
    return list(x) if isinstance(x, Sequence) else agent_turns(x)


def align(overlay_rec: "CallRecord | TurnSeq", control_rec: "CallRecord | TurnSeq",
          ) -> dict[str, tuple[AgentTurn | None, AgentTurn | None] | None]:
    """Match agent turns of an overlay run and its control by caller step (spec §3.4).

    Accepts CallRecords or the `(step_id, AgentTurn|None)` lists `agent_turns` returns.
    Keys are the overlay run's caller steps. A step is matched when it lies on the
    longest common subsequence of the two step sequences; otherwise (the
    conversations diverged in count or order) its value is None, which `validity`
    turns into `unmatched_control`. A step that repeats in either run is None too:
    its turn cannot be matched unambiguously.
    """
    o_turns, c_turns = _as_turns(overlay_rec), _as_turns(control_rec)
    o_steps = [s for s, _ in o_turns]
    c_steps = [s for s, _ in c_turns]
    repeated = {s for s in o_steps if o_steps.count(s) > 1} | {s for s in c_steps if c_steps.count(s) > 1}
    out: dict[str, tuple[AgentTurn | None, AgentTurn | None] | None] = {s: None for s in o_steps}
    sm = SequenceMatcher(a=o_steps, b=c_steps, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            step = o_steps[blk.a + k]
            if step in repeated:
                continue
            out[step] = (o_turns[blk.a + k][1], c_turns[blk.b + k][1])
    return out


def window_ms(metric: str, budget_ms: int | None, th: "Thresholds") -> int:
    """The validity window of `metric` (spec §3.4): B for Yield@B, 2.0 s for T_stop_ear and Hold."""
    m = Metric(metric)
    if m not in WINDOWED_METRICS:
        raise ValueError(f"{m}: no control-twin validity window (spec §3.4 defines one for "
                         f"{sorted(str(x) for x in WINDOWED_METRICS)})")
    if m == Metric.yield_rate:
        if budget_ms is None:
            raise ValueError("yield_rate validity needs budget_ms (the stop budget B)")
        return int(budget_ms)
    return th.hold_window_ms


def overlay_offset_ms(row: "OverlayRow", agent_onset_ns: int | None = None) -> float | None:
    """Scheduled offset from agent onset: `scheduled_ns - agent_onset_ns`.

    The `overlays` row's `onset_ns` is the overlay's own onset at send time (the
    runner's and latency.py's meaning), so the agent onset must be passed; the
    legacy fallback `scheduled_ns - onset_ns` is kept for rows without it.
    Callers that know the scenario pass `validity(offset_ms=Overlay.offset_ms)`.
    """
    ref = agent_onset_ns if agent_onset_ns is not None else row.onset_ns
    if row.scheduled_ns is None or ref is None:
        return None
    return (row.scheduled_ns - ref) / 1e6


def validity(overlay_row: "OverlayRow", control_turn: "AgentTurn | None", metric: str,
             budget_ms: int | None, th: "Thresholds", *, offset_ms: float | None = None) -> str | None:
    """Validity tag of one overlay event for one metric (spec §3.4), or None when valid.

    `control_turn` is the control's matched agent turn: the second element of the
    `align` pair, or None when `align` gave None (-> `unmatched_control`).
    Returns `ambiguous_end` when that turn lasted < offset + window. The offset is
    `offset_ms` when given (the scenario's pre-registered `Overlay.offset_ms`), else
    `scheduled_ns - onset_ns` from the overlay row. `not_injected` and `contested`
    are the overlay engine's tags on the row; they are not decided here.
    """
    win = window_ms(metric, budget_ms, th)
    if control_turn is None:
        return UNMATCHED
    off = offset_ms if offset_ms is not None else overlay_offset_ms(overlay_row)
    if off is None:
        raise ValueError(f"overlay {overlay_row.overlay_id}: no offset (scheduled_ns/onset_ns "
                         f"missing); pass offset_ms")
    if control_turn.duration_ms < off + win:
        return AMBIGUOUS
    return None


# -- Track C: scenario-level pairing, no turn alignment (spec §3.4 last bullet) --------------

PairKey = tuple[str, str, str, int]      # (scenario_id, condition, system_id, trial)


def pair_by_scenario(calls: Iterable[Mapping[str, Any]],
                     ) -> dict[PairKey, tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]]:
    """Pair overlay and control calls by (scenario_id, condition, system_id, trial).

    `calls` are `calls`-table rows (or any mapping with those keys plus `is_control`).
    Value: `(overlay_call, control_call)`; either side None when missing. Step
    sequences are never compared: Track C pass rates pair at scenario level.
    """
    out: dict[PairKey, list[Mapping[str, Any] | None]] = {}
    for c in calls:
        key = (str(c["scenario_id"]), str(c["condition"]), str(c["system_id"]), int(c["trial"]))
        slot = out.setdefault(key, [None, None])
        i = 1 if bool(c["is_control"]) else 0
        if slot[i] is not None:
            raise ValueError(f"duplicate {'control' if i else 'overlay'} call for {key}")
        slot[i] = c
    return {k: (v[0], v[1]) for k, v in out.items()}


def split_pass_rates(pairs: Mapping[PairKey, tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]],
                     passed: Mapping[str, bool]) -> dict[str, float | int | None]:
    """Interrupted-vs-clean pass rates over complete pairs (spec §3.4, §10 item 5).

    `passed`: call_id -> task pass. Only pairs with both calls scored count, so the
    two rates are over the same scenarios. Returns `{n_pairs, interrupted, clean}`.
    """
    io = [(o, c) for o, c in pairs.values()
          if o is not None and c is not None and o["call_id"] in passed and c["call_id"] in passed]
    n = len(io)
    if n == 0:
        return {"n_pairs": 0, "interrupted": None, "clean": None}
    return {"n_pairs": n,
            "interrupted": sum(passed[o["call_id"]] for o, _ in io) / n,
            "clean": sum(passed[c["call_id"]] for _, c in io) / n}
