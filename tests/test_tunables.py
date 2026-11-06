"""M3a.1 tunability (CFO rule: every threshold and every piece of maths is tunable).

1. ``orchestrator.tunables`` is the only table of defaults: the seed equals it, and every
   settings reader takes only a key (no literal fallback anywhere in ``src/``).
2. No decision literal appears in the decision modules outside that table: every number other
   than 0/1 — in Python code or inside generated SQL text — must sit on a line marked
   ``structural`` (a unit, a scale or a pass number, never a threshold).
3. Every key a reader names exists in the table; ``set_setting`` enforces the ranges.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from decimal import Decimal
from pathlib import Path

import pytest

from orchestrator.db.seed import SETTINGS
from orchestrator.tunables import BY_KEY, TUNABLES, validate

SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
#: modules that make decisions (thresholds, weights, windows, scores)
DECISION_MODULES = (
    "sql/genqueue.py",
    "sql/views.py",
    "evaluation/gen_eva.py",
    "evaluation/readiness.py",
    "evaluation/consensus.py",
    "evaluation/context.py",
    "queue.py",
    "runs.py",
    "branching.py",
    "gates.py",
    "budgets.py",
)
READERS = {"get_int": 2, "get_setting": 2, "get_decimal": 2, "_int": 1, "_setting": 1}
#: string tokens, including the literal text of f-strings (Python ≥ 3.12)
STRINGS = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
SQL_HINT = re.compile(r"\b(SELECT|CASE WHEN|WHERE|THEN)\b")
NUMBER_IN_TEXT = re.compile(r"(?<![\w.'])(\d{2,}|[2-9])(?![\w'])")


def _offending_lines(path: Path) -> list[str]:
    text = path.read_text()
    lines = text.splitlines()
    found: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.NUMBER and tok.string not in ("0", "1", "0.0"):
            line = lines[tok.start[0] - 1]
            if "structural" not in line:
                found.append(f"{path.name}:{tok.start[0]}: {line.strip()}")
        elif tok.type in STRINGS and (path.parent.name == "sql" or SQL_HINT.search(tok.string)):
            for offset, piece in enumerate(tok.string.splitlines()):
                row = tok.start[0] + offset
                source_line = lines[row - 1]
                if NUMBER_IN_TEXT.search(_strip_keys(piece)) and "structural" not in source_line:
                    found.append(f"{path.name}:{row}: {source_line.strip()}")
    return found


def _strip_keys(text: str) -> str:
    """Setting keys, refs, identifiers and comments are not decision literals."""
    text = re.sub(r"'[a-z_.]+'|\"[a-z_.0-9]+\"", "", text)
    text = re.sub(r"[A-Za-z_][A-Za-z_]*\d+[A-Za-z_\d]*", "", text)  # pass3, i4, e4, done1 …
    return text


def _docstrings(tree: ast.AST) -> set[int]:
    rows: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef):
            doc = node.body[0] if node.body else None
            constant = isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
            if constant and isinstance(doc.value.value, str):  # type: ignore[union-attr]
                rows |= set(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))  # type: ignore[union-attr]
    return rows


def test_the_seed_is_the_tunables_table() -> None:
    assert [(k, v) for k, v, _ in SETTINGS] == [(t.key, t.seed_value) for t in TUNABLES]
    for t in TUNABLES:
        validate(t.key, t.default)  # every default is within its own range


def test_no_settings_reader_carries_a_literal_default_and_every_key_exists() -> None:
    problems: list[str] = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in READERS or path.name in ("settings_store.py", "tunables.py"):
                continue
            if name in ("_int", "_setting") and path.parent.name != "sql":
                continue
            where = f"{path.relative_to(SRC)}:{node.lineno}"
            if len(node.args) + len(node.keywords) != READERS[name]:
                problems.append(f"{where}: {name} with a literal default")
            key = node.args[READERS[name] - 1] if len(node.args) >= READERS[name] else None
            if isinstance(key, ast.Constant) and key.value not in BY_KEY:
                problems.append(f"{where}: unknown tunable {key.value!r}")
    assert problems == []


def test_no_decision_literal_outside_the_table() -> None:
    offending: list[str] = []
    for module in DECISION_MODULES:
        path = SRC / module
        docs = _docstrings(ast.parse(path.read_text()))
        offending += [
            line for line in _offending_lines(path) if int(line.split(":")[1]) not in docs
        ]
    assert offending == [], "move these into orchestrator.tunables:\n" + "\n".join(offending)


@pytest.mark.parametrize(
    ("key", "value", "ok"),
    [
        ("admission.floor_e4", 10001, False),
        ("admission.floor_e4", 4000, True),
        ("consensus.agreement_min", 1.5, False),
        ("consensus.agreement_min", 0.65, True),
        ("lockout.max_position", 2.5, False),
        ("feedback.demote_e4", -2000, True),
    ],
)
def test_ranges_are_validated(key: str, value: object, ok: bool) -> None:
    if ok:
        validate(key, value)
    else:
        with pytest.raises(ValueError):
            validate(key, value)
    assert isinstance(BY_KEY["consensus.agreement_min"].default, Decimal)
