"""The immediate ``gen_eva`` gate: grade, seal, spawn the re-imagined child (M3a part e).

T1-C v3 §1/§3 and R6 OQ13-OQ18, OQ20, OQ24, OQ27. When a generation returns output,
``complete_generation`` runs **at once** — it reads no queue view (AC28):

1. **Crash or output.** No gradable output (``None`` or blank) is a crash (``runs.record_crash``):
   no grade, no seal, no child (Q4).
2. **Artifact.** The output becomes an artifact; the run is validated and accepted with its
   manifest hash. R6 OQ15 wants it committed to ``refs/gen/<run_id>`` before grading; M3a has no
   production repository wired in (M3b/M4), so ``sealed_bundles.output_ref`` stays NULL.
3. **Post-phase evaPackets** grade the artifact (``evaluating_post``); ``composite_grade_e4``
   (OQ16) = 0.30 requirement_alignment + 0.25 testability + 0.20 acceptance_criteria_coverage +
   0.15 risk_assessment + 0.10 sonar_hotspot_alignment, on ×1000 scores, integer floor; without
   an accepted sonar value its 0.10 goes 0.05 each to testability and risk_assessment.
4. **Seal** (OQ13, OQ20): the outbox is drained; the packet as it stood at the run's
   ``claim_content_revision`` is rebuilt from ``field_revisions`` and stored as canonical JSON in
   ``content_blobs``; one ``sealed_bundles`` row (``UNIQUE(packet_id)``); the packet moves
   ``evaluating_post → sealed`` and locks; the run ``accepted → succeeded``.
5. **Child** (OQ14, OQ17, OQ24, OQ27 with Argon K1): exactly one child genPacket —
   ``parent_id``, ``lineage_key = parent.lineage_key``, ``root_id = parent.root_id`` (the split
   tree is unchanged), ``lineage_depth + 1``, same position, the parent's
   *current* content (edits made during the run carry over, OQ20), a re-imagined prompt, the
   parent bundle id/hash, and copies of the parent's latest pre-phase results, each marked
   questioned (``carried``) so evaQueue re-evaluates them. It starts ``ready`` when readiness
   holds (``draft → ready``), else ``evaluating_pre``; if the parent's grade is at least
   ``lineage_stop_grade_e4`` (8,500) it is created ``held`` (``hold_reason = grade_met``).

Everything happens in the caller's transaction, and ``spawn_child`` is idempotent per bundle,
so a failure anywhere rolls back to the generation's result, and a retry yields exactly one
bundle and one child (AC31).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.contracts import EvaPhase, EvaState, GenerationRunState, PacketStatus
from orchestrator.db.models import (
    Artifact,
    EvaContextLink,
    EvaluationCycle,
    EvaluationPass,
    EvaluationPassItem,
    EvaPacket,
    GenerationRun,
    Objective,
    Packet,
    SealedBundle,
    new_id,
    utcnow,
)
from orchestrator.db.refs import next_packet_ref
from orchestrator.errors import OrchestratorError
from orchestrator.events import record_event
from orchestrator.llm import LLMProvider
from orchestrator.settings_store import get_int

log = logging.getLogger(__name__)
KERNEL = Actor.system("kernel")
GEN_EVA = Actor.system("gen_eva")
R = GenerationRunState
S = PacketStatus

#: OQ16 composite weights, ×10^4
COMPOSITE_WEIGHTS: dict[str, int] = {
    "requirement_alignment": 3000,
    "testability": 2500,
    "acceptance_criteria_coverage": 2000,
    "risk_assessment": 1500,
    "sonar_hotspot_alignment": 1000,
}


class GenEvaError(OrchestratorError):
    """The generation cannot be graded or sealed in its current state."""


@dataclass
class GenEvaResult:
    crashed: bool = False
    grade_e4: int | None = None
    bundle: SealedBundle | None = None
    child: Packet | None = None


# --------------------------------------------------------------------------- grade (OQ16)


def post_scores(session: Session, packet: Packet) -> dict[str, int]:
    """Accepted post-phase score fields of ``packet``, ×1000."""
    rows = session.execute(
        text(
            "SELECT field_name, final_value FROM v_field_consensus WHERE packet_id = :id "
            "AND phase = 'post' AND status = 'accepted' AND kind = 'score' AND item_key = ''"
        ),
        {"id": packet.id},
    ).all()
    return {name: int(value) * 1000 for name, value in rows if value is not None}


def composite_grade_e4(scores: dict[str, int]) -> int:
    """R6 OQ16, integer floor. A missing field counts 0, except sonar: when it has no accepted
    value its weight moves 500 each to testability and risk_assessment."""
    weights = dict(COMPOSITE_WEIGHTS)
    if "sonar_hotspot_alignment" not in scores:
        weights["sonar_hotspot_alignment"] = 0
        weights["testability"] += 500
        weights["risk_assessment"] += 500
    return sum(w * scores.get(f, 0) for f, w in weights.items()) // 10000


# --------------------------------------------------------------------------- prompt (OQ17)


class PromptSynth(Protocol):
    """A stateless rewrite of the child prompt (OQ17). Returns ``None`` to fall back."""

    def __call__(self, template_prompt: str, *, grade_e4: int) -> str | None: ...


def fast_synth_placeholder(
    template_prompt: str, *, grade_e4: int
) -> str | None:  # @ORCHESTRATOR:PLACEHOLDER
    """@ORCHESTRATOR:PLACEHOLDER — R6 OQ17's ``fast_synth`` LLM rewrite (below 5,000). No local
    model is wired into M3a, so this stub always declines and the deterministic template is
    used. A real implementation must be stateless and return Pydantic-validated text
    (Golden Rules 2-3); any failure must also fall back to the template."""
    return None


def _critiques(session: Session, packet: Packet, limit: int = 3) -> list[str]:
    """The ``limit`` lowest-scored accepted post-phase fields, with a model's reason."""
    rows = session.execute(
        text(
            "SELECT field_name, final_value, eva_packet_id FROM v_field_consensus "
            "WHERE packet_id = :id AND phase = 'post' AND kind = 'score' AND item_key = '' "
            "AND status = 'accepted' ORDER BY CAST(final_value AS INTEGER), field_name"
        ),
        {"id": packet.id},
    ).all()
    out: list[str] = []
    for name, value, eva_id in rows[:limit]:
        reason = session.scalar(
            select(EvaluationPass.reason)
            .where(EvaluationPass.eva_packet_id == eva_id, EvaluationPass.pass_no >= 3)
            .order_by(EvaluationPass.pass_no)
            .limit(1)
        )
        out.append(f"{name} {value}/10: {reason or 'no reason recorded'}")
    return out


def _contradictions(session: Session, packet: Packet) -> list[str]:
    quotes: list[str] = []
    for row in session.scalars(
        select(EvaluationPass)
        .join(EvaPacket, EvaPacket.id == EvaluationPass.eva_packet_id)
        .where(EvaPacket.packet_id == packet.id, EvaPacket.phase == EvaPhase.POST)
        .order_by(EvaluationPass.created_at, EvaluationPass.id)
    ):
        for ev in row.evidence or []:
            if ev.get("verified") and ev.get("tag") == "CONTRADICTION":
                quote = str(ev.get("quote", ""))
                if quote not in quotes:
                    quotes.append(quote)
    return quotes


def reimagined_prompt(
    session: Session,
    parent: Packet,
    *,
    artifact_hash: str,
    grade_e4: int,
    synth: PromptSynth | None = None,
) -> str:
    """OQ17: the deterministic template — objective, the parent's prompt and output reference,
    the top-3 failed critiques and verified contradiction quotes; below
    ``gen_eva.synth_below_e4`` a ``synth`` rewrite is tried, falling back to the template."""
    objective = session.get(Objective, parent.objective_id) if parent.objective_id else None
    lines = []
    if objective is not None:
        lines.append(
            f"Objective: {objective.name}" + (f" — {objective.brief}" if objective.brief else "")
        )
    lines += [
        parent.prompt_description.strip(),
        "",
        f"Re-imagined from {parent.ref} (gen_eva grade {grade_e4}/10000; previous output "
        f"blob {artifact_hash}).",
    ]
    critiques = _critiques(session, parent)
    if critiques:
        lines.append("Address these weaknesses of the previous attempt:")
        lines += [f"- {c}" for c in critiques]
    contradictions = _contradictions(session, parent)
    if contradictions:
        lines.append("Verified contradictions in the previous output:")
        lines += [f'- "{q}"' for q in contradictions[:5]]
    template = "\n".join(lines)
    if grade_e4 < get_int(session, "gen_eva.synth_below_e4", 5000):
        try:
            rewritten = (synth or fast_synth_placeholder)(template, grade_e4=grade_e4)
        except Exception as exc:  # any synth failure falls back to the template (OQ17)
            log.info("fast_synth failed for %s (%s); using the template", parent.ref, exc)
            rewritten = None
        if rewritten and rewritten.strip():
            return rewritten.strip()
    return template


# --------------------------------------------------------------------------- seal (OQ13, OQ20)


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def seal_packet(
    session: Session,
    packet: Packet,
    run: GenerationRun,
    artifact: Artifact,
    grade_e4: int,
    *,
    actor: Actor = GEN_EVA,
) -> SealedBundle:
    """Freeze ``packet`` as it stood at the run's claim into ``sealed_bundles`` and lock it."""
    from orchestrator.lifecycle import transition
    from orchestrator.revisions import has_undrained, snapshot_at, store_blob

    if PacketStatus(packet.status) is not S.EVALUATING_POST:
        raise GenEvaError(
            f"{packet.ref} is {PacketStatus(packet.status).value}, not evaluating_post"
        )
    revision = run.claim_content_revision if run.claim_content_revision is not None else 0
    content = snapshot_at(session, packet, revision)  # drains the outbox first
    if has_undrained(session, packet.id):
        raise GenEvaError(f"{packet.ref} has undrained revisions")
    snapshot = {
        "packet": {"id": packet.id, "ref": packet.ref, "content_revision": revision},
        "content": content,
        "run": {"id": run.id, "fencing_token": run.fencing_token},
        "artifact": {"sha256": artifact.sha256, "path": artifact.path},
        "post_scores_e4": post_scores(session, packet),
        "composite_grade_e4": grade_e4,
    }
    canonical = _canonical(snapshot)
    snapshot_hash = store_blob(session, canonical)
    bundle_hash = hashlib.sha256(
        _canonical([snapshot_hash, grade_e4, artifact.sha256]).encode()
    ).hexdigest()
    bundle = SealedBundle(
        id=new_id(),
        packet_id=packet.id,
        run_id=run.id,
        claim_content_revision=revision,
        snapshot_hash=snapshot_hash,
        bundle_hash=bundle_hash,
        composite_grade_e4=grade_e4,
        artifact_hash=artifact.sha256,
        # @ORCHESTRATOR:PLACEHOLDER (M3b: OQ15 refs/gen wiring) — the output is not committed
        # to refs/gen/<run_id> before gen_eva and output_ref stays NULL: M3a has no production
        # repository. Mandatory M3b acceptance criterion (M3a spec, "Carry-over to M3b").
        output_ref=None,
    )
    session.add(bundle)
    record_event(
        session,
        actor=actor,
        action="packet.sealed",
        entity_type="sealed_bundle",
        entity_id=bundle.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        after={
            "grade_e4": grade_e4,
            "bundle_hash": bundle_hash,
            "claim_content_revision": revision,
            "current_content_revision": packet.content_revision,
        },
        reason="gen_eva graded the generation; the claim-time snapshot is sealed (R6 OQ13/OQ20)",
    )
    session.flush()
    record_event(
        session,
        actor=actor,
        action="packet.run_released",
        entity_type="packet",
        entity_id=packet.id,
        entity_ref=packet.ref,
        entity_version=packet.version,
        before={"active_run_id": run.id},
        after={"active_run_id": None},
        reason="the run's output is sealed",
    )
    packet.active_run_id = None
    packet.claimed_by = None
    packet.lease_expires_at = None
    transition(packet, S.SEALED, actor, f"sealed after gen_eva (grade {grade_e4})", session=session)
    return bundle


# --------------------------------------------------------------------------- child (OQ24)


def _copy_results(session: Session, parent: Packet, child: Packet, actor: Actor) -> list[str]:
    """Copy the parent's current pre-phase evaPackets onto the child (bound to the child's own
    intra-packet hash), each with a questioned ``carried`` link to the parent (OQ24)."""
    from orchestrator.evaluation.evaluator import refresh_input_hash

    target = refresh_input_hash(session, child, actor)
    sources = (
        session.execute(
            text(
                "SELECT DISTINCT eva_packet_id FROM v_field_consensus WHERE packet_id = :id "
                "AND phase = 'pre' AND is_current = 1"
            ),
            {"id": parent.id},
        )
        .scalars()
        .all()
    )
    if not sources:
        return []
    cycle = EvaluationCycle(
        id=new_id(),
        packet_id=child.id,
        packet_version=child.version,
        phase=EvaPhase.PRE,
        target_hash=target,
        project_id=child.project_id,
        objective_id=child.objective_id,
        pass_plan="carried",
        models={},
        state="complete",
        completed_at=utcnow(),
    )
    session.add(cycle)
    record_event(
        session,
        actor=actor,
        action="evaluation.carried",
        entity_type="evaluation_cycle",
        entity_id=cycle.id,
        entity_ref=child.ref,
        entity_version=child.version,
        after={"from": parent.ref, "eva_packets": len(sources)},
        reason="R6 OQ24: the parent's latest pre-phase results carry to the child, questioned",
    )
    session.flush()
    carried: list[str] = []
    for source_id in sorted(sources):
        source = session.get(EvaPacket, source_id)
        assert source is not None
        copy = EvaPacket(
            id=new_id(),
            packet_id=child.id,
            packet_version=child.version,
            cycle_id=cycle.id,
            preset_id=source.preset_id,
            field_name=source.field_name,
            definition_version=source.definition_version,
            phase=EvaPhase.PRE,
            plan=source.plan,
            item_keys=source.item_keys,
            target_hash=target,
            model_config=source.model_config,
            model_config_hash=source.model_config_hash,
            content_address=hashlib.sha256(f"carried:{source.id}:{child.id}".encode()).hexdigest(),
            idempotency_key=hashlib.sha256(f"carried:{source.id}:{child.id}".encode()).hexdigest(),
            state=EvaState(source.state),
            escalation=source.escalation,
            reused_from_id=source.id,
        )
        session.add(copy)
        record_event(
            session,
            actor=actor,
            action="eva_packet.carried",
            entity_type="eva_packet",
            entity_id=copy.id,
            entity_ref=child.ref,
            after={"field": copy.field_name, "from": source.id},
        )
        session.flush()
        for old in session.scalars(
            select(EvaluationPass).where(EvaluationPass.eva_packet_id == source.id)
        ).all():
            new = EvaluationPass(
                id=new_id(),
                packet_id=child.id,
                field_name=old.field_name,
                eval_cycle_id=cycle.id,
                eva_packet_id=copy.id,
                run_id=old.run_id,
                model=old.model,
                pass_no=old.pass_no,
                role=old.role,
                target_pass_no=old.target_pass_no,
                value=old.value,
                score=old.score,
                evidence=old.evidence,
                reason=old.reason,
                attempt=old.attempt,
                latency_ms=0,
            )
            session.add(new)
            session.flush()
            for item in session.scalars(
                select(EvaluationPassItem).where(EvaluationPassItem.pass_id == old.id)
            ):
                session.add(
                    EvaluationPassItem(
                        id=new_id(),
                        pass_id=new.id,
                        eva_packet_id=copy.id,
                        pass_no=item.pass_no,
                        item_key=item.item_key,
                        value_norm=item.value_norm,
                        score=item.score,
                        supports=item.supports,
                        contradictions=item.contradictions,
                        unverified=item.unverified,
                    )
                )
        session.add(
            EvaContextLink(
                id=new_id(),
                eva_packet_id=copy.id,
                packet_id=child.id,
                field_name=copy.field_name,
                source_type="packet",
                source_id=parent.id,
                source_ref=parent.ref,
                source_hash=parent.input_hash or "",
                source_revision=parent.content_revision,
                link_strength="direct",
                link_reason="carried",
                is_questioned=True,
            )
        )
        session.flush()
        carried.append(copy.field_name)
    return carried


def inherit_overrides(
    session: Session, parent: Packet, child: Packet, *, actor: Actor = GEN_EVA
) -> list[str]:
    """M3a.1 (Argon ruling): copy the parent's **active, user-originated** HOLD and BLOCK queue
    overrides to the child, in the seal transaction. Origin actor, reason, scope and expiry are
    kept; ``inherited_from_id`` and ``note`` record the source. Dependency blocks (unmet edges)
    and system holds (``hold_reason`` statuses such as ``crash_limit``/``grade_met``) are not
    overrides and are never copied. Releasing the child's hold is a normal user action."""
    from orchestrator.contracts import OverrideKind
    from orchestrator.db.models import QueueOverride
    from orchestrator.governance import active_overrides

    copied: list[str] = []
    for source in active_overrides(session, parent):
        if OverrideKind(source.kind) not in (OverrideKind.HOLD, OverrideKind.BLOCK):
            continue
        if not source.actor.startswith("user:"):
            continue
        copy = QueueOverride(
            id=new_id(),
            packet_id=child.id,
            packet_ref=child.ref,
            kind=source.kind,
            scope=source.scope,
            target_version=child.version,
            value=source.value,
            actor=source.actor,
            reason=source.reason,
            expires_at=source.expires_at,
            inherited_from_id=source.id,
            note=f"inherited from {parent.ref}",
        )
        session.add(copy)
        record_event(
            session,
            actor=actor,
            action="queue_override.inherited",
            entity_type="queue_override",
            entity_id=copy.id,
            entity_ref=child.ref,
            entity_version=child.version,
            after={
                "kind": OverrideKind(copy.kind).value,
                "origin_actor": copy.actor,
                "inherited_from": source.id,
                "expires_at": copy.expires_at.isoformat() if copy.expires_at else None,
            },
            reason=f"inherited from {parent.ref} at its seal (M3a.1)",
        )
        session.flush()
        copied.append(copy.id)
    return copied


def child_of(session: Session, bundle: SealedBundle) -> Packet | None:
    return session.scalar(select(Packet).where(Packet.parent_bundle_id == bundle.id))


def spawn_child(
    session: Session,
    parent: Packet,
    bundle: SealedBundle,
    prompt: str,
    *,
    actor: Actor = GEN_EVA,
) -> Packet:
    """Exactly one re-imagined child per sealed bundle (idempotent)."""
    from orchestrator.evaluation.readiness import readiness
    from orchestrator.lifecycle import transition

    existing = child_of(session, bundle)
    if existing is not None:
        return existing
    child = Packet(
        id=new_id(),
        ref=next_packet_ref(session),
        project_id=parent.project_id,
        objective_id=parent.objective_id,
        position=parent.position,
        packet_kind=parent.packet_kind,
        title=parent.title,  # OQ24: no suffix; the generation is shown from lineage_depth
        prompt_description=prompt,
        response_structure=parent.response_structure,
        response_contract=parent.response_contract,
        repo_target_path=parent.repo_target_path,
        channel=parent.channel,
        priority_level=parent.priority_level,
        parent_id=parent.id,
        root_id=parent.root_id,  # Argon K1: the federation tree is unchanged by re-imagination
        depth=parent.depth,
        lineage_key=parent.lineage_key,  # Argon K1: the same re-imagination lineage
        lineage_depth=parent.lineage_depth + 1,
        parent_bundle_id=bundle.id,
        parent_bundle_hash=bundle.bundle_hash,
        branch_reason=f"re-imagined from {parent.ref} (bundle {bundle.id}, grade "
        f"{bundle.composite_grade_e4})",
        status=S.DRAFT,
        source="gen_eva",
    )
    session.add(child)
    record_event(
        session,
        actor=actor,
        action="packet.reimagined",
        entity_type="packet",
        entity_id=child.id,
        entity_ref=child.ref,
        entity_version=child.version,
        after={
            "parent": parent.ref,
            "root_id": child.root_id,
            "lineage_key": child.lineage_key,
            "lineage_depth": child.lineage_depth,
            "parent_bundle": bundle.id,
            "grade_e4": bundle.composite_grade_e4,
        },
        reason="every seal spawns exactly one re-imagined child genPacket (T1-C v3 §1)",
    )
    session.flush()
    _copy_results(session, parent, child, actor)
    inherit_overrides(session, parent, child, actor=actor)
    if bundle.composite_grade_e4 >= get_int(session, "lineage_stop_grade_e4", 8500):
        record_event(
            session,
            actor=KERNEL,
            action="packet.hold_reason",
            entity_type="packet",
            entity_id=child.id,
            entity_ref=child.ref,
            after={"hold_reason": "grade_met"},
        )
        child.hold_reason = "grade_met"
        transition(
            child,
            S.HELD,
            KERNEL,
            f"parent graded {bundle.composite_grade_e4} ≥ lineage stop (R6 OQ14); "
            "un-holding is a human action",
            session=session,
        )
    elif readiness(session, child).ready:
        transition(
            child, S.READY, KERNEL, "carried fields satisfy readiness (R6 OQ27)", session=session
        )
    else:
        transition(child, S.EVALUATING_PRE, KERNEL, "entering evaQueue", session=session)
    return child


# --------------------------------------------------------------------------- the gate


def complete_generation(
    session: Session,
    run: GenerationRun,
    output: str | None,
    provider: LLMProvider,
    *,
    lock: Any,
    now: datetime | None = None,
    synth: PromptSynth | None = None,
    path: str | None = None,
    before_spawn: Callable[[], None] | None = None,
) -> GenEvaResult:
    """The immediate post-generation gate (AC28-AC33). ``before_spawn`` is a test seam."""
    from orchestrator.evaluation.evaluator import evaluate_packet
    from orchestrator.queue import write_epoch_snapshot
    from orchestrator.revisions import drain_outbox
    from orchestrator.runs import StaleRun, _packet_cas, binding_mismatch, move_run, record_crash

    if output is None or not output.strip():
        record_crash(session, run, "no gradable output", lock=lock, now=now)
        return GenEvaResult(crashed=True)
    lock.fence(session, now=now)
    why = binding_mismatch(session, run, now=now)
    if why is not None:
        raise StaleRun(f"run {run.id}: {why}")
    packet = session.get(Packet, run.packet_id)
    assert packet is not None
    drain_outbox(session, actor=KERNEL)
    sha = hashlib.sha256(output.encode()).hexdigest()
    artifact = Artifact(
        id=new_id(),
        packet_id=packet.id,
        path=path or packet.repo_target_path or f"out/{packet.ref}.md",
        sha256=sha,
        content=output,
    )
    session.add(artifact)
    record_event(
        session,
        actor=KERNEL,
        action="artifact.created",
        entity_type="artifact",
        entity_id=artifact.id,
        entity_ref=packet.ref,
        after={"sha256": sha, "run": run.id},
    )
    session.flush()
    state = GenerationRunState(run.state)
    if state is R.DISPATCHED:
        move_run(session, run, R.AWAITING_RESULT, KERNEL, "result arrived")
        state = R.AWAITING_RESULT
    if state is R.AWAITING_RESULT:
        move_run(session, run, R.VALIDATING, KERNEL, "gradable output")
    move_run(session, run, R.ACCEPTED, KERNEL, "output accepted for gen_eva", manifest_hash=sha)
    _packet_cas(
        session,
        packet,
        S.EVALUATING_POST,
        KERNEL,
        "generation produced gradable output: immediate gen_eva",
        where=[Packet.__table__.c.active_run_id == run.id],
    )
    evaluate_packet(session, packet, provider, phase=EvaPhase.POST, actor=GEN_EVA)
    grade = composite_grade_e4(post_scores(session, packet))
    bundle = seal_packet(session, packet, run, artifact, grade)
    move_run(session, run, R.SUCCEEDED, KERNEL, f"sealed (grade {grade})")
    if before_spawn is not None:
        before_spawn()
    prompt = reimagined_prompt(session, packet, artifact_hash=sha, grade_e4=grade, synth=synth)
    child = spawn_child(session, packet, bundle, prompt)
    write_epoch_snapshot(session, "completion", now=now)
    return GenEvaResult(grade_e4=grade, bundle=bundle, child=child)


#: R6 Q5 / AC35: the documented lineage-history query — every member of re-imagination lineage
#: ``:lineage`` (a ``lineage_key``; Argon K1), oldest generation first, with its grade, bundle
#: hash and crash count
LINEAGE_HISTORY_SQL = """SELECT p.ref, p.lineage_depth, p.status, sb.composite_grade_e4,
       sb.bundle_hash, p.crash_count, p.parent_id
FROM packets p
LEFT JOIN sealed_bundles sb ON sb.packet_id = p.id
WHERE p.lineage_key = :lineage
ORDER BY p.lineage_depth, p.id"""
