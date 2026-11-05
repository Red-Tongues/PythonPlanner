"""Write exact evaPacket states into the database for consensus and readiness tests.

``seed_eva`` builds one evaPacket with the passes and per-item rows a real run would write for
the given candidate values, scores and *verified* evidence counts, leaving out ``missing``
passes. ``oracle_inputs`` turns the same specification into ``ItemInput`` for the pure oracle,
so every test compares the SQL view with ``decide()`` (D5 point 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import EvaPhase, EvaState, FieldKind, PassPlanName
from orchestrator.db.models import (
    CandidateFieldDefinitionRow,
    EvaluationCycle,
    EvaluationPass,
    EvaluationPassItem,
    EvaPacket,
    EvaPreset,
    Packet,
    new_id,
)
from orchestrator.evaluation.consensus import ItemInput, PassEvidence
from orchestrator.evaluation.evaluator import refresh_input_hash
from orchestrator.events import record_event

FIXTURE = Actor.system("fixture")
TARGET = {3: 2, 4: 1, 5: 1, 6: 2}
TEXT_FIELD = "test_summary"


@dataclass
class Item:
    va: str | None
    vb: str | None
    s: dict[int, int] = field(default_factory=dict)  # pass_no → score
    ev: dict[int, tuple[int, int, int]] = field(default_factory=dict)  # (sup, con, unverified)


def ensure_text_field(session: Session) -> None:
    """A text field for B3 cases (the registry has none); tier 3, so it never gates."""
    if session.scalar(
        select(CandidateFieldDefinitionRow).where(CandidateFieldDefinitionRow.name == TEXT_FIELD)
    ):
        return
    session.add(
        CandidateFieldDefinitionRow(
            name=TEXT_FIELD,
            label="Test summary",
            level="task",
            kind="text",
            max_chars=200,
            max_passes=4,
            prompt_hint="A one-line summary.",
            contract="test@1",
        )
    )
    session.add(
        EvaPreset(
            id=new_id(),
            field_name=TEXT_FIELD,
            phase="pre",
            revision=1,
            tier=3,
            required=False,
            hard=False,
            plan="combined",
            prompt_template="quickfire.v1",
            schema_version="1.0",
            policy_version="1.0",
            enabled=True,
        )
    )
    record_event(
        session, actor=FIXTURE, action="test.field", entity_type="candidate_field_definition"
    )
    record_event(session, actor=FIXTURE, action="test.preset", entity_type="eva_preset")
    session.flush()


def definition_row(session: Session, field_name: str) -> CandidateFieldDefinitionRow:
    row = session.scalar(
        select(CandidateFieldDefinitionRow).where(CandidateFieldDefinitionRow.name == field_name)
    )
    assert row is not None, field_name
    return row


def kind_of(session: Session, field_name: str) -> FieldKind:
    row = session.scalar(
        select(CandidateFieldDefinitionRow).where(CandidateFieldDefinitionRow.name == field_name)
    )
    assert row is not None, field_name
    return FieldKind(row.kind)


def _cycle(session: Session, packet: Packet, phase: str, target: str) -> EvaluationCycle:
    cycle = EvaluationCycle(
        id=new_id(),
        packet_id=packet.id,
        packet_version=packet.version,
        phase=phase,
        target_hash=target,
        pass_plan="combined@1",
        models={},
    )
    session.add(cycle)
    record_event(
        session,
        actor=FIXTURE,
        action="test.cycle",
        entity_type="evaluation_cycle",
        entity_id=cycle.id,
    )
    session.flush()
    return cycle


def seed_eva(
    session: Session,
    packet: Packet,
    field_name: str,
    items: dict[str, Item],
    *,
    phase: str = "pre",
    missing: set[int] | frozenset[int] = frozenset(),
    state: EvaState = EvaState.CROSS_SCORING,
    target_hash: str | None = None,
) -> EvaPacket:
    kind = kind_of(session, field_name)
    if target_hash is None:
        target_hash = refresh_input_hash(session, packet, FIXTURE) if phase == "pre" else "out"
    preset = session.scalars(
        select(EvaPreset)
        .where(EvaPreset.field_name == field_name, EvaPreset.phase == phase)
        .order_by(EvaPreset.revision.desc())
    ).first()
    assert preset is not None, f"no {phase} preset for {field_name}"
    cycle = _cycle(session, packet, phase, target_hash)
    keys = list(items) if kind is FieldKind.TAGSET else [""]
    if kind is FieldKind.TAGSET:
        definition = session.scalar(
            select(CandidateFieldDefinitionRow).where(
                CandidateFieldDefinitionRow.name == field_name
            )
        )
        keys = list(definition.vocabulary)  # type: ignore[union-attr]
    eva = EvaPacket(
        id=new_id(),
        packet_id=packet.id,
        packet_version=packet.version,
        cycle_id=cycle.id,
        preset_id=preset.id,
        field_name=field_name,
        definition_version=definition_row(session, field_name).version,
        phase=phase,
        plan=PassPlanName.COMBINED,
        item_keys=keys,
        target_hash=target_hash,
        model_config={},
        model_config_hash="x",
        content_address=new_id(),
        idempotency_key=new_id(),
        state=state,
        escalation=0,
    )
    session.add(eva)
    record_event(
        session, actor=FIXTURE, action="test.eva", entity_type="eva_packet", entity_id=eva.id
    )
    session.flush()
    passes = (1, 2, 3, 4) if kind is FieldKind.TEXT else (1, 2, 3, 4, 5, 6)
    for n in passes:
        if n in missing:
            continue
        if n in (1, 2):
            values = {k: (i.va if n == 1 else i.vb) for k, i in items.items()}
            present = {k: v for k, v in values.items() if v is not None}
            row = _pass(
                session,
                eva,
                n,
                value=sorted(present)
                if kind is FieldKind.TAGSET
                else next(iter(present.values()), None),
            )
            for key, value in present.items():
                session.add(
                    EvaluationPassItem(
                        id=new_id(),
                        pass_id=row.id,
                        eva_packet_id=eva.id,
                        pass_no=n,
                        item_key=key,
                        value_norm=value,
                        supports=0,
                        contradictions=0,
                        unverified=0,
                    )
                )
            continue
        target = TARGET[n]
        applicable = {k: i for k, i in items.items() if (i.vb if target == 2 else i.va) is not None}
        if not applicable:
            continue  # skipped: it would evaluate a NULL value (ruling 2)
        row = _pass(session, eva, n)
        for key, item in applicable.items():
            sup, con, unv = item.ev.get(n, (0, 0, 0))
            session.add(
                EvaluationPassItem(
                    id=new_id(),
                    pass_id=row.id,
                    eva_packet_id=eva.id,
                    pass_no=n,
                    item_key=key,
                    score=item.s.get(n),
                    supports=sup,
                    contradictions=con,
                    unverified=unv,
                )
            )
    session.flush()
    return eva


def _pass(session: Session, eva: EvaPacket, n: int, value: Any = None) -> EvaluationPass:
    row = EvaluationPass(
        id=new_id(),
        packet_id=eva.packet_id,
        field_name=eva.field_name,
        eval_cycle_id=eva.cycle_id,
        eva_packet_id=eva.id,
        model="fake-a" if n % 2 else "fake-b",
        pass_no=n,
        role="extract" if n < 3 else ("cross" if n < 5 else "self"),
        target_pass_no=TARGET.get(n),
        value=value,
        evidence=[],
        attempt=1,
        latency_ms=1,
    )
    session.add(row)
    session.flush()
    return row


def oracle_inputs(
    kind: FieldKind, items: dict[str, Item], missing: set[int] | frozenset[int] = frozenset()
) -> dict[str, ItemInput]:
    passes = (1, 2, 3, 4) if kind is FieldKind.TEXT else (1, 2, 3, 4, 5, 6)
    any_a = any(i.va is not None for i in items.values())
    any_b = any(i.vb is not None for i in items.values())
    required = {1, 2}
    if any_a:
        required |= {4, 5} & set(passes)
    if any_b:
        required |= {3, 6} & set(passes)
    complete = not (required & set(missing))
    out = {}
    for key, item in items.items():
        out[key] = ItemInput(
            kind=kind,
            value_a=None if 1 in missing else item.va,
            value_b=None if 2 in missing else item.vb,
            scores={n: (None if n in missing else item.s.get(n)) for n in (3, 4, 5, 6)},
            evidence={
                n: PassEvidence(*item.ev.get(n, (0, 0, 0)))
                for n in (3, 4, 5, 6)
                if n not in missing
            },
            complete=complete,
        )
    return out


def accept_all(
    session: Session, packet: Packet, *, skip: set[str] | frozenset[str] = frozenset()
) -> None:
    """Accepted consensus for every required pre-phase field (a ready evaluation set)."""
    from orchestrator.evaluation.readiness import current_presets_by_field

    eights = {3: 8, 4: 8, 5: 8, 6: 8}
    support = {4: (1, 0, 0), 5: (1, 0, 0)}
    for name, preset in current_presets_by_field(session, EvaPhase.PRE).items():
        if name in skip or not preset.required or preset.tier == 3:
            continue
        kind = kind_of(session, name)
        value = {FieldKind.SCORE: "5", FieldKind.BOOL: "true", FieldKind.TEXT: "x"}.get(kind)
        key = "other" if kind is FieldKind.TAGSET else ""
        seed_eva(session, packet, name, {key: Item(value or "on", value or "on", eights, support)})


def add_artifact(session: Session, packet: Packet, content: str | None = None):  # type: ignore[no-untyped-def]
    """An artifact row for ``packet`` (evented); its sha256 is what post-phase work targets."""
    import hashlib

    from orchestrator.db.models import Artifact

    content = content or f"output of {packet.ref} v{packet.version} #{new_id()[:8]}"
    artifact = Artifact(
        id=new_id(),
        packet_id=packet.id,
        path=f"out/{packet.ref}.md",
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        content=content,
    )
    session.add(artifact)
    record_event(
        session,
        actor=FIXTURE,
        action="test.artifact",
        entity_type="artifact",
        entity_id=artifact.id,
    )
    session.flush()
    return artifact


#: values that pass the default post-phase gates (requirement_alignment/correctness >= 6,
#: security_risk <= 6)
POST_VALUES = {"requirement_alignment": "8", "correctness": "8", "security_risk": "2"}


def accept_post(session: Session, packet: Packet, artifact) -> None:  # type: ignore[no-untyped-def]
    eights = {3: 8, 4: 8, 5: 8, 6: 8}
    support = {4: (1, 0, 0), 5: (1, 0, 0)}
    for name, value in POST_VALUES.items():
        seed_eva(
            session,
            packet,
            name,
            {"": Item(value, value, eights, support)},
            phase="post",
            target_hash=artifact.sha256,
        )


def complete_with_evidence(session: Session, packet: Packet, actor) -> None:  # type: ignore[no-untyped-def]
    """Complete ``packet`` the only way the gate allows: an artifact plus accepted post-phase
    evaluations of exactly that artifact (M2.1 fixes 3 and 5)."""
    from orchestrator.contracts import PacketStatus
    from orchestrator.lifecycle import transition

    drive_to(packet, PacketStatus.EVALUATING_POST)
    accept_post(session, packet, add_artifact(session, packet))
    transition(packet, PacketStatus.COMPLETED, actor, "accepted with post-evaluation evidence")


def drive_to(packet: Packet, target) -> None:  # type: ignore[no-untyped-def]
    """Move ``packet`` to ``target`` along the shortest path of kernel moves in the M3 table
    (a test shortcut past the claim and the write; the table and the DB trigger still apply)."""
    from collections import deque

    from orchestrator.contracts import PacketStatus
    from orchestrator.lifecycle import transition
    from orchestrator.transitions import PACKET_TRANSITIONS

    start = PacketStatus(packet.status).value
    target = PacketStatus(target).value
    previous: dict[str, str | None] = {start: None}
    todo = deque([start])
    while todo:
        node = todo.popleft()
        for (a, b), rule in sorted(PACKET_TRANSITIONS.items()):
            if a == node and b not in previous and "system" in rule.actors:
                previous[b] = node
                todo.append(b)
    if target not in previous:
        raise AssertionError(f"no kernel path {start} → {target}")
    path: list[str] = []
    node: str | None = target
    while node != start:
        path.append(node)  # type: ignore[arg-type]
        node = previous[node]  # type: ignore[index]
    for status in reversed(path):
        transition(packet, PacketStatus(status), FIXTURE, "test fixture path")
