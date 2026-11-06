"""Phone path: two trunk legs and a bridge (spec §6.2; BUILD_PLAN M9-T01, M9-T03).

    caller leg  --PSTN-->  agent leg  --bridge-->  adapter (system under test)
       ^ the ear on this transport is what the caller leg RECEIVES (spec §6.2)

Pieces:

  TrunkClient     Protocol for one trunk with two legs, "caller" and "agent": send a
                  20 ms μ-law frame on a leg, receive the next TrunkEvent on a leg
                  (media, or a mark echoed back once the far side "played" it),
                  `clear` (drop what this leg sent that is not yet delivered: Twilio
                  `clear` semantics, pending marks are returned), `close`.
  LoopbackTrunk   in-process fake: what one leg sends reaches the other after
                  delay_ms + U(0, jitter_ms) (seeded), FIFO per direction. Used by the
                  loopback baseline tests and by phone-path tests without spend.
  TwilioTrunk     stub: the trunk vendor is Open Question 5 (Twilio or Telnyx); it
                  raises NotImplementedError until a human decides. Media Streams
                  framing is transport/twilio_media.py, imported lazily (`media_codec()`).
  caller_leg      paces caller frames onto the caller leg and records the ear
                  (received frames with receive times).
  bridge          agent leg <-> adapter: received media -> `to_adapter(payload)`;
                  adapter audio -> agent-leg media + a `mark` per chunk (playback
                  confirmation); adapter cancel -> `clear` on the agent leg.

Agreement with the live stream (spec §6.2): per-metric SIGN agreement of the paired
differences between systems, not Spearman. `sign_agreement(live, phone)` takes
{metric: {system: value}} for both transports; `disagreements(table)` lists the
metrics whose sign differs for any system pair. They go to the run card as
`phone_path.disagreements` (run_card_fill.fill(..., phone_path=...)), where
report/aggregate.py and the caveats read them.

Phone-path latencies are reported raw AND baseline-subtracted, both labeled
(`with_baseline`); the baseline is the loopback round trip (transport/loopback_baseline.py).
"""
from __future__ import annotations

import asyncio
import importlib
import itertools
import random
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Protocol, runtime_checkable

NS_PER_MS = 1_000_000
LEGS = ("caller", "agent")


def _other(leg: str) -> str:
    if leg not in LEGS:
        raise ValueError(f"unknown leg {leg!r}")
    return LEGS[1 - LEGS.index(leg)]


@dataclass(frozen=True)
class TrunkEvent:
    kind: str                 # "media" | "mark" | "hangup"
    t_ns: int                 # receive time on the receiving leg
    payload: bytes = b""
    name: str = ""            # mark name


@runtime_checkable
class TrunkClient(Protocol):
    name: str

    async def connect(self) -> None: ...
    async def send(self, leg: str, payload: bytes) -> None: ...
    async def mark(self, leg: str, name: str) -> None: ...
    async def clear(self, leg: str) -> list[str]: ...
    async def recv(self, leg: str) -> TrunkEvent: ...
    async def close(self) -> None: ...


def media_codec() -> Any | None:
    """transport/twilio_media.py when present (written by the M6-T02 work), else None."""
    try:
        return importlib.import_module("duplexbench.transport.twilio_media")
    except ModuleNotFoundError:
        return None


class TwilioTrunk:
    """Placeholder for the real trunk (Open Question 5: Twilio or Telnyx, not chosen)."""

    name = "twilio"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("Q5: trunk vendor not chosen")


class LoopbackTrunk:
    """In-process two-leg trunk on a Clock (VirtualClock or RealClock).

    Delivery time of an item sent at t: max(t + delay + U(0, jitter), last delivery in
    that direction) -- FIFO, like an RTP stream with a jitter buffer.
    """

    name = "loopback"

    def __init__(self, clock: Any, delay_ms: float, jitter_ms: float = 0.0, seed: int = 0):
        self.clock = clock
        self.delay_ns = int(delay_ms * NS_PER_MS)
        self.jitter_ns = int(jitter_ms * NS_PER_MS)
        self.rng = random.Random(seed)
        # pending[to_leg]: deque of (deliver_ns, seq, from_leg, kind, payload, name)
        self.pending: dict[str, deque] = {leg: deque() for leg in LEGS}
        self._last: dict[str, int] = {leg: 0 for leg in LEGS}
        self._ev: dict[str, asyncio.Event] = {}
        self._seq = itertools.count()
        self.closed = False
        self.sent: dict[str, int] = {leg: 0 for leg in LEGS}

    def _event(self, leg: str) -> asyncio.Event:
        if leg not in self._ev:
            self._ev[leg] = asyncio.Event()
        return self._ev[leg]

    async def connect(self) -> None:
        self.closed = False

    def _push(self, from_leg: str, kind: str, payload: bytes = b"", name: str = "") -> None:
        to = _other(from_leg)
        jit = self.rng.randint(0, self.jitter_ns) if self.jitter_ns else 0
        t = max(self.clock.now_ns() + self.delay_ns + jit, self._last[to])
        self._last[to] = t
        self.pending[to].append((t, next(self._seq), from_leg, kind, payload, name))
        self._event(to).set()

    async def send(self, leg: str, payload: bytes) -> None:
        self.sent[leg] += 1
        self._push(leg, "media", payload)

    async def mark(self, leg: str, name: str) -> None:
        """The mark travels with the media; when delivered it is echoed back to `leg`."""
        self._push(leg, "mark", name=name)

    async def clear(self, leg: str) -> list[str]:
        """Drop undelivered media sent from `leg`; return (and echo) the pending marks."""
        to = _other(leg)
        keep, marks = deque(), []
        for item in self.pending[to]:
            if item[2] == leg and item[3] == "mark":
                marks.append(item[5])
            elif item[2] != leg:
                keep.append(item)
        self.pending[to].clear()          # in place: a waiting recv holds this deque
        self.pending[to].extend(keep)
        now = self.clock.now_ns()
        for m in marks:
            self.pending[leg].append((now, next(self._seq), to, "mark_echo", b"", m))
            self._event(leg).set()
        return marks

    async def recv(self, leg: str) -> TrunkEvent:
        q = self.pending[leg]
        while True:
            if self.closed and not q:
                return TrunkEvent("hangup", self.clock.now_ns())
            if q:
                t = q[0][0]
                if t > self.clock.now_ns():
                    await self.clock.sleep_until_ns(t)
                    continue
                _, _, src, kind, payload, name = q.popleft()
                now = self.clock.now_ns()
                if kind == "mark":
                    # played on this leg -> echo the mark back to the sender
                    self.pending[src].append((now + self.delay_ns, next(self._seq), leg,
                                              "mark_echo", b"", name))
                    self._event(src).set()
                    continue
                if kind == "mark_echo":
                    return TrunkEvent("mark", now, name=name)
                return TrunkEvent("media", now, payload)
            ev = self._event(leg)
            ev.clear()
            await self.clock.idle(ev.wait())

    async def close(self) -> None:
        self.closed = True
        for leg in LEGS:
            self._event(leg).set()


# ---- legs ----------------------------------------------------------------------

@dataclass
class EarFrame:
    t_ns: int
    payload: bytes


async def caller_leg(clock: Any, trunk: TrunkClient, frames: Iterable[bytes], frame_ms: int,
                     *, t0_ns: int | None = None, tail_ms: int = 0) -> tuple[list[int], list[EarFrame]]:
    """Send `frames` on the frame grid; record every received frame (the ear).

    Returns (send times, ear frames). Receiving continues `tail_ms` after the last send.
    """
    step = frame_ms * NS_PER_MS
    t = clock.now_ns() if t0_ns is None else t0_ns
    sends: list[int] = []
    ear: list[EarFrame] = []

    async def rx() -> None:
        while True:
            ev = await trunk.recv("caller")
            if ev.kind == "hangup":
                return
            if ev.kind == "media":
                ear.append(EarFrame(ev.t_ns, ev.payload))

    rx_task = clock.task(rx())
    try:
        for fr in frames:
            await clock.sleep_until_ns(t)
            sends.append(clock.now_ns())
            await trunk.send("caller", fr)
            t += step
        await clock.sleep_until_ns(t + tail_ms * NS_PER_MS)
    finally:
        rx_task.cancel()
        await clock.idle(asyncio.gather(rx_task, return_exceptions=True))
    return sends, ear


async def bridge(clock: Any, trunk: TrunkClient, *,
                 to_adapter: Callable[[bytes], Awaitable[None]],
                 from_adapter: AsyncIterator[tuple],
                 log: Callable[[str, int, dict], None] | None = None) -> dict:
    """Agent leg <-> adapter until the adapter stream ends or the trunk hangs up.

    `from_adapter` yields ("audio", mulaw_bytes) or ("cancel",). Every audio chunk is
    followed by a `mark` (name "m<k>") so playback is confirmed when Twilio echoes it;
    a cancel sends `clear`, whose returned marks are logged as cleared.
    """
    stats = {"to_adapter": 0, "audio": 0, "marks_sent": 0, "marks_played": 0, "clears": 0,
             "marks_cleared": 0}
    note = log or (lambda kind, t, payload: None)

    async def inbound() -> None:
        while True:
            ev = await trunk.recv("agent")
            if ev.kind == "hangup":
                return
            if ev.kind == "media":
                stats["to_adapter"] += 1
                await to_adapter(ev.payload)
            elif ev.kind == "mark":
                stats["marks_played"] += 1
                note("mark_played", ev.t_ns, {"name": ev.name})

    in_task = clock.task(inbound())
    k = 0
    try:
        async for item in from_adapter:
            if item[0] == "audio":
                await trunk.send("agent", item[1])
                stats["audio"] += 1
                await trunk.mark("agent", f"m{k}")
                stats["marks_sent"] += 1
                k += 1
            elif item[0] == "cancel":
                cleared = await trunk.clear("agent")
                stats["clears"] += 1
                stats["marks_cleared"] += len(cleared)
                note("clear", clock.now_ns(), {"marks": cleared})
    finally:
        in_task.cancel()
        await clock.idle(asyncio.gather(in_task, return_exceptions=True))
    return stats


# ---- agreement with the live stream (spec §6.2) --------------------------------------

def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def sign_agreement(live: dict[str, dict[str, float | None]],
                   phone: dict[str, dict[str, float | None]]) -> dict[str, dict]:
    """Per metric: for every system pair (a < b) present on both transports, compare
    sign(a - b) on live stream vs phone path. A pair agrees when the signs are equal."""
    out: dict[str, dict] = {}
    for m in sorted(set(live) & set(phone)):
        systems = sorted(s for s in set(live[m]) & set(phone[m])
                         if live[m][s] is not None and phone[m][s] is not None)
        pairs = []
        for i, a in enumerate(systems):
            for b in systems[i + 1:]:
                dl = float(live[m][a]) - float(live[m][b])
                dp = float(phone[m][a]) - float(phone[m][b])
                pairs.append({"a": a, "b": b, "live_diff": dl, "phone_diff": dp,
                              "agree": _sign(dl) == _sign(dp)})
        k = sum(p["agree"] for p in pairs)
        out[m] = {"pairs": pairs, "n": len(pairs), "agree": k,
                  "share": (k / len(pairs)) if pairs else None,
                  "disagree": any(not p["agree"] for p in pairs)}
    return out


def disagreements(table: dict[str, dict]) -> list[str]:
    return sorted(m for m, e in table.items() if e["disagree"])


def values_from_aggregate(agg: dict, condition: str = "C0") -> dict[str, dict[str, float | None]]:
    """{metric_key: {system: value}} from a report/aggregate.py result."""
    out: dict[str, dict[str, float | None]] = {}
    for key, per_sys in agg.get("metrics", {}).items():
        for sid, per_cond in per_sys.items():
            s = per_cond.get(condition)
            if s is not None:
                out.setdefault(key, {})[sid] = s.get("value")
    return out


def with_baseline(value_ms: float | None, loopback: dict) -> dict:
    """Raw and baseline-subtracted phone-path latency, both labeled (spec §6.2)."""
    rtt = loopback.get("rtt_ms_median")
    return {"raw_ms": value_ms,
            "baseline_subtracted_ms": (None if value_ms is None or rtt is None else value_ms - rtt),
            "baseline_rtt_ms": rtt, "labels": ("raw", "baseline-subtracted")}
