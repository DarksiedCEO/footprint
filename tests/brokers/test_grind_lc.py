"""L-c: the binding's order calls (place, cancel, broker-wide cancel) refuse until
this session's account check has passed. The rule itself lives in paper.py (no ibapi),
so it is tested here wherever the suite runs; the binding's use of it is tested in
tests/brokers/test_ibkr_binding.py (needs ibapi: CI)."""

from __future__ import annotations

import pytest

from footprint.brokers.ibkr.paper import AccountNotChecked, check_orders_allowed


def test_orders_are_allowed_only_after_the_account_check_passed() -> None:
    check_orders_allowed(refused=None, account_checked=True)  # no raise


def test_orders_refuse_before_the_account_check() -> None:
    with pytest.raises(AccountNotChecked, match="account check has not passed"):
        check_orders_allowed(refused=None, account_checked=False)


@pytest.mark.parametrize("checked", [True, False])
def test_orders_refuse_for_good_once_the_connection_was_refused(checked: bool) -> None:
    with pytest.raises(AccountNotChecked, match=r"refused \(the login manages a live login\)"):
        check_orders_allowed(refused="the login manages a live login", account_checked=checked)


def test_the_refusal_is_a_connection_error() -> None:
    # Callers already treat a ConnectionError as "nothing was sent" (a disconnected
    # binding raises one): an order call refused here takes the same path.
    assert issubclass(AccountNotChecked, ConnectionError)
