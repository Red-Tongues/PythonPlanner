"""Exclusivity and pauses (M3 C26, C33): the single-kernel lock and governance-scope pauses.

**Kernel lock.** One ``kernel_leases`` row per lock name, taken by a *conditional UPDATE*
(portable to SQLite and PostgreSQL): free or expired → this holder, ``epoch + 1``. Every guarded
kernel write calls ``KernelLock.fence`` inside its own transaction; that is again a conditional
UPDATE on the lease row (holder, epoch, not expired), so on PostgreSQL it holds the row lock until
commit and a takeover waits for it — and an owner that was taken over matches no row and is
*fenced out* (``FencedOut``) before it writes anything. On SQLite the single writer lock
serialises the same statements. Heartbeats are liveness, not state changes, and write no event;
acquisition, takeover and release do.

**Pauses.** ``governor_states`` holds one row per scope (global ``*``, a channel, a project /
objective / branch / packet ref). ``pause`` and ``resume`` are evented and bump the row's
``revision``. Dispatch authorisation (``orchestrator.runs.authorise_dispatch``) touches every
applicable scope row with ``UPDATE … SET revision = revision + 1 WHERE status = 'normal'`` in
the dispatch transaction: whichever of pause and dispatch commits first wins, and the other
sees it (C26, C38). A pause clears only on an explicit owner ``resume`` (C33).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import ScopeStatus, ScopeType
from orchestrator.db.models import GovernorState, KernelLease, new_id, utcnow
from orchestrator.db.upsert import insert_if_absent
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event

GLOBAL = (ScopeType.GLOBAL.value, "*")


class FencedOut(OrchestratorError):
    """This kernel no longer holds the lock (expired or taken over): it must not write."""


class LockHeld(OrchestratorError):
    """Another live kernel holds the lock."""


class DispatchRefused(OrchestratorError):
    """A scope that governs this dispatch is paused (or another gate refused it)."""


# --------------------------------------------------------------------------- kernel lock


@dataclass
class KernelLock:
    holder: str
    name: str = "kernel"
    ttl: timedelta = field(default_factory=lambda: timedelta(minutes=2))
    epoch: int | None = None

    def _ensure_row(self, session: Session) -> None:
        insert_if_absent(
            session,
            KernelLease.__table__,
            {"name": self.name, "holder": None, "epoch": 0, "updated_at": utcnow()},
            ["name"],
        )

    def acquire(self, session: Session, *, now: datetime | None = None) -> int:
        """Take the lock if it is free or its lease expired (a takeover). Returns the epoch."""
        now = now or utcnow()
        self._ensure_row(session)
        table = KernelLease.__table__
        before = session.execute(
            select(table.c.holder, table.c.epoch, table.c.lease_expires_at).where(
                table.c.name == self.name
            )
        ).one()
        result = session.execute(
            update(table)
            .where(
                table.c.name == self.name,
                table.c.holder.is_(None) | (table.c.lease_expires_at < now),
            )
            .values(
                holder=self.holder,
                epoch=table.c.epoch + 1,
                acquired_at=now,
                heartbeat_at=now,
                lease_expires_at=now + self.ttl,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise LockHeld(f"kernel lock {self.name!r} is held by {before.holder}")
        self.epoch = int(
            session.execute(select(table.c.epoch).where(table.c.name == self.name)).scalar_one()
        )
        takeover = before.holder is not None
        record_event(
            session,
            actor=Actor.system(self.holder),
            action="kernel_lock.takeover" if takeover else "kernel_lock.acquired",
            entity_type="kernel_lease",
            entity_id=self.name,
            before={"holder": before.holder, "epoch": before.epoch},
            after={"holder": self.holder, "epoch": self.epoch},
            reason="previous lease expired" if takeover else None,
        )
        session.flush()
        return self.epoch

    def fence(self, session: Session, *, now: datetime | None = None) -> None:
        """Prove, inside the caller's transaction, that this kernel still holds the lock, and
        extend the lease (a heartbeat). Raises ``FencedOut`` otherwise."""
        now = now or utcnow()
        if self.epoch is None:
            raise FencedOut("the kernel lock was never acquired")
        table = KernelLease.__table__
        result = session.execute(
            update(table)
            .where(
                table.c.name == self.name,
                table.c.holder == self.holder,
                table.c.epoch == self.epoch,
                table.c.lease_expires_at >= now,
            )
            .values(heartbeat_at=now, lease_expires_at=now + self.ttl, updated_at=now)
        )
        if result.rowcount != 1:
            raise FencedOut(f"{self.holder} (epoch {self.epoch}) no longer holds {self.name!r}")

    heartbeat = fence

    def release(self, session: Session, *, now: datetime | None = None) -> None:
        self.fence(session, now=now)
        table = KernelLease.__table__
        session.execute(
            update(table)
            .where(table.c.name == self.name, table.c.epoch == self.epoch)
            .values(holder=None, lease_expires_at=None, updated_at=now or utcnow())
        )
        record_event(
            session,
            actor=Actor.system(self.holder),
            action="kernel_lock.released",
            entity_type="kernel_lease",
            entity_id=self.name,
            before={"holder": self.holder, "epoch": self.epoch},
        )
        session.flush()
        self.epoch = None


# --------------------------------------------------------------------------- scopes and pauses


def _ensure_scope(session: Session, scope_type: str, scope_id: str) -> None:
    now = utcnow()
    insert_if_absent(
        session,
        GovernorState.__table__,
        {
            "id": new_id(),
            "scope_type": scope_type,
            "scope_id": scope_id,
            "status": ScopeStatus.NORMAL.value,
            "revision": 0,
            "created_at": now,
            "updated_at": now,
        },
        ["scope_type", "scope_id"],
    )


def scope_status(session: Session, scope_type: str, scope_id: str) -> ScopeStatus:
    row = session.scalar(
        select(GovernorState.status).where(
            GovernorState.scope_type == scope_type, GovernorState.scope_id == scope_id
        )
    )
    return ScopeStatus(row) if row is not None else ScopeStatus.NORMAL


def _set_status(
    session: Session,
    scope_type: str,
    scope_id: str,
    status: ScopeStatus,
    *,
    actor: Actor,
    reason: str,
) -> bool:
    ScopeType(scope_type)  # validate
    _ensure_scope(session, scope_type, scope_id)
    table = GovernorState.__table__
    now = utcnow()
    values: dict = {
        "status": status.value,
        "reason": reason,
        "revision": table.c.revision + 1,
        "changed_by": str(actor),
        "updated_at": now,
    }
    if actor.is_user:
        values["last_human_touch_at"] = now  # C33: only owner decisions count as a touch
    result = session.execute(
        update(table)
        .where(
            table.c.scope_type == scope_type,
            table.c.scope_id == scope_id,
            table.c.status != status.value,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        return False  # already in that status: nothing changed, nothing to record
    record_event(
        session,
        actor=actor,
        action=f"scope.{'paused' if status is ScopeStatus.PAUSED else 'resumed'}",
        entity_type="governor_state",
        entity_id=None,
        before={"scope": [scope_type, scope_id]},
        after={"scope": [scope_type, scope_id], "status": status.value},
        reason=reason,
    )
    session.flush()
    return True


def pause(session: Session, scope_type: str, scope_id: str, *, actor: Actor, reason: str) -> bool:
    """Pause a scope (owner or governor). In-flight work drains; nothing new is dispatched."""
    if not reason.strip():
        raise ValueError("a pause needs a reason")
    return _set_status(
        session, scope_type, scope_id, ScopeStatus.PAUSED, actor=actor, reason=reason
    )


def resume(session: Session, scope_type: str, scope_id: str, *, actor: Actor, reason: str) -> bool:
    """Clear a pause. Only an owner resumes (C33: a pause never clears automatically)."""
    if not actor.is_user:
        raise PermissionError("only a user may resume a paused scope")
    return _set_status(
        session, scope_type, scope_id, ScopeStatus.NORMAL, actor=actor, reason=reason
    )


def authorise_scopes(session: Session, scopes: list[tuple[str, str]]) -> None:
    """Serialise a dispatch with pauses (C26): touch every scope row, in a fixed order, only
    while it is ``normal``. Raises ``DispatchRefused`` naming the first paused scope."""
    table = GovernorState.__table__
    for scope_type, scope_id in sorted(set(scopes)):  # fixed order: no lock-order deadlocks
        _ensure_scope(session, scope_type, scope_id)
        result = session.execute(
            update(table)
            .where(
                table.c.scope_type == scope_type,
                table.c.scope_id == scope_id,
                table.c.status == ScopeStatus.NORMAL.value,
            )
            .values(revision=table.c.revision + 1)
        )
        if result.rowcount != 1:
            raise DispatchRefused(f"scope {scope_type}:{scope_id} is paused")
