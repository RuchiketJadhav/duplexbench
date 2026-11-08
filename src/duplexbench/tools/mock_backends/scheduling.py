"""Appointment scheduling backend (spec §4.2 domain 1). Policy: no double-booking.

Seed appointments (WORKFLOW_SPEC §9 G4) let `reschedule_appointment` and
`cancel_appointment` work without a booking earlier in the call:

    AP-101  slot S4  2026-11-12T11:00  Dr. Lee    Leo Park    3125550111  booked
    AP-102  slot S5  2026-11-13T14:00  Dr. Patel  Rita Gomez  3125550122  booked

Seed ids use the `AP-` prefix. In-call bookings get `A0001`, `A0002`, ... from
`Backend._next_id`, which counts only ids of that form, so the seed never shifts
or collides with them (the first booking in a call is still A0001). S4 and S5
are booked, so `find_slots` never lists them; S1-S3 stay open.

A scenario `seed` block may add or change appointments with the same row shape
`{slot_id, name, phone, start, status: booked|cancelled}`; a booked appointment's
slot must exist with `booked_by` set to that appointment id, and a seeded id must
not have the generated form `A<4 digits>` (`seed_errors`).
"""
from __future__ import annotations

import re

from duplexbench.tools.state import Backend, PolicyError, ToolArgError, ToolSchema, obj_schema

_NOON = 12
_GENERATED_APPT = re.compile(r"A\d{4}")
_APPT_FIELDS = ("slot_id", "name", "phone", "start", "status")


def spoken_time(iso: str) -> str:
    """Spoken anchor for an ISO datetime, e.g. '3 PM' on the hour, '10:30 AM' otherwise."""
    hh, mm = int(iso[11:13]), iso[14:16]
    h12 = hh % _NOON or _NOON
    suffix = "AM" if hh < _NOON else "PM"
    return f"{h12} {suffix}" if mm == "00" else f"{h12}:{mm} {suffix}"


class SchedulingBackend(Backend):
    domain = "scheduling"
    SEED = {
        "slots": {
            "S1": {"start": "2026-11-10T09:00", "provider": "Dr. Lee", "booked_by": None},
            "S2": {"start": "2026-11-10T15:00", "provider": "Dr. Lee", "booked_by": None},
            "S3": {"start": "2026-11-11T10:30", "provider": "Dr. Patel", "booked_by": None},
            "S4": {"start": "2026-11-12T11:00", "provider": "Dr. Lee", "booked_by": "AP-101"},
            "S5": {"start": "2026-11-13T14:00", "provider": "Dr. Patel", "booked_by": "AP-102"},
        },
        "appointments": {
            "AP-101": {"slot_id": "S4", "name": "Leo Park", "phone": "3125550111",
                       "start": "2026-11-12T11:00", "status": "booked"},
            "AP-102": {"slot_id": "S5", "name": "Rita Gomez", "phone": "3125550122",
                       "start": "2026-11-13T14:00", "status": "booked"},
        },
    }

    def tools(self) -> list[ToolSchema]:
        return [
            ToolSchema("find_slots", "read", "List open slots, optionally on one date.",
                       obj_schema({}, {"date": "string", "provider": "string"})),
            ToolSchema("book_appointment", "write", "Book an open slot for a caller.",
                       obj_schema({"slot_id": "string", "name": "string", "phone": "string"})),
            ToolSchema("reschedule_appointment", "write", "Move an appointment to another slot.",
                       obj_schema({"appointment_id": "string", "slot_id": "string"})),
            ToolSchema("cancel_appointment", "write", "Cancel an appointment.",
                       obj_schema({"appointment_id": "string"})),
        ]

    def _t_find_slots(self, date: str | None = None, provider: str | None = None) -> dict:
        slots = [{"slot_id": k, **v} for k, v in sorted(self._table("slots").items())
                 if v["booked_by"] is None
                 and (date is None or v["start"].startswith(date))
                 and (provider is None or v["provider"] == provider)]
        return {"slots": slots, "_anchors": [spoken_time(s["start"]) for s in slots]}

    def _take(self, slot_id: str, appt_id: str) -> dict:
        slot = self._row("slots", slot_id)
        if slot["booked_by"] is not None:
            raise PolicyError("double_booking", f"slot {slot_id} is already booked")
        slot["booked_by"] = appt_id
        return slot

    def _t_book_appointment(self, slot_id: str, name: str, phone: str) -> dict:
        appt_id = self._next_id("appointments", "A")
        slot = self._take(slot_id, appt_id)
        self._table("appointments")[appt_id] = {"slot_id": slot_id, "name": name,
                                                "phone": phone, "start": slot["start"],
                                                "status": "booked"}
        return {"appointment_id": appt_id, "start": slot["start"],
                "_anchors": [spoken_time(slot["start"])]}

    def _t_reschedule_appointment(self, appointment_id: str, slot_id: str) -> dict:
        appt = self._row("appointments", appointment_id)
        if appt["status"] != "booked":
            raise ToolArgError(f"appointment {appointment_id} is {appt['status']}")
        slot = self._take(slot_id, appointment_id)
        self._row("slots", appt["slot_id"])["booked_by"] = None
        appt.update(slot_id=slot_id, start=slot["start"])
        return {"appointment_id": appointment_id, "start": slot["start"],
                "_anchors": [spoken_time(slot["start"])]}

    def _t_cancel_appointment(self, appointment_id: str) -> dict:
        appt = self._row("appointments", appointment_id)
        if appt["status"] == "booked":
            self._row("slots", appt["slot_id"])["booked_by"] = None
        appt["status"] = "cancelled"
        return {"appointment_id": appointment_id, "status": "cancelled",
                "_anchors": ["cancelled"]}

    def seed_errors(self) -> list[str]:
        errs = []
        slots = self._table("slots")
        for aid, a in self._table("appointments").items():
            if _GENERATED_APPT.fullmatch(aid):
                errs.append(f"appointments/{aid}: id has the in-call form A<4 digits>; "
                            f"use another prefix (e.g. AP-)")
            missing = [k for k in _APPT_FIELDS if k not in a]
            if missing:
                errs.append(f"appointments/{aid}: missing {missing}")
                continue
            if a["status"] not in ("booked", "cancelled"):
                errs.append(f"appointments/{aid}: status {a['status']!r} not booked|cancelled")
            slot = slots.get(a["slot_id"])
            if slot is None:
                errs.append(f"appointments/{aid}: slot {a['slot_id']!r} not in slots")
            elif a["status"] == "booked" and slot.get("booked_by") != aid:
                errs.append(f"appointments/{aid}: slot {a['slot_id']} booked_by is "
                            f"{slot.get('booked_by')!r}, not {aid!r}")
        return errs
