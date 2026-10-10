"""A-18: an AST guard against instrument, money and strategy values written into src/.

The forbidden values are every leaf of every YAML file under config/ (dicts and lists
walked): numbers (small integers included; 0 and +-1 are the only ones never scanned),
strings and dates. src/ is read as Python, not as lines, so the guard sees through
spellings a line regex misses: `25_000`, `.25`, `2.5e2`, `Decimal("0.25")`,
`date(2026, 4, 3)`, `"2026-04-03"`. Comments and string text (docstrings, messages)
are scanned for numbers too.

Some forbidden values are also ordinary code: halving by 2, a weekday code that is a
schema key, the percent bound 100. Each of those is listed in REVIEWED below, per
file and per value, with how many times it may appear. A new occurrence of any
forbidden value anywhere in src/ fails, and so does a reviewed one that went away
(the list must shrink with the code). There are no in-line opt-out markers: the
reviewed list lives here, in the test, where review sees it.
"""

import ast
import datetime
import io
import re
import tokenize
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src"
CONFIG = REPO_ROOT / "config"
NEVER_SCANNED = frozenset({Decimal(0), Decimal(1), Decimal(-1)})
MARKER = "allow-" + "literal"
# In prose a number glued to ":" or "-" is part of a clock time or a date (checked as
# words and dates), not a number of its own.
PROSE_NUMBER = re.compile(r"(?<![\w.:-])(?:\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)(?![\w:-])")
DATE_CALLS = frozenset({"date", "datetime"})


def _leaves(node: object) -> Iterator[object]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _leaves(value)
    elif isinstance(node, list):
        for value in node:
            yield from _leaves(value)
    else:
        yield node


def _number(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip().replace("_", ""))
    except InvalidOperation:
        return None
    return value.normalize() if value.is_finite() else None


def _show(number: Decimal) -> str:
    return format(number, "f")


class Forbidden:
    def __init__(self, yaml_files: list[Path]) -> None:
        self.numbers: set[Decimal] = set()
        self.words: set[str] = set()
        self.dates: set[datetime.date] = set()
        for path in yaml_files:
            for leaf in _leaves(yaml.safe_load(path.read_text())):
                if leaf is None or isinstance(leaf, bool):
                    continue
                if isinstance(leaf, int | float):
                    number = Decimal(str(leaf)).normalize()
                    if number not in NEVER_SCANNED:
                        self.numbers.add(number)
                elif isinstance(leaf, datetime.date):
                    self.dates.add(leaf)
                elif isinstance(leaf, str):
                    self.words.add(leaf)
        self._word = re.compile(
            "|".join(rf"(?<!\w){re.escape(w)}(?!\w)" for w in sorted(self.words, key=len)[::-1])
        )

    def in_text(self, text: str) -> list[str]:
        hits = [f"word {w}" for w in self._word.findall(text)]
        hits += [f"date {d.isoformat()}" for d in sorted(self.dates) if d.isoformat() in text]
        for match in PROSE_NUMBER.finditer(text):
            number = _number(match.group())
            if number is not None and number in self.numbers:
                hits.append(f"text {_show(number)}")
        return hits


def _date_call(node: ast.Call) -> datetime.date | None:
    name = node.func.id if isinstance(node.func, ast.Name) else None
    if isinstance(node.func, ast.Attribute):
        name = node.func.attr
    if name not in DATE_CALLS or len(node.args) < 3 or node.keywords:
        return None
    parts: list[int] = []
    for arg in node.args[:3]:
        if not isinstance(arg, ast.Constant) or type(arg.value) is not int:
            return None
        parts.append(arg.value)
    try:
        return datetime.date(*parts)
    except ValueError:
        return None


def scan_source(source: str, forbidden: Forbidden) -> Counter[str]:
    """Every forbidden value in one Python file, counted by kind and value."""
    found: Counter[str] = Counter()
    nodes = list(ast.walk(ast.parse(source)))
    date_parts: set[int] = set()  # a date's year, month and day count as the date
    for node in nodes:
        if isinstance(node, ast.Call):
            day = _date_call(node)
            if day is not None and day in forbidden.dates:
                found[f"date {day.isoformat()}"] += 1
                date_parts.update(id(arg) for arg in node.args[:3])
    for node in nodes:
        if id(node) in date_parts:
            continue
        if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
            continue
        value = node.value
        if isinstance(value, int | float):
            number = Decimal(str(value)).normalize()
            if number in forbidden.numbers:
                found[f"code {_show(number)}"] += 1
        elif isinstance(value, str):
            spelled = _number(value)
            if spelled is not None and spelled in forbidden.numbers:
                found[f"code {_show(spelled)}"] += 1
            else:
                found.update(forbidden.in_text(value))
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            found.update(forbidden.in_text(token.string))
    return found


def scan_tree(root: Path, forbidden: Forbidden) -> dict[str, Counter[str]]:
    result: dict[str, Counter[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        hits = scan_source(path.read_text(), forbidden)
        if hits:
            result[path.relative_to(root).as_posix()] = hits
    return result


def yaml_files() -> list[Path]:
    return sorted(CONFIG.rglob("*.yaml")) + sorted(CONFIG.rglob("*.yml"))


# REVIEWED: forbidden values that are ordinary code or prose, file by file, with the
# number of times each may appear. Filled from the scan and reviewed one by one (grind
# stage 2026-10-10 09:06 UTC, A-18); see docs/decisions-pending.md, row A-18.
# What is reviewed here, by kind (none of it is an instrument, money or strategy value):
# - code 2: halving, pairs, squaring, two-sided checks, exit codes, 2**n backoff.
# - code 3/4/5/6 in market.py: "yyyymmdd" slicing [:4], [4:6]; date.weekday() 5 is
#   Saturday; a tuple index. config.py: the nth-weekday range (-1, 1..5) of holiday
#   rules. levels.py: typical price (high + low + close) / 3. path.py: the backtest's
#   price path reaches its vertices at a third and two thirds of the minute and closes
#   5 seconds before its end (a path model, not a trading rule). _KEEP = 4: cache sizes.
#   binding.py: 2**6, the cap on doubling a backoff.
# - code 8: uuid4().hex[:8] in ids. code -2: the "-2" suffix of a stop id.
# - code 100: _PERCENT_MAX, the upper bound of a percentage (was the one in-line marker).
# - words: holiday names, weekday codes and rule names that are the schema keys the
#   config is parsed with (core/config.py, core/holiday_rules.py, core/calendar.py);
#   "FOP" in a comment listing IBKR security types.
# - text N: small counts and item numbers in docstrings, comments and messages.
REVIEWED: dict[str, dict[str, int]] = {
    "footprint/agents/_series.py": {
        "code 2": 1,
        "code 4": 1,
    },
    "footprint/agents/contract_picker.py": {
        "code 2": 1,
    },
    "footprint/agents/levels.py": {
        "code 2": 2,
        "code 3": 1,
    },
    "footprint/agents/momentum_reader.py": {
        "code 2": 3,
        "text 2": 1,
    },
    "footprint/agents/range_reader.py": {
        "code 2": 4,
    },
    "footprint/agents/trend_reader.py": {
        "code 2": 6,
    },
    "footprint/agents/zero_day.py": {
        "code 2": 1,
    },
    "footprint/backtest/__main__.py": {
        "code 2": 2,
        "text 2": 1,
    },
    "footprint/backtest/engine.py": {
        "code 2": 1,
        "text 2": 1,
    },
    "footprint/backtest/options_model.py": {
        "code 2": 1,
    },
    "footprint/backtest/path.py": {
        "code 2": 1,
        "code 3": 2,
        "code 5": 1,
    },
    "footprint/backtest/readout.py": {
        "code 2": 1,
    },
    "footprint/brokers/ibkr/adapter.py": {
        "text 2": 1,
    },
    "footprint/brokers/ibkr/binding.py": {
        "code 2": 3,
        "code 6": 1,
        "text 2": 1,
        "text 3": 1,
    },
    "footprint/brokers/ibkr/check.py": {
        "code 2": 1,
    },
    "footprint/brokers/ibkr/gateway.py": {
        "code 2": 1,
        "word FOP": 1,
    },
    "footprint/brokers/ibkr/market.py": {
        "code 2": 4,
        "code 3": 1,
        "code 4": 2,
        "code 5": 1,
        "code 6": 2,
    },
    "footprint/core/calendar.py": {
        "text 7": 1,
        "word weekend": 1,
    },
    "footprint/core/config.py": {
        "code 100": 1,
        "code 2": 7,
        "code 3": 1,
        "code 4": 1,
        "code 5": 1,
        "text 2": 1,
        "text 8": 1,
        "word FRI": 1,
        "word MON": 1,
        "word THU": 1,
        "word TUE": 1,
        "word WED": 1,
    },
    "footprint/core/holiday_rules.py": {
        "word Christmas Day": 1,
        "word Christmas Eve": 1,
        "word Day after Thanksgiving": 1,
        "word Good Friday": 1,
        "word Independence Day": 1,
        "word Juneteenth": 1,
        "word Labor Day": 1,
        "word Martin Luther King Jr. Day": 1,
        "word Memorial Day": 1,
        "word New Year's Day": 1,
        "word Presidents Day": 1,
        "word Thanksgiving": 1,
        "word early_close": 4,
        "word not_saturday": 3,
        "word weekday_only": 3,
        "word weekend": 5,
    },
    "footprint/core/interfaces.py": {
        "text 2": 1,
        "text 4": 1,
    },
    "footprint/core/options.py": {
        "code 2": 1,
    },
    "footprint/core/series.py": {
        "code 4": 1,
    },
    "footprint/data/aggregate.py": {
        "code 4": 1,
    },
    "footprint/decision/console/desmond.py": {
        "text 2": 1,
    },
    "footprint/decision/pipeline.py": {
        "text 2": 1,
        "text 3": 1,
        "text 4": 1,
    },
    "footprint/decision/risk/gate.py": {
        "code 2": 1,
    },
    "footprint/decision/sizing.py": {
        "code 2": 1,
    },
    "footprint/decision/stop.py": {
        "code 2": 2,
    },
    "footprint/execution/channels.py": {
        "code 2": 1,
        "text 10": 1,
        "text 3": 1,
        "text 7": 1,
    },
    "footprint/execution/executor/diego.py": {
        "code -2": 1,
        "code 8": 1,
        "text 2": 1,
    },
    "footprint/execution/management/malik.py": {
        "code 2": 1,
        "code 8": 1,
        "text 2": 6,
        "text 3": 2,
        "text 4": 1,
        "text 9": 1,
    },
    "footprint/execution/orders.py": {
        "text 2": 1,
        "text 4": 1,
    },
    "footprint/execution/runner.py": {
        "text 2": 1,
        "text 3": 1,
        "text 4": 1,
    },
    "footprint/live/__main__.py": {
        "code 2": 3,
    },
    "footprint/live/alarm_setup.py": {
        "code 2": 6,
        "text 4": 1,
    },
}


def test_every_yaml_file_and_list_is_read() -> None:
    files = yaml_files()
    assert CONFIG / "instruments" / "nq.yaml" in files
    assert len(files) >= 5
    forbidden = Forbidden(files)
    assert {Decimal("0.25"), Decimal(20), Decimal(25000), Decimal(4)} <= forbidden.numbers
    assert {"NQ", "CME", "04:00"} <= forbidden.words
    assert forbidden.dates  # holiday lists
    assert not forbidden.numbers & NEVER_SCANNED


def compare(
    found: dict[str, dict[str, int]], reviewed: dict[str, dict[str, int]]
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """(new: more than reviewed, gone: reviewed but no longer in src/)."""
    new = {
        path: {k: n for k, n in hits.items() if n > reviewed.get(path, {}).get(k, 0)}
        for path, hits in found.items()
    }
    gone = {
        path: {k: n for k, n in hits.items() if found.get(path, {}).get(k, 0) < n}
        for path, hits in reviewed.items()
    }
    return (
        {path: hits for path, hits in new.items() if hits},
        {path: hits for path, hits in gone.items() if hits},
    )


def test_src_has_no_unreviewed_literal() -> None:
    found = {path: dict(hits) for path, hits in scan_tree(SRC, Forbidden(yaml_files())).items()}
    new, gone = compare(found, REVIEWED)
    assert not new, f"hardcoded config values under src/ (move them to config/): {new}"
    assert not gone, f"REVIEWED lists more than src/ holds; shrink it: {gone}"


def test_compare_finds_new_and_gone_entries() -> None:
    reviewed = {"a.py": {"code 2": 2}, "b.py": {"word NQ": 1}}
    assert compare({"a.py": {"code 2": 2}, "b.py": {"word NQ": 1}}, reviewed) == ({}, {})
    new, gone = compare({"a.py": {"code 2": 3, "code 4": 1}}, reviewed)
    assert new == {"a.py": {"code 2": 3, "code 4": 1}}
    assert gone == {"b.py": {"word NQ": 1}}
    assert compare({"a.py": {"code 2": 1}, "b.py": {"word NQ": 1}}, reviewed) == (
        {},
        {"a.py": {"code 2": 2}},
    )


def test_src_has_no_opt_out_marker() -> None:
    marked = [p for p in SRC.rglob("*.py") if MARKER in p.read_text()]
    assert not marked, f"in-line opt-out markers are not allowed: {marked}"


def test_scanner_sees_through_every_spelling(tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        "money:\n  bankroll: 25000\n  tick: 0.25\n  trades: 4\n"
        "symbols: [NQ, '04:00']\nholidays:\n  - 2026-04-03\n"
    )
    forbidden = Forbidden([config])
    planted = (
        "from datetime import date\n"
        "from decimal import Decimal\n"
        "A = 25_000\n"
        "B = .25\n"
        "C = Decimal('0.25')\n"
        "D = 4\n"
        "E = 2.5e4\n"
        "F = date(2026, 4, 3)\n"
        "G = '2026-04-03'\n"
        "H = 'NQ'\n"
        "I = 'opens 04:00'\n"
        "J = 1  # a 25,000 bankroll? no: 25000\n"
        "K = [0, 1, -1, 3, 'NQX', 2027]\n"
        '"""Docstring: tick .25"""\n'
    )
    hits = scan_source(planted, forbidden)
    assert hits == Counter(
        {
            "code 25000": 2,  # 25_000 and 2.5e4
            "code 0.25": 2,  # .25 and Decimal('0.25')
            "code 4": 1,
            "date 2026-04-03": 2,
            "word NQ": 1,
            "word 04:00": 1,
            "text 25000": 1,  # the comment
            "text 0.25": 1,  # the docstring
        }
    )
