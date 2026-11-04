"""Task branching (M2 §5, B5) — filled in below."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.db.models import Packet


def children_of(session: Session, trunk: Packet) -> list[Packet]:
    """Head versions of the packets split from any version of ``trunk``'s lineage."""
    from orchestrator.repository import _heads

    lineage = select(Packet.id).where(Packet.ref == trunk.ref)
    stmt = _heads(Packet).where(Packet.parent_id.in_(lineage), Packet.ref != trunk.ref)
    return list(session.scalars(stmt.order_by(Packet.position, Packet.ref)))
