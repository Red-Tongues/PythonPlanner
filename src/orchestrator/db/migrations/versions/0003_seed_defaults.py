"""Seed defaults: autonomy scopes (§11), settings, threshold rules (§8), control state.

Inserts only missing rows, so it never overwrites values the user has edited. The
candidate-field registry is synced from YAML by ``init-db`` after migrations (the YAML is the
source of truth for field definitions).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session

from orchestrator.db import seed

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    seed.seed_autonomy_scopes(session)
    seed.seed_settings(session)
    seed.seed_thresholds(session)
    seed.seed_control_state(session)
    session.flush()
    session.close()


def downgrade() -> None:
    # Seed rows are removed with their tables (0001); events are append-only by design.
    pass
