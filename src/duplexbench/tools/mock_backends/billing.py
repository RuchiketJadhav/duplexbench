"""Billing and payments backend (spec §4.2 domain 3).

Policy: a refund above the scenario's `policy["refund_cap"]` is refused with
PolicyError("refund_over_cap"). The cap is per scenario, so there is no
default cap here (no cap in the policy = no cap). A refund can never exceed
the account's paid total (PolicyError("refund_over_paid")).
"""
from __future__ import annotations

from decimal import Decimal

from duplexbench.tools.state import (Backend, PolicyError, ToolArgError, ToolSchema,
                                     canon_amount, obj_schema)


class BillingBackend(Backend):
    domain = "billing"
    SEED = {
        "accounts": {
            "AC-77": {"holder": "Ana Ruiz", "balance": "120.00", "paid_total": "300.00"},
        },
        "payments": {},
        "refunds": {},
    }

    def tools(self) -> list[ToolSchema]:
        return [
            ToolSchema("get_balance", "read", "Read an account's balance.",
                       obj_schema({"account_id": "string"})),
            ToolSchema("make_payment", "write", "Take a payment against the balance.",
                       obj_schema({"account_id": "string", "amount": "string"})),
            ToolSchema("issue_refund", "write", "Refund an amount to the account holder.",
                       obj_schema({"account_id": "string", "amount": "string",
                                   "reason": "string"})),
        ]

    def _t_get_balance(self, account_id: str) -> dict:
        a = self._row("accounts", account_id)
        return {"account_id": account_id, "balance": a["balance"], "_anchors": [a["balance"]]}

    def _t_make_payment(self, account_id: str, amount: str) -> dict:
        a = self._row("accounts", account_id)
        if Decimal(amount) <= 0:
            raise ToolArgError("amount must be positive")
        a["balance"] = canon_amount(Decimal(a["balance"]) - Decimal(amount))
        a["paid_total"] = canon_amount(Decimal(a["paid_total"]) + Decimal(amount))
        pid = self._next_id("payments", "P")
        self._table("payments")[pid] = {"account_id": account_id, "amount": amount}
        return {"payment_id": pid, "balance": a["balance"], "_anchors": [amount]}

    def _t_issue_refund(self, account_id: str, amount: str, reason: str) -> dict:
        a = self._row("accounts", account_id)
        cap = self.policy.get("refund_cap")
        if cap is not None and Decimal(amount) > Decimal(canon_amount(cap)):
            raise PolicyError("refund_over_cap", f"{amount} > cap {canon_amount(cap)}")
        if Decimal(amount) > Decimal(a["paid_total"]):
            raise PolicyError("refund_over_paid", f"{amount} > paid {a['paid_total']}")
        rid = self._next_id("refunds", "F")
        self._table("refunds")[rid] = {"account_id": account_id, "amount": amount,
                                       "reason": reason}
        return {"refund_id": rid, "amount": amount, "_anchors": [amount]}
