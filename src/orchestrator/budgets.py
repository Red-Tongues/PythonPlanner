"""Atomic capacity reservation (M3 C36) — the primitive the dispatch guard (E3) uses.

A counter row per scope × metric × window holds the used capacity. ``reserve`` adds to it with a
conditional UPDATE (``used + amount <= limit``) and records an append-only reservation keyed by
the operation ID, in the caller's transaction. Two writers racing for the last unit cannot both
win: on PostgreSQL the second UPDATE waits for the row lock and then re-checks the condition; on
SQLite the single writer lock serialises them. A refused reservation raises before any effect.
``reconcile`` records actual usage exactly once per reservation and releases what was not used.
Limits, windows and top-ups are wired to settings in Stage B (C33/C36 governor).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.db.models import (
    BudgetCounter,
    BudgetReconciliation,
    BudgetReservation,
    new_id,
    utcnow,
)
from orchestrator.db.upsert import insert_if_absent
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event


class BudgetExhausted(OrchestratorError):
    """The reservation would exceed the limit; nothing was reserved."""


def _require_nonnegative_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class Demand:
    scope_type: str
    scope_id: str
    metric: str  # runs | tokens | packets
    period: str  # hour | day
    period_start: datetime
    amount: int
    limit: int


def _counter(session: Session, d: Demand) -> str:
    t = BudgetCounter.__table__
    insert_if_absent(
        session,
        t,
        {
            "id": new_id(),
            "scope_type": d.scope_type,
            "scope_id": d.scope_id,
            "metric": d.metric,
            "period": d.period,
            "period_start": d.period_start,
            "used": 0,
            "updated_at": utcnow(),
        },
        ["scope_type", "scope_id", "metric", "period", "period_start"],
    )
    return session.execute(
        select(t.c.id).where(
            t.c.scope_type == d.scope_type,
            t.c.scope_id == d.scope_id,
            t.c.metric == d.metric,
            t.c.period == d.period,
            t.c.period_start == d.period_start,
        )
    ).scalar_one()


def reserve(
    session: Session,
    operation_id: str,
    demands: list[Demand],
    *,
    actor: Actor,
    packet_id: str | None = None,
    generation_run_id: str | None = None,
) -> list[str]:
    """Reserve every demand or none (the caller's transaction rolls back on ``BudgetExhausted``).
    Returns the reservation ids."""
    t = BudgetCounter.__table__
    ids: list[str] = []
    ordered = sorted(demands, key=lambda x: (x.scope_type, x.scope_id, x.metric, x.period))
    for demand in ordered:
        _require_nonnegative_integer(demand.amount, "budget demand amount")
        _require_nonnegative_integer(demand.limit, "budget demand limit")
    reservations: list[BudgetReservation] = []
    # GA-R5 / PM (c): the counter UPDATEs run first under no_autoflush (nothing is flushed
    # mid-loop, so the flush guard never sees a reservation without its event); the event and
    # the reservation rows are added together only once every demand has succeeded, so a
    # refused reservation never leaves a "budget.reserved" event behind, even for a caller
    # that does not roll back. (The caller must still roll back: earlier counter UPDATEs in a
    # refused multi-demand reservation are only undone by the rollback.)
    with session.no_autoflush:
        for d in ordered:
            counter_id = _counter(session, d)
            result = session.execute(
                update(t)
                .where(t.c.id == counter_id, t.c.used + d.amount <= d.limit)
                .values(used=t.c.used + d.amount, updated_at=utcnow())
            )
            if result.rowcount != 1:
                raise BudgetExhausted(
                    f"{d.scope_type}:{d.scope_id} {d.metric}/{d.period} has no capacity for "
                    f"{d.amount} (limit {d.limit})"
                )
            reservations.append(
                BudgetReservation(
                    id=new_id(),
                    operation_id=operation_id,
                    counter_id=counter_id,
                    amount=d.amount,
                    packet_id=packet_id,
                    generation_run_id=generation_run_id,
                )
            )
        record_event(
            session,
            actor=actor,
            action="budget.reserved",
            entity_type="budget_reservation",
            entity_id=None,
            after={
                "operation_id": operation_id,
                "demands": [
                    [d.scope_type, d.scope_id, d.metric, d.period, d.amount] for d in ordered
                ],
            },
        )
        session.add_all(reservations)
        ids.extend(r.id for r in reservations)
    session.flush()
    return ids


def reconcile(session: Session, reservation_id: str, actual: int, *, actor: Actor) -> bool:
    """Record actual usage once (C36). Releases ``amount - actual`` when less was used. Returns
    False when this reservation was already reconciled (a duplicate report changes nothing)."""
    _require_nonnegative_integer(actual, "actual budget usage")
    if session.scalar(
        select(BudgetReconciliation.id).where(BudgetReconciliation.reservation_id == reservation_id)
    ):
        return False
    reservation = session.get(BudgetReservation, reservation_id)
    if reservation is None:
        raise OrchestratorError(f"no reservation {reservation_id}")
    session.add(BudgetReconciliation(id=new_id(), reservation_id=reservation_id, actual=actual))
    record_event(  # before any statement: the flush guard needs the event in the same flush
        session,
        actor=actor,
        action="budget.reconciled",
        entity_type="budget_reconciliation",
        entity_id=None,
        after={"reservation": reservation_id, "reserved": reservation.amount, "actual": actual},
    )
    unused = reservation.amount - actual
    if unused != 0:
        t = BudgetCounter.__table__
        session.execute(
            update(t)
            .where(t.c.id == reservation.counter_id)
            .values(used=t.c.used - unused, updated_at=utcnow())
        )
    session.flush()
    return True
