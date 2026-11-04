"""Candidate-field registry sync and registry revisions.

Default settings, scopes, thresholds and control state are seeded by the frozen migration
``0003`` (R1 fix 4). The field registry is synced from YAML by ``init-db`` (the YAML is the source
of truth for definitions, per the M1 spec): changed fields are updated with a version bump,
removed ones disabled, and every change is evented. Each distinct registry content is also
snapshotted as an append-only ``field_registry_revisions`` row (R1 fix 12), which M2 evaluation
cycles reference.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import CandidateFieldDefinition
from orchestrator.db.models import CandidateFieldDefinitionRow, FieldRegistryRevision, new_id
from orchestrator.events import record_event

SYSTEM_ACTOR = Actor.system("init-db")
KEY_FIELDS = "@key_fields"  # threshold pseudo-field: "any candidate field with is_key_field"

_FIELD_COLUMNS = (
    "label",
    "level",
    "kind",
    "vocabulary",
    "scale",
    "max_chars",
    "max_passes",
    "prompt_hint",
    "is_key_field",
)


def _row_snapshot(row: CandidateFieldDefinitionRow) -> dict[str, Any]:
    snap = {c: getattr(row, c) for c in _FIELD_COLUMNS}
    snap["enabled"] = row.enabled
    return {k: (v.value if hasattr(v, "value") else v) for k, v in snap.items()}


def sync_candidate_fields(
    session: Session, definitions: list[CandidateFieldDefinition], actor: Actor = SYSTEM_ACTOR
) -> dict[str, list[str]]:
    """Upsert the registry into the DB; disable rows no longer in the YAML. Evented."""
    rows = {r.name: r for r in session.scalars(select(CandidateFieldDefinitionRow))}
    result: dict[str, list[str]] = {"inserted": [], "updated": [], "disabled": []}
    changes: dict[str, dict[str, Any]] = {}
    contract = CandidateFieldDefinition.contract_id()

    for definition in definitions:
        values = definition.model_dump(include=set(_FIELD_COLUMNS), mode="json")
        row = rows.get(definition.name)
        if row is None:
            session.add(
                CandidateFieldDefinitionRow(
                    name=definition.name, enabled=True, version=1, contract=contract, **values
                )
            )
            result["inserted"].append(definition.name)
            continue
        before = _row_snapshot(row)
        wanted = {**values, "enabled": True}
        if before != wanted:
            for column, value in wanted.items():
                setattr(row, column, value)
            row.version += 1
            row.contract = contract
            result["updated"].append(definition.name)
            changes[definition.name] = {"before": before, "after": wanted}

    wanted_names = {d.name for d in definitions}
    for name, row in rows.items():
        if name not in wanted_names and row.enabled:
            row.enabled = False
            row.version += 1
            result["disabled"].append(name)

    if any(result.values()):
        record_event(  # a batch event covering every row changed above (R1 fix 7)
            session,
            actor=actor,
            action="fields.synced",
            entity_type="candidate_field_definition",
            before={"changes": {n: c["before"] for n, c in changes.items()}} if changes else None,
            after={**result, "changes": {n: c["after"] for n, c in changes.items()}},
            reason="candidate-field registry synced from YAML",
        )
    session.flush()
    return result


def registry_fingerprint(definitions: list[CandidateFieldDefinition]) -> tuple[str, list[dict]]:
    """SHA-256 of the canonical JSON of the definitions (order-independent)."""
    snapshot = sorted((d.model_dump(mode="json") for d in definitions), key=lambda d: d["name"])
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest(), snapshot


def current_registry_revision(session: Session) -> FieldRegistryRevision | None:
    stmt = select(FieldRegistryRevision).order_by(FieldRegistryRevision.revision.desc()).limit(1)
    return session.scalar(stmt)


def record_registry_revision(
    session: Session, definitions: list[CandidateFieldDefinition], actor: Actor = SYSTEM_ACTOR
) -> FieldRegistryRevision:
    """Snapshot the registry if its content differs from the latest revision. Evented."""
    sha256, snapshot = registry_fingerprint(definitions)
    latest = current_registry_revision(session)
    if latest is not None and latest.sha256 == sha256:
        return latest
    row = FieldRegistryRevision(
        id=new_id(),
        revision=(latest.revision + 1) if latest else 1,
        sha256=sha256,
        field_count=len(snapshot),
        definitions=snapshot,
    )
    session.add(row)
    record_event(
        session,
        actor=actor,
        action="fields.registry_revision",
        entity_type="field_registry_revision",
        entity_id=row.id,
        entity_version=row.revision,
        before={"revision": latest.revision, "sha256": latest.sha256} if latest else None,
        after={"revision": row.revision, "sha256": sha256, "field_count": row.field_count},
        reason="candidate-field registry content changed",
    )
    session.flush()
    return row
