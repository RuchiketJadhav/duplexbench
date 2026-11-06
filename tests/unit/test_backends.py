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
    assert list(b.state()["appointments"]) == ["A0001"]


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
