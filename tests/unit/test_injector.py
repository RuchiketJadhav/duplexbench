"""M4-T03: latency and failure injector on the VirtualClock."""
from duplexbench.scenarios.schema import ToolDef
from duplexbench.tools.latency_injector import Injector
from duplexbench.tools.mock_backends.account import AccountBackend
from duplexbench.tools.mock_backends.scheduling import SchedulingBackend

MS = 1_000_000
BOOK = {"slot_id": "S2", "name": "Ana", "phone": "4155550100"}


def _td(failure="none", latency_ms=2000, kind="write"):
    return ToolDef(name="book_appointment", kind=kind, latency_ms=latency_ms, failure=failure)


def _run(run_virtual, failure, latency_ms=2000, start_at_ms=100):
    b = SchedulingBackend()

    async def main(clock):
        await clock.sleep_until_ns(start_at_ms * MS)
        return await Injector(clock, _td(failure, latency_ms)).apply(
            lambda: b.call("book_appointment", BOOK))

    inj, _ = run_virtual(main)
    return inj, b


def test_none_sleeps_latency_then_runs(run_virtual):
    inj, b = _run(run_virtual, "none")
    assert (inj.status, inj.failure, inj.latency_ms) == (200, "none", 2000)
    assert (inj.start_ns, inj.result_ns) == (100 * MS, 2100 * MS)
    assert inj.body["appointment_id"] == "A0001" and inj.body["result_anchors"] == ["3 PM"]
    assert "A0001" in b.state()["appointments"]


def test_timeout_has_no_result_and_no_write(run_virtual):
    inj, b = _run(run_virtual, "timeout", latency_ms=5000)
    assert (inj.status, inj.body, inj.failure) == (504, None, "timeout")
    assert inj.result_ns == inj.start_ns + 5000 * MS
    assert "A0001" not in b.state()["appointments"]


def test_error_is_http_500_body(run_virtual):
    inj, b = _run(run_virtual, "error")
    assert inj.status == 500 and inj.body["error"] == "internal_error"
    assert inj.failure == "error" and inj.result_ns == 2100 * MS
    assert "A0001" not in b.state()["appointments"]


def test_partial_drops_one_required_field(run_virtual):
    inj, _ = _run(run_virtual, "partial")
    assert inj.status == 200 and inj.failure == "partial"
    assert set(inj.body) == {"start", "result_anchors"}      # appointment_id dropped (sorted first)


def test_zero_latency_does_not_advance(run_virtual):
    inj, _ = _run(run_virtual, "none", latency_ms=0)
    assert inj.start_ns == inj.result_ns == 100 * MS


def test_policy_error_is_403(run_virtual):
    b = AccountBackend()

    async def main(clock):
        td = ToolDef(name="update_phone", kind="write", latency_ms=0)
        return await Injector(clock, td).apply(
            lambda: b.call("update_phone", {"customer_id": "C-100", "phone": "1"}))

    inj, _ = run_virtual(main)
    assert inj.status == 403 and inj.policy_error == "auth_required"
    assert inj.body == {"error": "policy_violation", "code": "auth_required",
                        "detail": "write phone before verify_identity"}
