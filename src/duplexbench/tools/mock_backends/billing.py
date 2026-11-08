"""Billing and payments backend (spec §4.2 domain 3).

Policy (all keys per scenario, in the scenario's `policy` block):

- `refund_cap`: a refund above it is refused with PolicyError("refund_over_cap").
  There is no default cap here (no cap in the policy = no cap). `get_balance`
  returns the cap as `refund_cap` (canonical amount string, null when unset) so
  the agent can read it before it tries (WORKFLOW_SPEC §9 G5).
- `require_identity` (bool, default false): when true, `make_payment` and
  `issue_refund` require a successful `verify_identity` for that account earlier
  in the call; otherwise PolicyError("auth_required") (WORKFLOW_SPEC §9 G2).
  Off by default so scenarios without the key (pd_c_002) behave as before.
  `verify_identity` itself is always available. A failed verification records
  nothing. The account backend keeps its always-on gate.

A refund can never exceed the account's paid total (PolicyError("refund_over_paid")).

Identity seed values (for scenario `facts`): AC-77 dob 1958-07-14, zip 60614.
A scenario's `seed` block may override them (row fields merge onto SEED).
"""
from __future__ import annotations

from decimal import Decimal

from duplexbench.tools.state import (Backend, PolicyError, ToolArgError, ToolSchema,
                                     canon_amount, obj_schema)


class BillingBackend(Backend):
    domain = "billing"
    SEED = {
        "accounts": {
            "AC-77": {"holder": "Ana Ruiz", "balance": "120.00", "paid_total": "300.00",
                      "dob": "1958-07-14", "zip": "60614"},
        },
        "payments": {},
        "refunds": {},
    }

    def tools(self) -> list[ToolSchema]:
        return [
            ToolSchema("verify_identity", "read",
                       "Verify a caller by date of birth and ZIP. customer_id is the billing "
                       "account ID.",
                       obj_schema({"customer_id": "string", "dob": "string", "zip": "string"})),
            ToolSchema("get_balance", "read",
                       "Read an account's balance and its refund cap. refund_cap is the "
                       "largest amount one issue_refund call may refund, or null when there "
                       "is no cap.",
                       obj_schema({"account_id": "string"})),
            ToolSchema("make_payment", "write", "Take a payment against the balance.",
                       obj_schema({"account_id": "string", "amount": "string"})),
            ToolSchema("issue_refund", "write", "Refund an amount to the account holder.",
                       obj_schema({"account_id": "string", "amount": "string",
                                   "reason": "string"})),
        ]

    def _refund_cap(self) -> str | None:
        cap = self.policy.get("refund_cap")
        return None if cap is None else canon_amount(cap)

    def _gate(self, account_id: str, tool: str) -> None:
        if self.policy.get("require_identity") is True and account_id not in self.verified:
            raise PolicyError("auth_required", f"{tool} before verify_identity")

    def _t_verify_identity(self, customer_id: str, dob: str, zip: str) -> dict:
        a = self._row("accounts", customer_id)
        ok = (a.get("dob") is not None and a.get("zip") is not None
              and a["dob"] == dob and str(a["zip"]) == str(zip))
        if ok:
            self.verified.add(customer_id)
        return {"customer_id": customer_id, "verified": ok,
                "_anchors": ["verified" if ok else "could not verify"]}

    def _t_get_balance(self, account_id: str) -> dict:
        a = self._row("accounts", account_id)
        return {"account_id": account_id, "balance": a["balance"],
                "refund_cap": self._refund_cap(), "_anchors": [a["balance"]]}

    def _t_make_payment(self, account_id: str, amount: str) -> dict:
        a = self._row("accounts", account_id)
        self._gate(account_id, "make_payment")
        if Decimal(amount) <= 0:
            raise ToolArgError("amount must be positive")
        a["balance"] = canon_amount(Decimal(a["balance"]) - Decimal(amount))
        a["paid_total"] = canon_amount(Decimal(a["paid_total"]) + Decimal(amount))
        pid = self._next_id("payments", "P")
        self._table("payments")[pid] = {"account_id": account_id, "amount": amount}
        return {"payment_id": pid, "balance": a["balance"], "_anchors": [amount]}

    def _t_issue_refund(self, account_id: str, amount: str, reason: str) -> dict:
        a = self._row("accounts", account_id)
        self._gate(account_id, "issue_refund")
        cap = self._refund_cap()
        if cap is not None and Decimal(amount) > Decimal(cap):
            raise PolicyError("refund_over_cap", f"{amount} > cap {cap}")
        if Decimal(amount) > Decimal(a["paid_total"]):
            raise PolicyError("refund_over_paid", f"{amount} > paid {a['paid_total']}")
        rid = self._next_id("refunds", "F")
        self._table("refunds")[rid] = {"account_id": account_id, "amount": amount,
                                       "reason": reason}
        return {"refund_id": rid, "amount": amount, "_anchors": [amount]}

    def seed_errors(self) -> list[str]:
        errs = []
        if self.policy.get("require_identity") not in (None, True, False):
            errs.append("policy.require_identity must be true or false")
        for aid, a in self._table("accounts").items():
            for k in ("balance", "paid_total"):
                if k not in a:
                    errs.append(f"accounts/{aid}: missing {k}")
            if self.policy.get("require_identity") is True and (
                    a.get("dob") is None or a.get("zip") is None):
                errs.append(f"accounts/{aid}: require_identity needs dob and zip")
        return errs
