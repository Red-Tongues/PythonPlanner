"""Immutability triggers (Requirement F, A1) and lineage-head views (A2).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from orchestrator.db import ddl

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    ddl.install(op.get_bind())


def downgrade() -> None:
    # Dropping the tables in 0001's downgrade removes the triggers; views go first.
    for view in ddl.HEAD_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
