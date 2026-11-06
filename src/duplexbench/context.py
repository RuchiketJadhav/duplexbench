"""Per-call context handed to adapters and call tasks (BUILD_PLAN §2.7; spec §1.1).

Lives in its own module so adapters can import it without importing the
runner. `turn_ended(t_ns)` is how the runner tells the adapter base class that
a caller turn ended (spec §1.5 anchor "caller turn end"); the base class then
emits `first_audio_byte` on the next `audio` event (BUILD_PLAN §2.3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.scenarios.schema import Scenario
    from duplexbench.storage import Store


@dataclass
class CallContext:
    run_id: str
    call_id: str
    scenario: "Scenario"
    trial: int
    condition: Any          # channel.chain.Condition (M1-T07); a str id until that exists
    system_id: str
    is_control: bool
    clock: "Clock"
    store: "Store"
    tool_token: str
    turn_end_ns: list[int] = field(default_factory=list)   # every caller turn end, in order

    def turn_ended(self, t_ns: int) -> None:
        """Record a caller turn end at `t_ns` (clock time of the last speech frame sent)."""
        self.turn_end_ns.append(t_ns)
