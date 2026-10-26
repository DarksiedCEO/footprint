"""The paper-account boundary. Hard-coded on purpose: no .env value, YAML key, command-line
argument or other setting can change it (decision D33, remediation of AEGIS H1).

    PAPER_ACCOUNT  IBKR paper account ids: "DU" then letters or digits (e.g. DU1234567).
                   Every other id (U..., F..., I..., DF...) is refused.
    LIVE_PORTS     IB Gateway's live port (4001) and TWS's live port (7496): refused even
                   with a paper account, so a misdirected .env fails at start-up.

`check_paper` is called when the connection settings are made (so nothing can be
built with a live account or port), again by the binding before every connection
attempt, and on IBKR's list of the login's accounts: a login that manages any account
that is not a paper account is a live login, and is refused.

Going live is not built. It must be a code change, reviewed and approved by Andre,
never a setting.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

PAPER_ACCOUNT: Final = re.compile(r"DU[A-Z0-9]+", re.ASCII)
LIVE_PORTS: Final = frozenset({4001, 7496})


class LiveAccountRefused(ValueError):
    """A live account, a live port, or a live login: never connected to."""


class AccountNotChecked(ConnectionError):
    """An order call (place, cancel, broker-wide cancel) before this session's account
    check passed, or after the connection was refused: nothing is sent. A
    ConnectionError, like a call on a binding with no connection, so callers take the
    path they already take for "nothing was sent" (independent audit L-c)."""


def is_paper_account(account: str) -> bool:
    return PAPER_ACCOUNT.fullmatch(account) is not None


def check_paper(account: str, port: int) -> None:
    """Raise LiveAccountRefused unless `account` is a paper account and `port` is not
    a live port."""
    if not is_paper_account(account):
        raise LiveAccountRefused(
            f"{account!r} is not an IBKR paper account (paper ids begin with DU): "
            "this program runs IBKR's paper account only"
        )
    if port in LIVE_PORTS:
        raise LiveAccountRefused(
            f"port {port} is an IBKR live port (live: {sorted(LIVE_PORTS)}; IB Gateway "
            "paper is 4002): this program runs IBKR's paper account only"
        )


def check_paper_login(accounts: Iterable[str]) -> None:
    """Raise LiveAccountRefused if the login manages any account that is not paper."""
    live = sorted(a for a in accounts if not is_paper_account(a))
    if live:
        raise LiveAccountRefused(
            f"the login manages non-paper account(s) {live}: this is a live login"
        )


def check_orders_allowed(refused: str | None, account_checked: bool) -> None:
    """Raise AccountNotChecked unless this session's account check passed and the
    connection was never refused. The binding asks this before every order call."""
    if refused is not None:
        raise AccountNotChecked(f"the IBKR connection was refused ({refused}): nothing is sent")
    if not account_checked:
        raise AccountNotChecked(
            "this session's account check has not passed (IBKR has not yet listed the "
            "login's accounts, or the session ended): nothing is sent"
        )
