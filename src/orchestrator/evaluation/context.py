"""Context links and ``question_context`` (M3a part b; T1-C v3 §2; R6 OQ4, OQ5, OQ22-OQ24, OQ34).

Upstream changes never invalidate an evaluation. Instead, every evaPacket records — in Python,
deterministically, with no LLM call — the context it read (``eva_context_links``):

* the project and the objective, ``direct`` when a **verified** SUPPORT/CONTRADICTION quote of
  passes 3-6 lies in that section of the source document (``direct_quote``; a quote spanning
  several sections marks each one), otherwise ``peripheral`` (``ambient``) — R6 OQ22;
* every hard dependency, ``direct`` (``hard_dep``);
* the field definition version and the preset revision it was evaluated with, ``direct``
  (``definition``): a version bump questions the result instead of invalidating it (OQ23).

A link is *questioned* when its source's revision moved past the one it read, or when the
outbox drain finds that its quote no longer verifies against the edited source (OQ34). The SQL
view ``v_context_link_status`` computes this; ``v_field_consensus`` turns it into
``question_context`` / ``question_context_count`` / ``question_weight_e4`` (OQ4). A newer
evaluation of the field writes fresh links, which clears it (OQ5).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import EvaPhase
from orchestrator.db.models import (
    CandidateFieldDefinitionRow,
    EvaContextLink,
    EvaluationPass,
    EvaPacket,
    EvaPreset,
    Objective,
    Packet,
    PacketDependency,
    Project,
    Revocation,
    new_id,
)
from orchestrator.evaluation.normalise import normalise_quote, verify_quote
from orchestrator.events import record_event

log = logging.getLogger(__name__)


def _sha(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def intra_packet_hash(packet: Packet) -> str:
    """The packet's own stage-1 definition only (C.i.2.1): upstream edits never change it."""
    return _sha(
        packet.title, packet.prompt_description, packet.response_contract, packet.repo_target_path
    )


def project_text(project: Project) -> str:
    return f"{project.name}\n{project.brief}"


def objective_text(objective: Objective) -> str:
    return f"{objective.name}\n{objective.brief}"


@dataclass(frozen=True)
class Sections:
    """Section offsets of the source document a quote is attributed to (R6 OQ22).

    The document (``prompts.source_document``) is ``PROJECT … [OBJECTIVE …] TASK …``; after
    the same normalisation ``verify_quote`` uses, each occurrence of a quote covers a span, and
    every section that span overlaps is cited — so a quote matching several sections, or
    spanning a boundary, marks each one."""

    document: str

    def spans(self) -> dict[str, tuple[int, int]]:
        doc = normalise_quote(self.document)
        task = doc.find("TASK: ")
        objective = doc.find("OBJECTIVE: ")
        end_project = objective if 0 <= objective < task else task
        result = {"project": (0, end_project), "task": (task, len(doc))}
        if 0 <= objective < task:
            result["objective"] = (objective, task)
        return result

    def attribute(self, quote: str) -> set[str]:
        doc = normalise_quote(self.document)
        needle = normalise_quote(quote)
        found: set[str] = set()
        if not needle:
            return found
        spans = self.spans()
        start = doc.find(needle)
        while start >= 0:
            end = start + len(needle)
            found |= {n for n, (a, b) in spans.items() if start < b and end > a}
            start = doc.find(needle, start + 1)
        return found - {"task"}


def verified_quotes(session: Session, eva: EvaPacket) -> list[str]:
    """Verified SUPPORT/CONTRADICTION quotes of passes 3-6 of ``eva`` (or of its source)."""
    quotes: list[str] = []
    for row in session.scalars(
        select(EvaluationPass).where(
            EvaluationPass.eva_packet_id == eva.id, EvaluationPass.pass_no >= 3
        )
    ):
        for ev in row.evidence or []:
            if ev.get("verified") and ev.get("tag") in ("SUPPORT", "CONTRADICTION"):
                quotes.append(str(ev.get("quote", "")))
    return quotes


def hard_dependencies(session: Session, packet: Packet) -> list[Packet]:
    revoked = select(Revocation.target_id).where(Revocation.target_type == "dependency")
    targets = session.scalars(
        select(Packet)
        .join(PacketDependency, PacketDependency.depends_on_id == Packet.id)
        .where(PacketDependency.packet_id == packet.id, PacketDependency.id.not_in(revoked))
        .order_by(Packet.id)
    ).all()
    return list(targets)


def register_context_links(
    session: Session,
    eva: EvaPacket,
    packet: Packet,
    project: Project,
    objective: Objective | None,
    *,
    actor: Actor,
) -> list[EvaContextLink]:
    """Write the links of one finished pre-phase evaPacket (idempotent per evaPacket)."""
    if EvaPhase(eva.phase) is not EvaPhase.PRE:
        return []
    existing = session.scalars(
        select(EvaContextLink).where(EvaContextLink.eva_packet_id == eva.id)
    ).all()
    if existing:
        return list(existing)
    from orchestrator.evaluation.evaluator import packet_document

    sections = Sections(packet_document(session, packet)[0])
    cited: dict[str, str] = {}
    for quote in verified_quotes(session, eva):
        for name in sections.attribute(quote):
            cited.setdefault(name, quote)

    def link(**kw: object) -> EvaContextLink:
        row = EvaContextLink(
            id=new_id(), eva_packet_id=eva.id, packet_id=packet.id, field_name=eva.field_name, **kw
        )
        session.add(row)
        return row

    links = [
        link(
            source_type="project",
            source_id=project.id,
            source_ref=project.ref,
            source_hash=_sha(project_text(project)),
            source_revision=project.content_revision,
            link_strength="direct" if "project" in cited else "peripheral",
            link_reason="direct_quote" if "project" in cited else "ambient",
            quote=cited.get("project"),
        )
    ]
    if objective is not None:
        links.append(
            link(
                source_type="objective",
                source_id=objective.id,
                source_ref=objective.ref,
                source_hash=_sha(objective_text(objective)),
                source_revision=objective.content_revision,
                link_strength="direct" if "objective" in cited else "peripheral",
                link_reason="direct_quote" if "objective" in cited else "ambient",
                quote=cited.get("objective"),
            )
        )
    for dep in hard_dependencies(session, packet):
        links.append(
            link(
                source_type="packet",
                source_id=dep.id,
                source_ref=dep.ref,
                source_hash=intra_packet_hash(dep),
                source_revision=dep.content_revision,
                link_strength="direct",
                link_reason="hard_dep",
            )
        )
    definition = session.scalar(
        select(CandidateFieldDefinitionRow).where(
            CandidateFieldDefinitionRow.name == eva.field_name
        )
    )
    preset = session.get(EvaPreset, eva.preset_id)
    if definition is not None:
        links.append(
            link(
                source_type="definition",
                source_id=definition.name,
                source_ref=None,
                source_hash=_sha(definition.name, eva.definition_version),
                source_revision=eva.definition_version or definition.version,
                link_strength="direct",
                link_reason="definition",
            )
        )
    if preset is not None:
        links.append(
            link(
                source_type="definition",
                source_id=f"preset:{preset.field_name}:{EvaPhase(preset.phase).value}",
                source_ref=None,
                source_hash=_sha(preset.id),
                source_revision=preset.revision,
                link_strength="direct",
                link_reason="definition",
            )
        )
    record_event(
        session,
        actor=actor,
        action="eva_packet.context_links",
        entity_type="eva_packet",
        entity_id=eva.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={
            "field": eva.field_name,
            "links": [
                [lk.source_type, lk.source_ref or lk.source_id, lk.link_strength] for lk in links
            ],
        },
        reason="context registered without an LLM call (R6 OQ22)",
    )
    session.flush()
    return links


def question_links(
    session: Session, link_ids: list[str], *, actor: Actor, reason: str
) -> list[str]:
    """Flip links to questioned (false → true only; DB-enforced), evented."""
    if not link_ids:
        return []
    table = EvaContextLink.__table__
    session.flush()
    session.connection().execute(
        update(table)
        .where(table.c.id.in_(link_ids), table.c.is_questioned.is_(False))
        .values(is_questioned=True)
    )
    record_event(
        session,
        actor=actor,
        action="eva_context_link.questioned",
        entity_type="eva_context_link",
        after={"links": sorted(link_ids)},
        reason=reason,
    )
    session.flush()
    for obj in list(session.identity_map.values()):
        if isinstance(obj, EvaContextLink) and obj.id in link_ids:
            session.expire(obj)
    return link_ids


def reverify_links(session: Session, entity_type: str, entity_id: str, *, actor: Actor) -> None:
    """R6 OQ34: after an edit of a project or objective, re-run ``verify_quote`` for every
    quote-backed link to it; a quote that no longer verifies flips the link to questioned
    (it is already ``direct``)."""
    if entity_type == "project":
        source = session.get(Project, entity_id)
        text = project_text(source) if source is not None else None
    elif entity_type == "objective":
        source = session.get(Objective, entity_id)
        text = objective_text(source) if source is not None else None
    else:
        return
    if text is None:
        return
    broken = [
        link.id
        for link in session.scalars(
            select(EvaContextLink).where(
                EvaContextLink.source_type == entity_type,
                EvaContextLink.source_id == entity_id,
                EvaContextLink.quote.is_not(None),
                EvaContextLink.is_questioned.is_(False),
            )
        )
        if not verify_quote(link.quote or "", text)
    ]
    question_links(
        session, broken, actor=actor, reason=f"cited quote no longer in the edited {entity_type}"
    )
