"""M3-T02 / BUILD_PLAN §2.10: reservation at call admission."""
import pytest

from duplexbench.budget import Budget, BudgetExceeded


def test_plan_case():
    b = Budget(max_usd=10)
    r1 = b.reserve(4)
    b.reserve(4)
    with pytest.raises(BudgetExceeded):
        b.reserve(4)
    b.settle(r1, 2)
    b.reserve(4)                       # settled 2 + reserved 4 + 4 = 10 <= 10
    assert b.remaining == pytest.approx(0)


def test_double_settle_refused():
    b = Budget(5)
    r = b.reserve(1)
    b.settle(r, 1)
    with pytest.raises(ValueError):
        b.settle(r, 1)


def test_unknown_actual_settles_at_projection():
    b = Budget(5)
    r = b.reserve(2)
    b.settle(r, None)
    assert b.settled == 2 and b.unknown_actual == 1


def test_overrun_counts_against_later_admissions():
    b = Budget(10)
    r = b.reserve(3)
    b.settle(r, 8)                     # actual exceeded the projection
    with pytest.raises(BudgetExceeded):
        b.reserve(3)
    b.reserve(2)


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        Budget(-1)
    with pytest.raises(ValueError):
        Budget(1).reserve(-0.5)
