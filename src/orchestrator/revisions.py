"""Revision history: drain the trigger outbox into ``content_blobs`` and ``field_revisions``.

T1-C v3 §1 / R6 OQ35. The AFTER UPDATE triggers (``db/ddl.py``) copy every change of an audited
column of ``packets``, ``projects`` and ``objectives`` into the transient ``raw_revision_outbox``,
whoever made it — the kernel through the ORM or the CFO with raw SQL. ``drain_outbox`` turns
those rows, **synchronously and in the caller's transaction**, into:

* one insert-once ``content_blobs`` row per distinct value (sha256-addressed);
* one append-only, hash-only ``field_revisions`` row per changed field, with the hash it was
  based on (Q6), the producer, the content revision and the entity's manifest hash;
* one ``<entity>.revised`` event per revision group (a statement's changes to one row);

then deletes the outbox rows. It runs after every governed edit, at every admission tick and
before any claim or seal; a packet with undrained rows is neither claimable nor sealable (AC8).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.db.ddl import AUDITED_COLUMNS, ENTITY_OF
from orchestrator.db.models import (
    ContentBlob,
    FieldRevision,
    Objective,
    Packet,
    Project,
    RawRevisionOutbox,
    new_id,
)
from orchestrator.events import record_event

log = logging.getLogger(__name__)

KERNEL = Actor.system("kernel")
MODELS: dict[str, type] = {"packet": Packet, "project": Project, "objective": Objective}
TABLE_OF: dict[str, str] = {v: k for k, v in ENTITY_OF.items()}


def blob_hash(value: str | None) -> str:
    """The content address of a captured value (NULL and '' differ)."""
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def store_blob(session: Session, value: str | None) -> str:
    """Insert ``value`` once; return its hash."""
    digest = blob_hash(value)
    if session.get(ContentBlob, digest) is None:
        session.add(ContentBlob(hash=digest, body=value))
        session.flush()
    return digest


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def manifest_of(row: Any, entity_type: str) -> dict[str, Any]:
    """Every audited column of ``row`` as text (what the outbox captures)."""
    return {
        c: None if getattr(row, c) is None else _text(getattr(row, c))
        for c in AUDITED_COLUMNS[TABLE_OF[entity_type]]
    }


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return repr(value)
    return str(value)


def manifest_hash(row: Any, entity_type: str) -> str:
    return hashlib.sha256(canonical_json(manifest_of(row, entity_type)).encode()).hexdigest()


def has_undrained(session: Session, entity_id: str) -> bool:
    session.flush()
    return (
        session.scalar(
            select(func.count())
            .select_from(RawRevisionOutbox)
            .where(RawRevisionOutbox.entity_id == entity_id)
        )
        or 0
    ) > 0


def _producer_actor(producer: str) -> Actor:
    if producer == "user_sql":
        return Actor.user("sql")
    kind, _, ident = producer.partition(":")
    try:
        return {"user": Actor.user, "system": Actor.system, "llm": Actor.llm}[kind](ident)
    except (KeyError, ValueError):
        return Actor.system("kernel")


def drain_outbox(
    session: Session, *, entity_id: str | None = None, actor: Actor = KERNEL
) -> list[FieldRevision]:
    """Enrich every pending outbox row (or one entity's) into history; returns the revisions.

    Idempotent and safe to call at any time; each row is deleted in the same transaction as the
    revision it produced, so a crash never loses or duplicates a revision."""
    session.flush()
    stmt = select(RawRevisionOutbox).order_by(RawRevisionOutbox.id)
    if entity_id is not None:
        stmt = stmt.where(RawRevisionOutbox.entity_id == entity_id)
    rows = list(session.scalars(stmt))
    if not rows:
        return []
    groups: dict[tuple[str, str, str, int], list[RawRevisionOutbox]] = {}
    for row in rows:
        groups.setdefault(
            (row.entity_type, row.entity_id, row.tx_id, row.content_revision), []
        ).append(row)
    revisions: list[FieldRevision] = []
    edited: set[tuple[str, str]] = set()
    for (entity_type, ent_id, tx_id, revision), members in groups.items():
        entity = session.get(MODELS[entity_type], ent_id)
        if entity is not None:
            session.refresh(entity)
        manifest = manifest_hash(entity, entity_type) if entity is not None else "missing"
        group_id = f"{tx_id}:{revision}"
        changes: dict[str, dict[str, str]] = {}
        for row in members:
            old_hash = store_blob(session, row.old_value)
            new_hash = store_blob(session, row.new_value)
            previous = session.scalar(
                select(FieldRevision.new_hash)
                .where(
                    FieldRevision.entity_id == ent_id,
                    FieldRevision.field_name == row.field_name,
                )
                .order_by(FieldRevision.content_revision.desc(), FieldRevision.created_at.desc())
                .limit(1)
            )
            revision_row = FieldRevision(
                id=new_id(),
                entity_type=entity_type,
                entity_id=ent_id,
                field_name=row.field_name,
                old_hash=old_hash,
                new_hash=new_hash,
                based_on_hash=previous or old_hash,
                producer=row.producer,
                content_revision=row.content_revision,
                revision_group_id=group_id,
                manifest_hash=manifest,
            )
            session.add(revision_row)
            revisions.append(revision_row)
            changes[row.field_name] = {"old": old_hash, "new": new_hash}
        record_event(
            session,
            actor=_producer_actor(members[0].producer),
            action=f"{entity_type}.revised",
            entity_type=entity_type,
            entity_id=ent_id,
            entity_ref=getattr(entity, "ref", None),
            entity_version=getattr(entity, "version", None),
            before={"content_revision": revision - 1},
            after={
                "content_revision": revision,
                "fields": changes,
                "producer": members[0].producer,
                "revision_group_id": group_id,
                "manifest_hash": manifest,
            },
            reason="audited field change captured by the outbox trigger (T1-C v3 H1)",
        )
        session.flush()
        session.connection().execute(
            delete(RawRevisionOutbox.__table__).where(
                RawRevisionOutbox.__table__.c.id.in_([m.id for m in members])
            )
        )
        edited.add((entity_type, ent_id))
    for entity_type, ent_id in sorted(edited):
        after_drain(session, entity_type, ent_id, actor=actor)
    log.debug("drained %d outbox rows into %d revisions", len(rows), len(revisions))
    return revisions


def after_drain(session: Session, entity_type: str, entity_id: str, *, actor: Actor) -> None:
    """Enrichment of an edited entity: a packet's intra-packet ``input_hash`` is refreshed (its
    own evaluations stop being current); a project's or objective's quote-backed context links
    are re-verified (R6 OQ34)."""
    from orchestrator.evaluation.context import reverify_links

    if entity_type == "packet":
        from orchestrator.evaluation.evaluator import refresh_input_hash

        packet = session.get(Packet, entity_id)
        if packet is not None and not packet.locked and packet.input_hash is not None:
            refresh_input_hash(session, packet, actor)
        return
    reverify_links(session, entity_type, entity_id, actor=actor)


def value_at(
    session: Session, entity_id: str, field_name: str, revision: int, current: str | None
) -> str | None:
    """The text of ``field_name`` as it stood at ``content_revision = revision`` (R6 OQ20):
    walk back from the current value over the field's revisions newer than ``revision``."""
    newer = session.scalars(
        select(FieldRevision)
        .where(
            FieldRevision.entity_id == entity_id,
            FieldRevision.field_name == field_name,
            FieldRevision.content_revision > revision,
        )
        .order_by(FieldRevision.content_revision.asc(), FieldRevision.created_at.asc())
    ).all()
    if not newer:
        return current
    blob = session.get(ContentBlob, newer[0].old_hash)
    return blob.body if blob is not None else current


def snapshot_at(session: Session, packet: Packet, revision: int) -> dict[str, str | None]:
    """The packet's audited columns as they stood at ``revision`` (the seal's source)."""
    drain_outbox(session, entity_id=packet.id)
    current = manifest_of(packet, "packet")
    return {f: value_at(session, packet.id, f, revision, v) for f, v in current.items()}
