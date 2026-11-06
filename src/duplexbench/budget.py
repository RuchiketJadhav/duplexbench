"""Run budget with reservation at call admission (BUILD_PLAN §2.10, M3-T02; spec §9.3; CLAUDE.md §6 item 1).

    budget = Budget(max_usd=run_cfg.max_usd)
    r = budget.reserve(projected_usd)   # before adapter.open; raises BudgetExceeded
    ...
    budget.settle(r, actual_usd)        # after close; None = actual unknown

Admission rule: refuse when `settled + reserved + projected > max_usd`.
An unknown actual (`None`, e.g. no usage fields and no price) is settled at the
projected amount and counted in `unknown_actual`; per M3-T01 the run must stop
before the next call in that case, which is the caller's decision.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    id: int
    projected_usd: float


class Budget:
    def __init__(self, max_usd: float) -> None:
        if max_usd < 0:
            raise ValueError("max_usd must be >= 0")
        self.max_usd = float(max_usd)
        self.settled = 0.0
        self.unknown_actual = 0
        self._open: dict[int, Reservation] = {}
        self._ids = itertools.count(1)

    @property
    def reserved(self) -> float:
        return sum(r.projected_usd for r in self._open.values())

    @property
    def remaining(self) -> float:
        return self.max_usd - self.settled - self.reserved

    def reserve(self, projected_usd: float) -> Reservation:
        if projected_usd < 0:
            raise ValueError("projected_usd must be >= 0")
        need = self.settled + self.reserved + projected_usd
        if need > self.max_usd + 1e-9:
            raise BudgetExceeded(
                f"reserve {projected_usd:.4f} USD refused: settled {self.settled:.4f} + reserved "
                f"{self.reserved:.4f} + projected > max_usd {self.max_usd:.4f}")
        r = Reservation(next(self._ids), float(projected_usd))
        self._open[r.id] = r
        return r

    def settle(self, reservation: Reservation, actual_usd: float | None) -> None:
        if self._open.pop(reservation.id, None) is None:
            raise ValueError(f"reservation {reservation.id} is not open (settled twice?)")
        if actual_usd is None:
            self.unknown_actual += 1
            actual_usd = reservation.projected_usd
        self.settled += float(actual_usd)
