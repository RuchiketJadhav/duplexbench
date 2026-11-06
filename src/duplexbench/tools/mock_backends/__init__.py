"""Domain mock backends (spec §4.2): one deterministic Backend per Track C domain."""
from __future__ import annotations

from duplexbench.tools.mock_backends.account import AccountBackend
from duplexbench.tools.mock_backends.billing import BillingBackend
from duplexbench.tools.mock_backends.orders import OrdersBackend
from duplexbench.tools.mock_backends.outbound import OutboundBackend
from duplexbench.tools.mock_backends.scheduling import SchedulingBackend

BACKENDS = {b.domain: b for b in (SchedulingBackend, OrdersBackend, BillingBackend,
                                  AccountBackend, OutboundBackend)}


def get_backend(domain: str) -> type:
    try:
        return BACKENDS[domain]
    except KeyError:
        raise KeyError(f"no mock backend for domain {domain!r}; have {sorted(BACKENDS)}") from None
