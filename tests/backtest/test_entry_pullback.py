"""Q5.3 slice 2b-2: "pullback" is a selectable entry rule.

The factory builds the pullback trigger (slice 2b-1) from the live spike-test numbers
(`strategy.volume`, the instrument's prime-session time zone: the same ones "breakout"
uses) and the pullback lifetime in config/backtest.yaml (slice 2a), on the pipeline's
1-minute bars. Only the momentum read changes; every other gate still decides (proved
for any trigger by the seam tests). "breakout" and the run with no rule named are
unchanged. Synthetic bars say NOTHING about profitability.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from footprint.backtest import rules
from footprint.backtest.__main__ import main
from footprint.backtest.pullback import PullbackSettings, PullbackTrigger
from footprint.backtest.settings import BacktestSettings, RuleSettings
from footprint.core.config import Profiles
from footprint.core.models import Bar, Read
from tests.backtest.conftest import TRADE_FROM
from tests.backtest.test_cli import write_bars
from tests.backtest.test_rules_seam import _run
from tests.conftest import REPO_ROOT

ONE_MINUTE = timedelta(minutes=1)


def test_pullback_is_registered_and_built_from_the_live_spike_numbers_and_the_backtest_file(
    profiles: Profiles, settings: BacktestSettings
) -> None:
    trigger = rules.ENTRY_RULES["pullback"](profiles, settings.rules)
    assert type(trigger) is PullbackTrigger
    assert trigger._volume is profiles.strategy.volume
    assert trigger._timezone == profiles.instrument.prime_session.timezone
    assert trigger._lifetime == settings.rules.pullback.lifetime
    assert trigger._interval == ONE_MINUTE


def test_a_pullback_run_reads_every_decision_through_the_pullback_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: BacktestSettings
) -> None:
    built: list[PullbackTrigger] = []
    seen: list[Read] = []
    real_factory = rules.ENTRY_RULES["pullback"]
    real_read = PullbackTrigger.read

    def spy_factory(profiles: Profiles, rule_settings: RuleSettings) -> PullbackTrigger:
        assert rule_settings == settings.rules  # the numbers in config/backtest.yaml
        trigger = real_factory(profiles, rule_settings)
        assert isinstance(trigger, PullbackTrigger)
        built.append(trigger)
        return trigger

    def spy_read(self: PullbackTrigger, bars: tuple[Bar, ...]) -> Read:
        seen.append(real_read(self, bars))
        return seen[-1]

    monkeypatch.setitem(rules.ENTRY_RULES, "pullback", spy_factory)
    monkeypatch.setattr(PullbackTrigger, "read", spy_read)
    out, printed = _run(tmp_path, "--entry", "pullback")
    assert len(built) == 1
    assert seen
    assert all(r.reason.startswith("pullback: ") for r in seen)
    assert "entry rule" in printed
    assert "pullback" in printed
    assert (out / "readout.json").exists()


def _config_with_lifetime(tmp_path: Path) -> Path:
    """A copy of config/ whose pullback lifetime differs from the repo's."""
    config = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config)
    path = config / "backtest.yaml"
    text = path.read_text(encoding="utf-8")
    old, new = "    lifetime_minutes: 15 ", "    lifetime_minutes: 7 "
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), encoding="utf-8")
    return config


def test_a_pullback_run_uses_and_records_the_lifetime_of_the_named_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G48-3: the readout names the pullback number the run used (as G47-1 for the
    # stair); G47-2 pattern: it comes from --config, not from the repo's own config/.
    config = _config_with_lifetime(tmp_path)
    built: list[PullbackTrigger] = []
    real_factory = rules.ENTRY_RULES["pullback"]

    def spy_factory(profiles: Profiles, rule_settings: RuleSettings) -> PullbackTrigger:
        trigger = real_factory(profiles, rule_settings)
        assert isinstance(trigger, PullbackTrigger)
        built.append(trigger)
        return trigger

    monkeypatch.setitem(rules.ENTRY_RULES, "pullback", spy_factory)
    bars = tmp_path / "bars.csv"
    write_bars(bars)
    out = tmp_path / "run"
    with contextlib.redirect_stdout(io.StringIO()) as printed:
        main(
            [
                *("--bars", str(bars), "--out", str(out), "--config", str(config)),
                *("--trade-from", TRADE_FROM.isoformat(), "--entry", "pullback"),
                *("--rolls", "none"),
            ]
        )
    assert [t._lifetime for t in built] == [PullbackSettings(lifetime_minutes=7).lifetime]
    recorded = json.loads((out / "readout.json").read_text(encoding="utf-8"))["assumptions"]
    assert recorded["entry rule"] == "pullback"
    assert {k: v for k, v in recorded.items() if k.startswith("rule ")} == {
        "rule pullback lifetime_minutes": "7",
    }
    assert "- rule pullback lifetime_minutes: 7" in printed.getvalue()
