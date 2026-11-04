"""Write the current orchestrator.sql.views output into a migration as frozen literals.

Usage (authoring time only, for a NEW revision): python scripts/freeze_views.py <revision.py>
The file must contain the placeholder line ``VIEWS: dict[str, list[str]] = {}  # @@FROZEN_VIEWS@@``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from orchestrator.sql.views import view_statements

PLACEHOLDER = "VIEWS: dict[str, list[str]] = {}  # @@FROZEN_VIEWS@@"


def render() -> str:
    lines = ["VIEWS: dict[str, list[str]] = {"]
    for dialect in ("sqlite", "postgresql"):
        lines.append(f'    "{dialect}": [')
        for stmt in view_statements(dialect):
            assert '"""' not in stmt and "\\" not in stmt
            lines.append(f'        """{stmt}""",')
        lines.append("    ],")
    lines.append("}")
    return "\n".join(lines)


def main(path: str) -> None:
    target = Path(path)
    text = target.read_text()
    if PLACEHOLDER not in text:
        sys.exit(f"{path}: placeholder not found")
    target.write_text(text.replace(PLACEHOLDER, render()))


if __name__ == "__main__":
    main(sys.argv[1])
