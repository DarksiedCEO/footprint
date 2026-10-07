"""The IBKR connection settings and the .env reader, without ibapi.

Kept apart from the binding so the paper boundary that guards them can be built and
tested where IBKR's ibapi is not installed (independent audit). The binding
re-exports both names.
"""

from __future__ import annotations

from dataclasses import dataclass

from footprint.brokers.ibkr.paper import check_paper
from footprint.core.config import IbkrConfig


@dataclass(frozen=True, slots=True)
class IbConnectionSettings:
    host: str
    port: int
    client_id: int
    account: str
    reconnect_seconds: float
    escalate_after_failures: int
    max_reconnect_seconds: float
    min_healthy_session_seconds: float

    def __post_init__(self) -> None:
        check_paper(self.account, self.port)  # LiveAccountRefused: never built

    @classmethod
    def from_env(cls, env: dict[str, str], ibkr: IbkrConfig) -> IbConnectionSettings:
        """From BROKER_HOST, BROKER_PORT, BROKER_ACCOUNT_ID, BROKER_CLIENT_ID."""
        missing = [
            k
            for k in ("BROKER_HOST", "BROKER_PORT", "BROKER_ACCOUNT_ID", "BROKER_CLIENT_ID")
            if not env.get(k, "").strip()
        ]
        if missing:
            raise ValueError(f"missing in .env: {', '.join(missing)}")
        try:
            port, client_id = int(env["BROKER_PORT"]), int(env["BROKER_CLIENT_ID"])
        except ValueError as exc:
            raise ValueError("BROKER_PORT and BROKER_CLIENT_ID must be whole numbers") from exc
        return cls(
            host=env["BROKER_HOST"].strip(),
            port=port,
            client_id=client_id,
            account=env["BROKER_ACCOUNT_ID"].strip(),
            reconnect_seconds=float(ibkr.reconnect_seconds),
            escalate_after_failures=ibkr.escalate_after_failures,
            max_reconnect_seconds=float(ibkr.max_reconnect_seconds),
            min_healthy_session_seconds=float(ibkr.min_healthy_session_seconds),
        )


def read_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments ignored. No shell expansion."""
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            key, sep, value = text.partition("=")
            if not sep or not key.strip():
                raise ValueError(f"{path}:{number}: expected KEY=VALUE")
            env[key.strip()] = value.strip()
    return env
