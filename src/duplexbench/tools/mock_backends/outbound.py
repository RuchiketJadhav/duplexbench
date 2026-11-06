"""Outbound confirmation calls backend (spec §4.2 domain 5).

Policy: after `mark_do_not_call`, any further write for that contact is
refused with PolicyError("do_not_call").
"""
from __future__ import annotations

from duplexbench.tools.state import Backend, PolicyError, ToolArgError, ToolSchema, obj_schema

STATUSES = ("confirmed", "declined", "reschedule_requested")


class OutboundBackend(Backend):
    domain = "outbound"
    SEED = {
        "contacts": {
            "K-1": {"name": "Sam Ortiz", "phone": "2125550142",
                    "appointment_start": "2026-11-12T14:00", "confirmation": "pending",
                    "do_not_call": False},
        },
        "callbacks": {},
    }

    def tools(self) -> list[ToolSchema]:
        return [
            ToolSchema("get_contact", "read", "Read the contact and their appointment.",
                       obj_schema({"contact_id": "string"})),
            ToolSchema("record_confirmation", "write",
                       "Record confirmed / declined / reschedule_requested.",
                       obj_schema({"contact_id": "string", "status": "string"})),
            ToolSchema("schedule_callback", "write", "Schedule a callback time.",
                       obj_schema({"contact_id": "string", "when": "string"})),
            ToolSchema("mark_do_not_call", "write", "Add the contact to the do-not-call list.",
                       obj_schema({"contact_id": "string"})),
        ]

    def _contact_for_write(self, contact_id: str) -> dict:
        c = self._row("contacts", contact_id)
        if c["do_not_call"]:
            raise PolicyError("do_not_call", f"contact {contact_id} opted out")
        return c

    def _t_get_contact(self, contact_id: str) -> dict:
        c = self._row("contacts", contact_id)
        return {"contact_id": contact_id, **c, "_anchors": [c["appointment_start"]]}

    def _t_record_confirmation(self, contact_id: str, status: str) -> dict:
        if status not in STATUSES:
            raise ToolArgError(f"status must be one of {STATUSES}")
        self._contact_for_write(contact_id)["confirmation"] = status
        return {"contact_id": contact_id, "confirmation": status, "_anchors": [status]}

    def _t_schedule_callback(self, contact_id: str, when: str) -> dict:
        self._contact_for_write(contact_id)
        cid = self._next_id("callbacks", "CB")
        self._table("callbacks")[cid] = {"contact_id": contact_id, "when": when}
        return {"callback_id": cid, "when": when, "_anchors": [when]}

    def _t_mark_do_not_call(self, contact_id: str) -> dict:
        self._contact_for_write(contact_id)["do_not_call"] = True
        return {"contact_id": contact_id, "do_not_call": True, "_anchors": ["do not call"]}
