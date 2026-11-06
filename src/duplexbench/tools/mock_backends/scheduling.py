"""Appointment scheduling backend (spec §4.2 domain 1). Policy: no double-booking."""
from __future__ import annotations

from duplexbench.tools.state import Backend, PolicyError, ToolArgError, ToolSchema, obj_schema

_NOON = 12


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
        },
        "appointments": {},
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
