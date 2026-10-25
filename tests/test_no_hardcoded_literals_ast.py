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
# The number is atomic: "900.0-" is not read back as "900" (G21-3). A "$" end that a
# hyphen chain reads ("900-$1000") is left to CHAIN_NUMBER below; one after a hyphen no
# chain takes ("x6-$900", "10:30-$900") is read here (G22-1).
PROSE_NUMBER = re.compile(r"(?<![\w.:-])(?>\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)(?![\w:-])")
# A number written with thousands separators ("$1,800", "25,000") is also read whole
# (G18-2); PROSE_NUMBER alone would split it at the commas.
GROUPED_NUMBER = re.compile(r"(?<![\w.:,-])(?>\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?![\w:-]|,\d)")
# A minus that is not glued to a word, a digit or another minus is a sign ("-900",
# "-1,800"); glued to a digit it is a date or a range ("2026-04-03", "2100-2199") (G19-1).
SIGNED_NUMBER = re.compile(
    r"(?<![\w.:,-])-((?:\d{1,3}(?:,\d{3})+|\d[\d_]*)(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)(?![\w:-]|,\d)"
)
# A hyphen between numbers is a range or a chain ("900-1000", "$900-$1,000",
# "750-900-1000", ".25-.5"), not a sign: every end is read as a plain value (G20-1,
# G21-1, G21-2), once, "$" ends included. The chain is atomic, so a chain glued to a word
# or a colon reads nothing, and no end is read back shorter than written (G21-3). Only
# the ISO date shape is left out: the date check reads dates.
_CHAIN_PART = r"(?:(?:\d{1,3}(?:,\d{3})+|\d[\d_]*)(?:\.\d+)?|\.\d+)"
CHAIN_NUMBER = re.compile(rf"(?<![\w.:,-])(?>{_CHAIN_PART}(?:-\$?{_CHAIN_PART})+)(?![\w:-]|[,.]\d)")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
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


def _dollar_ends(chain: str) -> list[int]:
    """Offsets of every "-$" in a chain; its "$" end starts two characters later."""
    return [i for i in range(len(chain)) if chain.startswith("-$", i)]


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
        words = [rf"(?<!\w){re.escape(w)}(?!\w)" for w in sorted(self.words, key=len)[::-1]]
        # No string leaves: an empty pattern would match everywhere; match nothing.
        self._word = re.compile("|".join(words) if words else r"(?!)")

    def in_text(self, text: str) -> list[str]:
        hits = [f"word {w}" for w in self._word.findall(text)]
        hits += [f"date {d.isoformat()}" for d in sorted(self.dates) if d.isoformat() in text]
        chains = [m for m in CHAIN_NUMBER.finditer(text) if not ISO_DATE.fullmatch(m.group())]
        # A "$" end a chain reads is read there, once (G22-1): skip it here. Nothing else
        # is skipped, so a "$" end no chain takes is still read here.
        claimed = {m.start() + i + 2 for m in chains for i in _dollar_ends(m.group())}
        for pattern in (PROSE_NUMBER, GROUPED_NUMBER):
            for match in pattern.finditer(text):
                if match.start() in claimed:
                    continue
                number = _number(match.group().replace(",", ""))
                if number is not None and number in self.numbers:
                    hits.append(f"text {_show(number)}")
        for match in chains:
            for end in match.group().split("-"):
                number = _number(end.replace("$", "").replace(",", ""))
                if number is not None and number in self.numbers:
                    hits.append(f"text {_show(number)}")
        for match in SIGNED_NUMBER.finditer(text):
            number = _number(match.group(1).replace(",", ""))
            if number is not None:
                hits += [f"text {_show(n)}" for n in self.signed(-number)]
        return hits

    def signed(self, negative: Decimal) -> list[Decimal]:
        """A negative number counts as itself when forbidden, else as its value when
        that is forbidden (the rule G18-1 applies to a minus over a constant in code)."""
        for number in (negative, -negative):
            if number in self.numbers:
                return [number]
        return []


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
        # G18-1: -750 is UnaryOp(USub, 750). When the negative is itself forbidden it
        # counts as that value; otherwise the constant is scanned as written below.
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and type(node.operand.value) in (int, float)
        ):
            negative = -Decimal(str(node.operand.value)).normalize()
            if negative in forbidden.numbers:
                found[f"code {_show(negative)}"] += 1
                date_parts.add(id(node.operand))
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
            elif spelled is not None and spelled < 0 and -spelled in forbidden.numbers:
                found[f"code {_show(-spelled)}"] += 1  # G19-2: "-900" as 900 does in code
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
# - code 8: uuid4().hex[:8] in ids. code -2: the "-2" suffix of a stop id; and,
#   since G18-1 reads a minus over a constant as one negative value: scaleb(-2) (a
#   percent as a fraction), bars[-2] (the next-to-last bar), IBKR's market data type
#   -2 (frozen or delayed). Config's -2 is Good Friday's offset from Easter.
# - code 100: _PERCENT_MAX, the upper bound of a percentage (was the one in-line marker).
# - words: holiday names, weekday codes and rule names that are the schema keys the
#   config is parsed with (core/config.py, core/holiday_rules.py, core/calendar.py);
#   "FOP" in a comment listing IBKR security types.
# - text N: small counts and item numbers in docstrings, comments and messages; since
#   G20-1 reads both ends of a hyphen range: config.py's message "nth must be 1-5" (the
#   nth-weekday range above) and channels.py's E.164 phone regex "[1-9]".
REVIEWED: dict[str, dict[str, int]] = {
    "footprint/agents/_series.py": {
        "code 2": 1,
        "code 4": 1,
    },
    "footprint/agents/contract_picker.py": {
        "code 2": 1,
    },
    # 365 (integration of #37 with #40/#43): config/research.yaml lists days_per_year: 365
    # (research's own Black-76 time unit), which made every 365 in src/ forbidden. In the
    # trading path 365 is the length of a calendar year: B76-1 (Andre, 2026-10-22) fixes
    # time as CALENDAR time, and `_YEAR = timedelta(days=365)` converts a span to years
    # for Black-76 (theta.py, greeks.py, model_check.py; plus one docstring each in
    # theta.py and greeks.py saying so). A unit conversion, not an instrument, money or
    # strategy value; research keeps its own setting and the trading path never reads it.
    "footprint/agents/greeks.py": {
        "code 365": 1,
        "text 365": 1,
    },
    "footprint/agents/levels.py": {
        "code -2": 2,
        "code 3": 1,
    },
    "footprint/agents/momentum_reader.py": {
        "code -2": 1,
        "code 2": 2,
        "text 2": 1,
    },
    "footprint/agents/range_reader.py": {
        "code -2": 4,
    },
    "footprint/agents/theta.py": {
        "code 365": 1,
        "text 365": 1,
    },
    "footprint/agents/trend_reader.py": {
        "code -2": 4,
        "code 2": 2,
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
    "footprint/backtest/model_check.py": {
        "code 365": 1,
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
        "code -2": 1,
        "word FOP": 1,
    },
    "footprint/brokers/ibkr/market.py": {
        "code 2": 4,
        "code 3": 1,
        "code 4": 2,
        "code 5": 1,
        "code 6": 2,
    },
    # black76.py: the Black-76 formula's own constants (the square root of 2 in the normal
    # distribution, halving s squared, the normal cdf's erfc(...) / 2, the density's
    # exp(-x * x / 2), theta's 2 * sqrt(T)). Mathematics, not a trading value.
    "footprint/core/black76.py": {
        "code 2": 5,
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
        "text 5": 1,
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
        "code -2": 1,
    },
    "footprint/decision/sizing.py": {
        "code -2": 1,
    },
    "footprint/decision/stop.py": {
        "code -2": 1,
        "code 2": 1,
    },
    "footprint/execution/channels.py": {
        "code 2": 1,
        "text 10": 1,
        "text 3": 1,
        "text 7": 1,
        "text 9": 1,
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
        # Process exit code 2 (refused), not a trading value; the 4th is the play
        # caller's refusals (playcaller-wire-v1, PC-2), the 5th a config error at load
        # (playcaller-wire-v1 review round 1, L4).
        "code 2": 5,
    },
    "footprint/live/alarm_setup.py": {
        "code 2": 6,
        "text 4": 1,
    },
    "footprint/live/kill_switch.py": {
        "code 2": 1,  # REFUSED, the command's exit code (P6-1)
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
            "text 25000": 2,  # the comment: "25,000" read whole (G18-2) and "25000"
            "text 0.25": 1,  # the docstring
        }
    )


def test_scanner_sees_negative_only_values_and_grouped_prose_numbers(tmp_path: Path) -> None:
    """G18-1: a value that is in config only as a negative is caught when src writes it
    with a minus (a unary minus over a constant). G18-2: a number written with thousands
    separators in prose ("$1,800") is read whole, not as 1 and 800."""
    config = tmp_path / "c.yaml"
    config.write_text("risk:\n  daily_loss: -750\n  max_cost: 1800\n  weekly: [-3]\n")
    forbidden = Forbidden([config])
    hits = scan_source(
        "X = -750\nY = -1800\nZ = -(3)\n# cap is $1,800; 750 or 1,800,000\n", forbidden
    )
    assert hits == Counter(
        {
            "code -750": 1,  # in config only as -750
            "code 1800": 1,  # -1800: 1800 is forbidden, so the inner constant counts
            "code -3": 1,
            "text 1800": 1,  # "$1,800" whole; "1,800,000" is 1800000, not forbidden
        }
    )


def test_scanner_reads_a_leading_minus_in_prose_and_in_number_strings(tmp_path: Path) -> None:
    """G19-1: a number with a leading minus in a comment or docstring ("-900") is read,
    as its negative when that is forbidden, else as the value itself; a minus glued to
    a digit (a date "2026-04-03", a range "2100-2199") is still not a sign. G19-2: a
    string spelling of a negative ("-900") counts as the value when only the positive
    is in config, as `-900` in code already does (G18-1)."""
    config = tmp_path / "c.yaml"
    config.write_text("risk:\n  daily_loss: -750\n  stop: 900\n  max_cost: 1800\n  day: 4\n")
    forbidden = Forbidden([config])
    hits = scan_source(
        "from decimal import Decimal\n"
        'A = Decimal("-900")\n'
        'B = Decimal("-750")\n'
        "# daily loss -750; stop at -900; loss -1,800; $-900\n"
        "# not signs: 2026-04-03, 2100-2199, x-900, --900\n"
        '"""Ranges 1-4 and 750-900 are not signs either (G20-1 reads their ends)."""\n',
        forbidden,
    )
    assert hits == Counter(
        {
            "code 900": 1,  # Decimal("-900"): 900 is forbidden, -900 is not
            "code -750": 1,
            "text -750": 1,
            "text 900": 3,  # "-900", "$-900" and the range end "750-900" (G20-1)
            "text 4": 1,  # the range end "1-4" (G20-1)
            "text 1800": 1,  # "-1,800" whole
        }
    )


def test_scanner_reads_both_ends_of_a_hyphen_range(tmp_path: Path) -> None:
    """G20-1: both ends of a hyphen range ("900-1000", "$900-$1,000") are read as plain
    values, in prose and inside a string literal; the hyphen is not a sign. Each end is
    read once. An ISO date ("2026-04-03"), a range glued to a word ("G6-5") and a
    clock time stay unread as numbers."""
    config = tmp_path / "c.yaml"
    config.write_text("risk:\n  daily_loss: -750\n  stop: 900\n  max_cost: 1000\n  day: 4\n")
    forbidden = Forbidden([config])
    hits = scan_source(
        'A = "900-1000"\n'
        "# stop 900-1,000; $900-$1,000; 1.5-4 contracts\n"
        "# not ranges: 2026-04-03, G6-5, x900-1000, 10:04-4:00\n",
        forbidden,
    )
    assert hits == Counter(
        {
            "text 900": 3,  # string, prose, "$900-"
            "text 1000": 3,  # string, prose "1,000", "$1,000" (read once)
            "text 4": 1,  # "1.5-4"
        }
    )


def test_scanner_reads_every_end_of_a_hyphen_chain_once(tmp_path: Path) -> None:
    """G21-1: every number of a hyphen chain of three or more ("750-900-1000") is read;
    only the ISO date shape ("2026-04-03") is left to the date check. G21-2: a range end
    written with a leading dot (".25-.5") is read. G21-3: a decimal end ("900.0-1000",
    "1,000.0-1200") is read once, at its written value, never also as the digits before
    its dot. Every "$" end of a chain is read once."""
    config = tmp_path / "c.yaml"
    config.write_text(
        "risk:\n  stop: 900\n  max_cost: 1000\n  big: 1200\n  frac: 0.25\n  half: 0.5\n  day: 3\n"
    )
    forbidden = Forbidden([config])
    hits = scan_source(
        'A = "750-900-1000"\n'
        "# stop 900-1000-1200; $900-$1000-$1,200\n"
        "# frac .25-.5 and .25-0.5 and 0.25-.5\n"
        "# 900.0-1000 and 1,000.0-1200; 900.5-1000\n"
        "# -$900 and 0.5-$1,000; stop 900-$1200\n"
        "# not numbers: 2026-04-03, 2026-04-03T09:00, G6-5-3, x900-1000-1200, 0.25-0.5.1\n",
        forbidden,
    )
    assert hits == Counter(
        {
            "text 900": 6,  # string, prose chain, "$900-", "900.0-", "-$900", "900-$1200"
            "text 1000": 7,  # string, 2 chains, "900.0-", "1,000.0", "900.5-", "$1,000"
            "text 1200": 4,  # chain, "$1,200", "-1200" after 1,000.0, "-$1200" (read once)
            "text 0.25": 3,
            "text 0.5": 4,  # ".5" twice, "0.5" and the "0.5-$1,000" left end
        }
    )


def test_scanner_reads_a_dollar_amount_after_a_hyphen_no_chain_takes(tmp_path: Path) -> None:
    """G22-1: a "$" amount after a hyphen glued to a word or a clock time ("x6-$900",
    "10:30-$900", "G6-$1,000") is read, as it was before G21: only a "$" end that a hyphen
    chain actually reads is left to the chain, and it is read once ("7_-$1,200" was read
    twice). A number a chain swallows without reading it ("2026-04-03,900") is still read."""
    config = tmp_path / "c.yaml"
    config.write_text("risk:\n  stop: 900\n  max_cost: 1000\n  big: 1200\n")
    forbidden = Forbidden([config])
    hits = scan_source(
        "# x6-$900, 10:30-$900 and T1-$1200; dated 2026-04-03,900\n"
        "# G6-$1,000; 9:30-$1,200\n"
        "# chains read once: 900-$1000, 6-$1,200, 7_-$1,200\n",
        forbidden,
    )
    assert hits == Counter(
        {
            "text 900": 4,  # "x6-$900", "10:30-$900", "2026-04-03,900", the chain's "900"
            "text 1000": 2,  # "G6-$1,000", the chain's "$1000"
            "text 1200": 4,  # "T1-$1200", "9:30-$1,200", the chains' "$1,200" (once each)
        }
    )
