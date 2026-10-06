"""No instrument or money-model value may be written into anything under src/.

The forbidden values are taken from the YAML profiles themselves, so the test
follows the config. Integers below SMALL_INT_LIMIT (dte bounds, trade counts)
are not scanned for: they are indistinguishable from ordinary code constants
such as the -1/0/+1 scores. A line may opt out with ALLOW_MARKER plus a reason.
"""

import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import yaml

from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src"
SMALL_INT_LIMIT = 10
ALLOW_MARKER = "allow-literal:"
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def leaves(node: object) -> Iterator[object]:
    if isinstance(node, dict):
        for value in node.values():
            yield from leaves(value)
    else:
        yield node


def forbidden_numbers(*profiles: Path) -> set[Decimal]:
    numbers: set[Decimal] = set()
    for profile in profiles:
        for leaf in leaves(yaml.safe_load(profile.read_text())):
            if isinstance(leaf, int | float) and not isinstance(leaf, bool):
                number = Decimal(str(leaf))
                if number >= SMALL_INT_LIMIT or number != number.to_integral_value():
                    numbers.add(number)
    return numbers


def forbidden_words(instrument: Path) -> set[str]:
    """Symbol, exchange and session clock times."""
    return {
        leaf
        for leaf in leaves(yaml.safe_load(instrument.read_text()))
        if isinstance(leaf, str) and "/" not in leaf
    }


def violations(root: Path, numbers: set[Decimal], words: set[str]) -> list[str]:
    word_pattern = re.compile("|".join(rf"(?<!\w){re.escape(word)}(?!\w)" for word in words))
    found = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if ALLOW_MARKER in line:
                continue
            hits = [m.group() for m in NUMBER.finditer(line) if Decimal(m.group()) in numbers]
            hits += word_pattern.findall(line)
            if hits:
                found.append(f"{path.relative_to(root)}:{line_number}: {hits}")
    return found


# Every exemption must be reviewed: adding a marker under src/ means bumping this.
EXPECTED_ALLOW_MARKERS = 1


def count_allow_markers(root: Path) -> int:
    return sum(
        path.read_text().count(ALLOW_MARKER)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def test_allow_marker_count_matches_the_reviewed_number() -> None:
    assert count_allow_markers(SRC) == EXPECTED_ALLOW_MARKERS, (
        f"the number of '{ALLOW_MARKER}' exemptions under src/ changed; "
        "justify the new one in review and update EXPECTED_ALLOW_MARKERS"
    )


def test_profiles_yield_the_expected_forbidden_values(
    instrument_path: Path, risk_path: Path, strategy_path: Path, execution_path: Path
) -> None:
    numbers = forbidden_numbers(instrument_path, risk_path, strategy_path, execution_path)
    assert {Decimal("20"), Decimal("0.25"), Decimal("100"), Decimal("25000")} <= numbers
    assert forbidden_words(instrument_path) == {
        "NQ",
        "CME",
        "USD",
        "04:00",
        "14:00",
        "15:00",
        "03:59",
        "06:30",
        "13:00",
        "10:00",  # the early closes
    }


def test_src_contains_no_literal_instrument_values(
    instrument_path: Path, risk_path: Path, strategy_path: Path, execution_path: Path
) -> None:
    found = violations(
        SRC,
        forbidden_numbers(instrument_path, risk_path, strategy_path, execution_path),
        forbidden_words(instrument_path),
    )
    assert not found, "hardcoded profile values under src/:\n" + "\n".join(found)


def test_scanner_catches_planted_values(
    instrument_path: Path, risk_path: Path, strategy_path: Path, tmp_path: Path
) -> None:
    (tmp_path / "planted.py").write_text(
        "POINT_VALUE = 20\n"
        "TICK = 0.25\n"
        "x = round(price / 100.0)\n"
        'SYMBOL = "NQ"\n'
        'OPEN = "04:00"\n'
        "fine = [1, 2, 3, 200, 1.25, 2026]\n"
        "limit = 100  # allow-literal: percent\n"
    )
    found = violations(
        tmp_path,
        forbidden_numbers(instrument_path, risk_path, strategy_path),
        forbidden_words(instrument_path),
    )
    assert [line.split(":")[1] for line in found] == ["1", "2", "3", "4", "5"]
