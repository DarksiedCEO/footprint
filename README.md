# Footprint

A Python trading system. **This is the skeleton only**: typed models, the seams
between floors, validated config and a CSV bar source. There is no trading
logic, no indicators, no order or broker code, and no AI/LLM calls.

## Layout

```
config/
  instruments/nq.yaml    instrument profile (all instrument numbers live here)
  risk.yaml              money model
src/footprint/
  core/models.py         Bar, Read, Signal, Trade (frozen, self-validating)
  core/interfaces.py     Protocols: DataSource, Broker, Reader
  core/config.py         loads + validates the YAML profiles; raises ConfigError
  data/                  floor 1: data sources (CSV file source)
  agents/                floor 2: crew (empty; roster in agents/README.md)
  decision/              floor 3: console + risk layer (empty)
  execution/             floor 4: execution, trade management, journal (empty)
tests/
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

## Development

Requires [uv](https://docs.astral.sh/uv/); Python 3.12 is pinned in `.python-version`.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

## Secrets

Credentials go in a git-ignored `.env` only. Copy `.env.example` and fill it in;
never commit real values.
