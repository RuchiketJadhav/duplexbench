"""M4-T02: domain backends, canonical state, policies, result anchors."""
import pytest

from duplexbench.tools.mock_backends import BACKENDS, get_backend
from duplexbench.tools.mock_backends.account import AccountBackend
from duplexbench.tools.mock_backends.billing import BillingBackend
from duplexbench.tools.mock_backends.orders import OrdersBackend
from duplexbench.tools.mock_backends.outbound import OutboundBackend
from duplexbench.tools.mock_backends.scheduling import SchedulingBackend
from duplexbench.tools.state import (PolicyError, ToolArgError, canon_amount, canon_date,
                                     canon_email, canon_phone, canonicalize, expected_equals,
                                     snapshot_hash)


# -- canonicalization ----------------------------------------------------------
def test_phone_canonicalization():
    assert canon_phone("(555) 123-4567") == canon_phone("5551234567") == "5551234567"
    assert canon_phone("+1 555-123-4567") == "5551234567"
    assert canonicalize({"phone": "(555) 123-4567"}) == canonicalize({"phone": "5551234567"})


def test_email_date_amount_canonicalization():
    assert canon_email("  John.Doe@Example.COM ") == "john.doe@example.com"
    assert canon_date("March 3 1990") == canon_date("03/03/1990") == "1990-03-03"
    assert canon_date("2026-11-10 3:00 PM") == "2026-11-10T15:00"
    assert canon_amount("$42.1") == canon_amount("42.10") == "42.10"
    assert canon_amount("1,250") == "1250.00"


def test_key_tokens_not_substrings():
    # "updated" contains "date" but is not a date field
    assert canonicalize({"updated": " yes "}) == {"updated": "yes"}


def test_hash_stable_across_key_order():
    a = {"customers": {"C1": {"phone": "5551234567", "email": "a@b.co"}}, "z": 1}
    b = {"z": 1, "customers": {"C1": {"email": "A@B.co", "phone": "(555) 123-4567"}}}
    assert snapshot_hash(a) == snapshot_hash(b)
    assert snapshot_hash(a) != snapshot_hash({**a, "z": 2})


def test_expected_equals_is_canonical_subset():
    state = {"customers": {"C1": {"phone": "5551234567", "name": "Jo"}, "C2": {}}}
    assert expected_equals(state, {"customers": {"C1": {"phone": "(555) 123-4567"}}})
    assert not expected_equals(state, {"customers": {"C1": {"phone": "5550000000"}}})
    assert not expected_equals(state, {"customers": {"C3": {}}})


# -- happy paths to expected states ------------------------------------------------
def test_scheduling_happy_path():
    b = SchedulingBackend()
    slots = b.call("find_slots", {"date": "2026-11-10"})
    assert [s["slot_id"] for s in slots["slots"]] == ["S1", "S2"]
    assert slots["result_anchors"] == ["9 AM", "3 PM"]
    r = b.call("book_appointment", {"slot_id": "S2", "name": "Ana", "phone": "(415) 555-0100"})
    assert r["result_anchors"] == ["3 PM"]
    assert expected_equals(b.state(), {"appointments": {"A0001": {
        "slot_id": "S2", "phone": "4155550100", "start": "2026-11-10T15:00", "status": "booked"}}})


def test_orders_happy_path():
    b = OrdersBackend()
    assert b.call("lookup_order", {"order_id": "1001"})["status"] == "placed"
    b.call("update_shipping_address", {"order_id": "1001", "address": "9 Bay St"})
    b.call("create_return", {"order_id": "1002", "reason": "broken"})
    assert expected_equals(b.state(), {"orders": {"1001": {"address": "9 Bay St"}},
                                       "returns": {"R0001": {"order_id": "1002"}}})


def test_billing_happy_path():
    b = BillingBackend(policy={"refund_cap": 50})
    b.call("make_payment", {"account_id": "AC-77", "amount": "$20"})
    r = b.call("issue_refund", {"account_id": "AC-77", "amount": "50", "reason": "dup"})
    assert r["result_anchors"] == ["50.00"]
    assert expected_equals(b.state(), {"accounts": {"AC-77": {"balance": "100.00",
                                                              "paid_total": "320.00"}},
                                       "refunds": {"F0001": {"amount": "50.00"}}})


def test_account_happy_path():
    b = AccountBackend()
    v = b.call("verify_identity", {"customer_id": "C-100", "dob": "March 3 1990",
                                   "zip": "94107"})
    assert v["verified"] is True
    b.call("update_email", {"customer_id": "C-100", "email": "Jordan.S@Example.com"})
    assert expected_equals(b.state(), {"customers": {"C-100": {"email": "jordan.s@example.com"}}})


def test_outbound_happy_path():
    b = OutboundBackend()
    b.call("record_confirmation", {"contact_id": "K-1", "status": "confirmed"})
    assert expected_equals(b.state(), {"contacts": {"K-1": {"confirmation": "confirmed"}}})


# -- policies ---------------------------------------------------------------------
def test_account_write_before_verify_raises_and_state_unchanged():
    b = AccountBackend()
    h = b.snapshot_hash()
    with pytest.raises(PolicyError) as ei:
        b.call("update_phone", {"customer_id": "C-100", "phone": "5550001111"})
    assert ei.value.code == "auth_required"
    assert b.snapshot_hash() == h


def test_account_failed_verify_does_not_authorize():
    b = AccountBackend()
    assert b.call("verify_identity", {"customer_id": "C-100", "dob": "1990-03-04",
                                      "zip": "94107"})["verified"] is False
    with pytest.raises(PolicyError):
        b.call("update_address", {"customer_id": "C-100", "address": "1 Main"})


def test_billing_refund_over_cap_refused():
    b = BillingBackend(policy={"refund_cap": "40.00"})
    with pytest.raises(PolicyError) as ei:
        b.call("issue_refund", {"account_id": "AC-77", "amount": "40.01", "reason": "x"})
    assert ei.value.code == "refund_over_cap"
    assert b.state()["refunds"] == {}


def test_scheduling_double_booking_refused():
    b = SchedulingBackend()
    b.call("book_appointment", {"slot_id": "S1", "name": "A", "phone": "1"})
    with pytest.raises(PolicyError) as ei:
        b.call("book_appointment", {"slot_id": "S1", "name": "B", "phone": "2"})
    assert ei.value.code == "double_booking"
    assert sorted(b.state()["appointments"]) == ["A0001", "AP-101", "AP-102"]


def test_orders_and_outbound_policies():
    with pytest.raises(PolicyError):
        OrdersBackend().call("cancel_order", {"order_id": "1002"})       # delivered
    b = OutboundBackend()
    b.call("mark_do_not_call", {"contact_id": "K-1"})
    with pytest.raises(PolicyError):
        b.call("schedule_callback", {"contact_id": "K-1", "when": "2026-11-13 10:00"})


# -- contract details -----------------------------------------------------------------
def test_bad_args_and_unknown_tool():
    b = SchedulingBackend()
    with pytest.raises(ToolArgError):
        b.call("book_appointment", {"slot_id": "S1"})
    with pytest.raises(ToolArgError):
        b.call("teleport", {})
    with pytest.raises(ToolArgError):
        b.call("cancel_appointment", {"appointment_id": "nope"})


def test_declared_anchors_override_defaults():
    b = SchedulingBackend(anchors={"book_appointment": ["three PM", "Tuesday"]})
    r = b.call("book_appointment", {"slot_id": "S2", "name": "A", "phone": "1"})
    assert r["result_anchors"] == ["three PM", "Tuesday"]


def test_reset_and_tools_schema():
    b = SchedulingBackend()
    h0 = b.snapshot_hash()
    b.call("book_appointment", {"slot_id": "S1", "name": "A", "phone": "1"})
    assert b.snapshot_hash() != h0
    b.reset()
    assert b.snapshot_hash() == h0
    b.reset({"slots": {}, "appointments": {}})
    assert b.state() == {"appointments": {}, "slots": {}}
    kinds = {t.name: t.kind for t in b.tools()}
    assert kinds["find_slots"] == "read" and kinds["book_appointment"] == "write"
    assert b.tool("book_appointment").parameters["required"] == ["name", "phone", "slot_id"]


def test_registry_has_five_domains():
    assert sorted(BACKENDS) == ["account", "billing", "orders", "outbound", "scheduling"]
    assert get_backend("billing") is BillingBackend
    with pytest.raises(KeyError):
        get_backend("pets")


# -- G2: billing identity gate (policy require_identity) --------------------------
def test_billing_verify_identity_tool_shape():
    b = BillingBackend()
    t = b.tool("verify_identity")
    assert t.kind == "read" and t.parameters["required"] == ["customer_id", "dob", "zip"]
    r = b.call("verify_identity", {"customer_id": "AC-77", "dob": "July 14 1958",
                                   "zip": "60614"})
    assert r == {"customer_id": "AC-77", "verified": True, "result_anchors": ["verified"]}
    bad = BillingBackend().call("verify_identity", {"customer_id": "AC-77",
                                                    "dob": "1958-07-15", "zip": "60614"})
    assert bad["verified"] is False and bad["result_anchors"] == ["could not verify"]
    with pytest.raises(ToolArgError):
        b.call("verify_identity", {"customer_id": "AC-99", "dob": "1958-07-14", "zip": "1"})


def test_billing_gate_off_by_default_writes_without_verify():
    b = BillingBackend()
    b.call("make_payment", {"account_id": "AC-77", "amount": "20"})
    b.call("issue_refund", {"account_id": "AC-77", "amount": "5", "reason": "dup"})
    assert expected_equals(b.state(), {"payments": {"P0001": {"amount": "20.00"}},
                                       "refunds": {"F0001": {"amount": "5.00"}}})


@pytest.mark.parametrize("tool,args", [
    ("make_payment", {"account_id": "AC-77", "amount": "20"}),
    ("issue_refund", {"account_id": "AC-77", "amount": "5", "reason": "dup"}),
])
def test_billing_gate_on_refuses_unverified_write(tool, args):
    b = BillingBackend(policy={"require_identity": True})
    h0 = b.snapshot_hash()
    with pytest.raises(PolicyError) as e:
        b.call(tool, args)
    assert e.value.code == "auth_required" and b.snapshot_hash() == h0
    b.call("verify_identity", {"customer_id": "AC-77", "dob": "1958-07-15", "zip": "60614"})
    with pytest.raises(PolicyError):
        b.call(tool, args)                         # a failed verify does not authorize
    b.call("verify_identity", {"customer_id": "AC-77", "dob": "1958-07-14", "zip": "60614"})
    b.call(tool, args)
    assert b.snapshot_hash() != h0
    b.reset()
    with pytest.raises(PolicyError):
        b.call(tool, args)                         # reset clears the per-call verified flag


def test_billing_gate_checked_before_cap():
    b = BillingBackend(policy={"require_identity": True, "refund_cap": 10})
    with pytest.raises(PolicyError) as e:
        b.call("issue_refund", {"account_id": "AC-77", "amount": "50", "reason": "x"})
    assert e.value.code == "auth_required"


def test_account_gate_stays_always_on():
    b = AccountBackend(policy={"require_identity": False})
    with pytest.raises(PolicyError) as e:
        b.call("update_phone", {"customer_id": "C-100", "phone": "4155550199"})
    assert e.value.code == "auth_required"


def test_billing_seed_overlay_overrides_identity():
    b = BillingBackend(policy={"require_identity": True},
                       seed_overlay={"accounts": {"AC-77": {"dob": "1960-01-02",
                                                            "zip": "10001"}}})
    assert b.state()["accounts"]["AC-77"]["balance"] == "120.00"      # other fields kept
    assert not b.call("verify_identity", {"customer_id": "AC-77", "dob": "1958-07-14",
                                          "zip": "60614"})["verified"]
    assert b.call("verify_identity", {"customer_id": "AC-77", "dob": "1960-01-02",
                                      "zip": "10001"})["verified"]


def test_billing_verify_on_account_without_identity_fields_is_false():
    b = BillingBackend(seed_overlay={"accounts": {"AC-5": {"holder": "X", "balance": "1",
                                                           "paid_total": "1"}}})
    assert b.call("verify_identity", {"customer_id": "AC-5", "dob": "2000-01-01",
                                      "zip": "1"})["verified"] is False


# -- G5: get_balance carries the refund cap -----------------------------------------
def test_get_balance_refund_cap_field():
    r = BillingBackend(policy={"refund_cap": 50}).call("get_balance", {"account_id": "AC-77"})
    assert r == {"account_id": "AC-77", "balance": "120.00", "refund_cap": "50.00",
                 "result_anchors": ["120.00"]}
    r = BillingBackend().call("get_balance", {"account_id": "AC-77"})
    assert r["refund_cap"] is None
    assert "refund cap" in BillingBackend().tool("get_balance").description
    with pytest.raises(PolicyError) as e:
        BillingBackend(policy={"refund_cap": 50}).call(
            "issue_refund", {"account_id": "AC-77", "amount": "50.01", "reason": "x"})
    assert e.value.code == "refund_over_cap"


# -- G4: seeded appointments ------------------------------------------------------
def test_scheduling_seed_appointments_reschedule():
    b = SchedulingBackend()
    assert {"AP-101", "AP-102"} <= set(b.state()["appointments"])
    r = b.call("reschedule_appointment", {"appointment_id": "AP-101", "slot_id": "S2"})
    assert r["start"] == "2026-11-10T15:00" and r["result_anchors"] == ["3 PM"]
    assert expected_equals(b.state(), {
        "slots": {"S2": {"booked_by": "AP-101"}, "S4": {"booked_by": None}},
        "appointments": {"AP-101": {"slot_id": "S2", "status": "booked"}}})


def test_scheduling_seed_appointments_cancel():
    b = SchedulingBackend()
    r = b.call("cancel_appointment", {"appointment_id": "AP-102"})
    assert r == {"appointment_id": "AP-102", "status": "cancelled",
                 "result_anchors": ["cancelled"]}
    assert expected_equals(b.state(), {"slots": {"S5": {"booked_by": None}},
                                       "appointments": {"AP-102": {"status": "cancelled"}}})
    with pytest.raises(ToolArgError):
        b.call("reschedule_appointment", {"appointment_id": "AP-102", "slot_id": "S1"})


def test_scheduling_seed_does_not_shift_booking_ids():
    b = SchedulingBackend()
    assert b.seed_errors() == []
    assert [s["slot_id"] for s in b.call("find_slots", {})["slots"]] == ["S1", "S2", "S3"]
    assert b.call("book_appointment", {"slot_id": "S3", "name": "Maya Chen",
                                       "phone": "202-555-0142"})["appointment_id"] == "A0001"
    assert b.call("book_appointment", {"slot_id": "S1", "name": "B",
                                       "phone": "1"})["appointment_id"] == "A0002"
    with pytest.raises(PolicyError):
        b.call("book_appointment", {"slot_id": "S4", "name": "C", "phone": "1"})


def test_next_id_skips_ids_in_use():
    b = SchedulingBackend(seed_overlay={"appointments": {"A0002": {}}})
    assert b._next_id("appointments", "A") == "A0003"     # count 1 -> A0002 taken -> A0003
    assert SchedulingBackend()._next_id("appointments", "A") == "A0001"


def test_scheduling_seed_errors():
    assert SchedulingBackend().seed_errors() == []
    bad = SchedulingBackend(seed_overlay={"appointments": {
        "A0007": {"slot_id": "S1", "name": "X", "phone": "1", "start": "2026-11-10T09:00",
                  "status": "booked"},
        "AP-9": {"slot_id": "S99", "name": "X", "phone": "1", "start": "2026-11-10T09:00",
                 "status": "booked"}}})
    errs = " | ".join(bad.seed_errors())
    assert "A0007" in errs and "in-call form" in errs and "S99" in errs
    assert "booked_by" in errs                      # S1 not marked booked by A0007


def test_merge_seed_semantics():
    from duplexbench.tools.state import merge_seed
    base = {"t": {"r1": {"a": 1, "b": 2}}, "u": {}}
    out = merge_seed(base, {"t": {"r1": {"b": 3}, "r2": {"a": 9}}})
    assert out == {"t": {"r1": {"a": 1, "b": 3}, "r2": {"a": 9}}, "u": {}}
    assert base == {"t": {"r1": {"a": 1, "b": 2}}, "u": {}}       # base untouched


def test_backend_for_scenario_applies_policy_and_seed():
    from types import SimpleNamespace

    from duplexbench.tools.mock_backends import backend_for_scenario
    s = SimpleNamespace(domain="billing", policy={"refund_cap": 25},
                        seed={"accounts": {"AC-77": {"zip": "99999"}}})
    b = backend_for_scenario(s)
    assert isinstance(b, BillingBackend) and b.state()["accounts"]["AC-77"]["zip"] == "99999"
    assert b.call("get_balance", {"account_id": "AC-77"})["refund_cap"] == "25.00"


# -- scenario `seed` block validation ------------------------------------------------
def _scen(domain, seed=None, policy=None):
    from duplexbench.scenarios.schema import Scenario
    return Scenario(id="t1", version="1", split="public-dev", domain=domain, track="C",
                    source="simulated", caller_voice="v", steps=[{"id": "s1", "text": "hi"}],
                    seed=seed or {}, policy=policy or {})


def test_validate_accepts_seed_appointments_and_rejects_bad_seed():
    from duplexbench.scenarios.loader import validate
    good = {"slots": {"S9": {"start": "2026-11-20T09:00", "provider": "Dr. Lee",
                             "booked_by": "AP-200"}},
            "appointments": {"AP-200": {"slot_id": "S9", "name": "Z", "phone": "1",
                                        "start": "2026-11-20T09:00", "status": "booked"}}}
    assert validate(_scen("scheduling", good)) == []
    errs = validate(_scen("scheduling", {"rooms": {}}))
    assert len(errs) == 1 and "unknown table 'rooms'" in errs[0]
    errs = validate(_scen("scheduling", {"appointments": {"A0001": {
        "slot_id": "S1", "name": "Z", "phone": "1", "start": "x", "status": "booked"}}}))
    assert any("in-call form" in e for e in errs)


def test_validate_billing_identity_policy():
    from duplexbench.scenarios.loader import validate
    assert validate(_scen("billing", policy={"require_identity": True})) == []
    errs = validate(_scen("billing", policy={"require_identity": "yes"}))
    assert any("require_identity" in e for e in errs)
    errs = validate(_scen("billing",
                          {"accounts": {"AC-5": {"balance": "1", "paid_total": "1"}}},
                          policy={"require_identity": True}))
    assert any("AC-5" in e and "dob and zip" in e for e in errs)


def test_empty_seed_does_not_change_pack_hash_dump():
    from duplexbench.scenarios.loader import _hash_dump
    assert "seed" not in _hash_dump(_scen("billing"))
    assert "seed" in _hash_dump(_scen("billing", {"accounts": {"AC-77": {"zip": "1"}}}))
