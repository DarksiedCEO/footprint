# Footprint

An autonomous NQ futures-options trading system for **Interactive Brokers' paper
account only** (hard-coded: `src/footprint/brokers/ibkr/paper.py`; going live is not
built). Nothing is merged without Andre's review; see `docs/decisions-pending.md` for
every decision and its status, and `docs/live-paper-runbook.md` to run it.

## Layout

```
config/
  instruments/nq.yaml    instrument profile and exchange calendar (holidays, early
                         closes, no-trade days, calendar_through: fail closed after)
  risk.yaml              money model
  strategy.yaml          signal thresholds
  execution.yaml         execution, alarms, IBKR connection timing
  backtest.yaml          backtest settings
src/footprint/
  core/                  models, validated config, calendar
  data/                  bar sources, news blackouts
  agents/                readers (momentum, range, trend, volume), levels, zero-day,
                         contract picker
  decision/              console, pipeline, risk gate, stop keeper
  execution/             executor, management, reconcile, journal, ledger, runner,
                         alarm line and phone channels (Telegram, Twilio)
  brokers/ibkr/          IBKR binding (ibapi), adapter, market data, paper boundary,
                         connection settings (no ibapi), read-only check
  backtest/              backtest engine and option model
  live/                  the live paper program and the alarm setup tool
tests/                   unit, integration (against fake IBKR / simulated broker);
                         no test can reach the network
```

## Rules the tests enforce

- Models reject invalid data (naive or non-UTC timestamps, high < low, prices <= 0, bad scores).
- A `Trade` always has a log that opens with a `SCANNING` event, and can only move along the allowed state transitions;
  `CLOSED` is terminal.
- A `Read` is an opinion only: its fields are exactly `agent_id, ts, score, reason`.
- Profiles fail loudly on missing, unknown or out-of-range values.
- No value from the YAML profiles is hardcoded anywhere under `src/`, and the number of
  `allow-literal:` exemptions is pinned in the test.
- `.env` is git-ignored.
- No live account, live port or configuration can get past the paper boundary.
- No test reaches the network (Twilio, Telegram, IBKR): an autouse guard blocks it.

## Development

Requires [uv](https://docs.astral.sh/uv/); Python 3.12 is pinned in `.python-version`.
IBKR's `ibapi` is not on PyPI: install it from IBKR's download with the pinned hash,
and again after every `uv sync` (which removes it).

```sh
uv sync
uv run python scripts/install_ibapi.py      # after every uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
FOOTPRINT_REQUIRE_IBAPI=1 uv run pytest     # fails, not skips, without ibapi
```

## Secrets

Credentials go in a git-ignored `.env` only. Copy `.env.example` and fill it in;
never commit real values.
