"""Write the current view / trigger DDL into a NEW migration as frozen literals.

Usage (authoring time only, for a new revision): python scripts/freeze_views.py <revision.py>

Placeholders the file may contain (each replaced if present):
``VIEWS: dict[str, list[str]] = {}  # @@FROZEN_VIEWS@@``    ← orchestrator.sql.views
``TRIGGERS: dict[str, list[str]] = {}  # @@FROZEN_TRIGGERS@@`` ← orchestrator.db.ddl.m21_statements
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from orchestrator.db.ddl import m21_statements
from orchestrator.sql.views import view_statements

PLACEHOLDERS: dict[str, tuple[str, Callable[[str], list[str]]]] = {
    "VIEWS: dict[str, list[str]] = {}  # @@FROZEN_VIEWS@@": ("VIEWS", view_statements),
    "TRIGGERS: dict[str, list[str]] = {}  # @@FROZEN_TRIGGERS@@": ("TRIGGERS", m21_statements),
}


def render(name: str, source: Callable[[str], list[str]]) -> str:
    lines = [f"{name}: dict[str, list[str]] = {{"]
    for dialect in ("sqlite", "postgresql"):
        lines.append(f'    "{dialect}": [')
        for stmt in source(dialect):
            assert '"""' not in stmt and "\\" not in stmt
            lines.append(f'        """{stmt}""",')
        lines.append("    ],")
    lines.append("}")
    return "\n".join(lines)


def main(path: str) -> None:
    target = Path(path)
    text = target.read_text()
    found = [p for p in PLACEHOLDERS if p in text]
    if not found:
        sys.exit(f"{path}: no placeholder found")
    for placeholder in found:
        text = text.replace(placeholder, render(*PLACEHOLDERS[placeholder]))
    target.write_text(text)


if __name__ == "__main__":
    main(sys.argv[1])
