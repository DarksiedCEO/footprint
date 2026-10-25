"""option_model.reference_price in config/backtest.yaml (BH-2): a positive futures price,
or null for no rescaling. The shipped value is a DEFAULT (draft, Andre BH-2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from footprint.backtest.settings import load_backtest
from footprint.core.config import ConfigError
from tests.backtest.conftest import config_copy
from tests.conftest import REPO_ROOT


def test_the_shipped_reference_is_the_draft_default() -> None:
    settings = load_backtest(REPO_ROOT / "config" / "backtest.yaml")
    assert settings.option_model.reference_price == Decimal(20000)
    text = (REPO_ROOT / "config" / "backtest.yaml").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines() if "reference_price:" in x)
    assert "DEFAULT (draft, Andre BH-2)" in line


def test_null_turns_rescaling_off(tmp_path: Path) -> None:
    settings = load_backtest(config_copy(tmp_path / "c", None) / "backtest.yaml")
    assert settings.option_model.reference_price is None


@pytest.mark.parametrize("value", ["0", "-20000", "abc", "true", "[20000]", ".nan"])
def test_a_bad_reference_is_refused(tmp_path: Path, value: str) -> None:
    path = config_copy(tmp_path / "c", Decimal(1)) / "backtest.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("reference_price: 1", f"reference_price: {value}"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="reference_price"):
        load_backtest(path)


def test_the_key_is_required(tmp_path: Path) -> None:
    path = config_copy(tmp_path / "c", Decimal(1)) / "backtest.yaml"
    lines = [x for x in path.read_text(encoding="utf-8").splitlines() if "reference_price" not in x]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="reference_price"):
        load_backtest(path)
