"""The evaPacket evaluator (M2 §3, B1, B3, B7–B9, B11, B12).

``evaluate_packet`` runs one evaluation cycle for one genPacket version and phase:

1. **Target and binding** — the source document (packet core + head project/objective core) is
   hashed; ``packets.input_hash`` is refreshed through a governed, evented update. The cycle
   records the packet, project and objective versions, the field-registry revision, the pass plan
   and both model configurations (B1).
2. **Tiered lazy gating** (D1) — tier 1 fields first; a tier-1 failure or dispute sends the packet
   to ``needs_review`` and nothing further is spawned; then tier 2, then tier 3.
3. **evaPackets** — one per field, content-addressed: an identical earlier evaluation (same
   target hash, preset revision, schema/policy versions, plan and model configuration) is reused
   by copying its passes; otherwise passes run.
4. **Batching** (D1, D3 research) — all model-A extractions, then all model-B extractions, then
   all model-A evaluations, then all model-B evaluations, one call at a time.
5. **Validation** — tolerant JSON extraction, strict Pydantic contracts, one targeted repair with
   a one-line correction (B7, B12); every attempt is a ``QE`` channel run; every valid pass is an
   append-only ``evaluation_passes`` row with per-item rows; quotes are verified (unverified ones
   are stored and excluded).
6. **Decision** — the SQL view ``v_eva_item_consensus`` decides; Python only records the outcome
   as the evaPacket state (golden rule 2).
7. **Hybrid escalation** (B12) — a field that failed validation, or ended disputed on tier 1, is
   re-run once with the ``split`` plan; still unresolved → ``needs_review``.
8. **Stale-write guard** (B9) — before every write the packet must still be the head with the
   same input hash; otherwise the evaPacket is marked ``superseded`` and nothing more is written.

Afterwards the consensus projections (``packets.tags`` / ``scores``) are written with a guarded
UPDATE (``WHERE id = :id AND input_hash = :hash``), ``evaluations_count`` is incremented and the
packet moves to ``ready`` or ``needs_review``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orchestrator.actors import Actor
from orchestrator.config import get_settings
from orchestrator.contracts import (
    Channel,
    CycleState,
    EvaPhase,
    EvaState,
    FieldKind,
    PacketStatus,
    PassPlanName,
    RunStatus,
)
from orchestrator.db.models import (
    CandidateFieldDefinitionRow,
    ChannelRun,
    EvaluationCycle,
    EvaluationPass,
    EvaluationPassItem,
    EvaPacket,
    EvaPreset,
    Exemplar,
    Objective,
    Packet,
    Project,
    new_id,
    utcnow,
)
from orchestrator.db.refs import allocate
from orchestrator.db.seed import current_registry_revision
from orchestrator.errors import ImmutableRecordError, OrchestratorError
from orchestrator.evaluation import prompts
from orchestrator.evaluation.contracts import (
    CONTRACT_VERSION,
    EVALUATION_CONTRACTS,
    candidate_model,
    concise_error,
    json_schema,
    tag_map_model,
)
from orchestrator.evaluation.normalise import (
    OutputFormatError,
    extract_json,
    normalise_value,
    verify_quote,
)
from orchestrator.evaluation.plan import PassSpec, load_plan
from orchestrator.events import record_event
from orchestrator.llm import LLMProvider, ModelPinningError, ModelSpec, ProviderError, model_specs
from orchestrator.repository import is_head, packet_snapshot
from orchestrator.settings_store import get_int

log = logging.getLogger(__name__)

EVALUATOR = Actor.system("evaluator")
PRE_STATUSES_TO_EVALUATE = frozenset(
    {
        PacketStatus.DRAFT,
        PacketStatus.READY,
        PacketStatus.NEEDS_REVIEW,
        PacketStatus.FAILED,
        PacketStatus.EVALUATING,
        PacketStatus.EVALUATING_PRE,
    }
)


class EvaluationError(OrchestratorError):
    """The packet cannot be evaluated in its current state."""


class StaleEvaluation(OrchestratorError):
    """The packet changed (new version or new input hash) while an evaPacket was running."""


class PassFailed(OrchestratorError):
    """No valid output after the allowed attempts."""


@dataclass
class EvaluationReport:
    packet_ref: str
    packet_version: int
    phase: EvaPhase
    cycle_id: str | None = None
    fields: dict[str, str] = field(default_factory=dict)  # field → evaPacket state
    reused: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    status: str | None = None  # packet status afterwards
    stale: bool = False
    calls: int = 0


# --------------------------------------------------------------------------- helpers


def sha256(*parts: Any) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def head_context(session: Session, packet: Packet) -> tuple[Project, Objective | None]:
    """The head versions of the packet's project and objective lineages."""
    from orchestrator.repository import get_objective, get_project

    project = session.get(Project, packet.project_id)
    objective = session.get(Objective, packet.objective_id) if packet.objective_id else None
    assert project is not None
    project = get_project(session, project.ref)
    if objective is not None:
        objective = get_objective(session, objective.ref)
    return project, objective


def packet_document(session: Session, packet: Packet) -> tuple[str, str]:
    """(source document, target hash) for a packet version's pre-phase evaPackets."""
    project, objective = head_context(session, packet)
    document = prompts.source_document(
        project_name=project.name,
        project_brief=project.brief,
        objective_name=objective.name if objective else None,
        objective_brief=objective.brief if objective else None,
        title=packet.title,
        description=packet.prompt_description,
    )
    return document, hashlib.sha256(document.encode()).hexdigest()


def refresh_input_hash(session: Session, packet: Packet, actor: Actor = EVALUATOR) -> str:
    """Set ``packets.input_hash`` to the current target hash (evented when it changes)."""
    _, target = packet_document(session, packet)
    if packet.input_hash != target:
        before = packet.input_hash
        packet.input_hash = target
        record_event(
            session,
            actor=actor,
            action="packet.input_hash",
            entity_type="packet",
            entity_id=packet.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            before={"input_hash": before},
            after={"input_hash": target},
            reason="evaluation inputs changed" if before else "first evaluation",
        )
        session.flush()
    return target


def current_presets(
    session: Session, phase: EvaPhase, fields: Iterable[str] | None = None
) -> list[tuple[EvaPreset, CandidateFieldDefinitionRow]]:
    """The newest enabled preset revision per field, joined to its enabled definition."""
    wanted = set(fields) if fields is not None else None
    rows = session.scalars(
        select(EvaPreset).where(EvaPreset.phase == phase).order_by(EvaPreset.revision)
    )
    latest: dict[str, EvaPreset] = {}
    for preset in rows:
        latest[preset.field_name] = preset
    definitions = {
        d.name: d
        for d in session.scalars(
            select(CandidateFieldDefinitionRow).where(CandidateFieldDefinitionRow.enabled)
        )
    }
    result = [
        (p, definitions[name])
        for name, p in latest.items()
        if p.enabled and name in definitions and (wanted is None or name in wanted)
    ]
    return sorted(result, key=lambda pair: (pair[0].tier, pair[0].field_name))


def field_info(definition: CandidateFieldDefinitionRow) -> prompts.FieldInfo:
    return prompts.FieldInfo(
        name=definition.name,
        label=definition.label,
        kind=FieldKind(definition.kind),
        prompt_hint=definition.prompt_hint,
        vocabulary=tuple(definition.vocabulary or ()),
        max_chars=definition.max_chars,
    )


# --------------------------------------------------------------------------- the evaluator


@dataclass
class _Work:
    eva: EvaPacket
    preset: EvaPreset
    definition: CandidateFieldDefinitionRow
    info: prompts.FieldInfo


class Evaluator:
    def __init__(
        self,
        session: Session,
        provider: LLMProvider,
        *,
        specs: dict[str, ModelSpec] | None = None,
        actor: Actor = EVALUATOR,
    ) -> None:
        self.session = session
        self.provider = provider
        self.actor = actor
        num_ctx = get_int(session, "evaluation.num_ctx", 4096)
        self.specs = specs or model_specs(get_settings(), num_ctx=num_ctx)
        self.max_tokens = get_int(session, "evaluation.max_tokens", 512)
        self.repair_attempts = get_int(session, "evaluation.repair_attempts", 1)
        self.max_attempts_final = get_int(session, "attempts.quickfire_evaluation", 4)
        self.max_escalations = get_int(session, "evaluation.max_escalations", 1)
        self.max_exemplars = get_int(session, "evaluation.max_exemplars", 3)
        self.calls = 0
        self._verified = False

    # ------------------------------------------------------------------ pinning (B11)

    def verify_models(self) -> None:
        if self._verified:
            return
        specs = [self.specs["a"], self.specs["b"]]
        try:
            digests = self.provider.verify_models(specs)
        except ModelPinningError as exc:
            record_event(
                self.session,
                actor=self.actor,
                action="evaluation.model_pinning_failed",
                entity_type="provider",
                after={"provider": self.provider.name, "error": str(exc)},
                reason="evaluator models do not match the pinned digests; evaluation refused",
            )
            self.session.flush()
            raise
        record_event(
            self.session,
            actor=self.actor,
            action="evaluation.models_verified",
            entity_type="provider",
            after={"provider": self.provider.name, "digests": digests},
        )
        self._verified = True

    def model_config(self, plan_label: str) -> dict[str, Any]:
        return {
            "a": self.specs["a"].config(),
            "b": self.specs["b"].config(),
            "plan": plan_label,
            "template": prompts.TEMPLATE_VERSION,
            "max_tokens": self.max_tokens,
            "provider": self.provider.name,
        }

    # ------------------------------------------------------------------ entry point

    def evaluate(
        self,
        packet: Packet,
        *,
        phase: EvaPhase = EvaPhase.PRE,
        fields: Iterable[str] | None = None,
        output: str | None = None,
    ) -> EvaluationReport:
        session = self.session
        report = EvaluationReport(packet.ref, packet.version, phase)
        calls_before = self.calls
        if packet.locked or not is_head(session, packet):
            raise ImmutableRecordError(f"{packet.ref} v{packet.version} is locked or superseded")
        status = PacketStatus(packet.status)

        if phase is EvaPhase.PRE:
            if status not in PRE_STATUSES_TO_EVALUATE:
                raise EvaluationError(f"{packet.ref} is {status.value}; cannot pre-evaluate now")
            target = refresh_input_hash(session, packet, self.actor)
            document, _ = packet_document(session, packet)
            if status is not PacketStatus.EVALUATING_PRE:
                self._transition(packet, PacketStatus.EVALUATING_PRE, "evaluation started")
        else:
            if status is not PacketStatus.EVALUATING_POST:
                raise EvaluationError(
                    f"{packet.ref} is {status.value}; post-evaluation needs evaluating_post"
                )
            if not output:
                raise EvaluationError("post-phase evaluation needs the generated output")
            document = output
            target = hashlib.sha256(output.encode()).hexdigest()

        self.verify_models()
        cycle = self._open_cycle(packet, phase, target)
        report.cycle_id = cycle.id

        tiers: dict[int, list[tuple[EvaPreset, CandidateFieldDefinitionRow]]] = {}
        for preset, definition in current_presets(session, phase, fields):
            tiers.setdefault(preset.tier, []).append((preset, definition))

        unresolved_tier1 = False
        try:
            for tier in sorted(tiers):
                work = [
                    self._eva_packet(packet, cycle, preset, definition, target, report)
                    for preset, definition in tiers[tier]
                ]
                self._run(work, document)
                self._finish(work, report)
                escalate = [
                    w
                    for w in work
                    if w.eva.escalation < self.max_escalations
                    and (
                        w.eva.state is EvaState.FAILED
                        or (w.eva.state is EvaState.DISPUTED and w.preset.tier == 1)
                    )
                ]
                if escalate:
                    retry = [self._escalate(w, cycle, report) for w in escalate]
                    self._run(retry, document)
                    self._finish(retry, report)
                if tier == 1 and any(
                    report.fields.get(p.field_name) in ("disputed", "failed")
                    for p, _ in tiers[tier]
                    if p.required
                ):
                    unresolved_tier1 = True
                    break  # D1: a tier-1 failure or dispute spawns nothing further
        except StaleEvaluation:
            report.stale = True
            report.calls = self.calls - calls_before
            return report

        cycle.state = CycleState.COMPLETE
        cycle.completed_at = utcnow()
        record_event(
            session,
            actor=self.actor,
            action="evaluation.cycle_complete",
            entity_type="evaluation_cycle",
            entity_id=cycle.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            after={
                "fields": report.fields,
                "reused": report.reused,
                "escalated": report.escalated,
                "phase": phase.value,
            },
        )
        session.flush()
        if phase is EvaPhase.PRE:
            self._after_pre(packet, target, unresolved_tier1, report)
        report.status = PacketStatus(packet.status).value
        report.calls = self.calls - calls_before
        return report

    # ------------------------------------------------------------------ cycle and evaPackets

    def _open_cycle(self, packet: Packet, phase: EvaPhase, target: str) -> EvaluationCycle:
        project, objective = head_context(self.session, packet)
        registry = current_registry_revision(self.session)
        cycle = EvaluationCycle(
            id=new_id(),
            packet_id=packet.id,
            packet_version=packet.version,
            phase=phase,
            target_hash=target,
            project_id=project.id,
            project_version=project.version,
            objective_id=objective.id if objective else None,
            objective_version=objective.version if objective else None,
            registry_revision_id=registry.id if registry else None,
            pass_plan=load_plan(PassPlanName.COMBINED).label,
            models={"a": self.specs["a"].config(), "b": self.specs["b"].config()},
        )
        self.session.add(cycle)
        record_event(
            self.session,
            actor=self.actor,
            action="evaluation.cycle_opened",
            entity_type="evaluation_cycle",
            entity_id=cycle.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            after={
                "phase": phase.value,
                "target_hash": target,
                "registry_revision": registry.revision if registry else None,
            },
        )
        self.session.flush()
        return cycle

    def _eva_packet(
        self,
        packet: Packet,
        cycle: EvaluationCycle,
        preset: EvaPreset,
        definition: CandidateFieldDefinitionRow,
        target: str,
        report: EvaluationReport,
        *,
        plan_name: PassPlanName | None = None,
        escalation: int = 0,
        escalated_from: EvaPacket | None = None,
    ) -> _Work:
        session = self.session
        plan = load_plan(plan_name or preset.plan)
        kind = FieldKind(definition.kind)
        item_keys = list(definition.vocabulary or []) if kind is FieldKind.TAGSET else [""]
        config = self.model_config(plan.label)
        config_hash = sha256(config)
        content_address = sha256(
            target,
            preset.id,
            preset.schema_version,
            preset.policy_version,
            plan.label,
            config_hash,
            item_keys,
            CONTRACT_VERSION,
        )
        key = sha256(packet.id, packet.version, preset.phase, content_address, escalation)
        info = field_info(definition)

        existing = session.scalar(select(EvaPacket).where(EvaPacket.idempotency_key == key))
        if existing is not None:  # resume (crash recovery) or already finished
            report.fields[preset.field_name] = EvaState(existing.state).value
            return _Work(existing, preset, definition, info)

        source = session.scalar(
            select(EvaPacket)
            .where(
                EvaPacket.content_address == content_address,
                EvaPacket.state.in_([EvaState.CONSENSUS, EvaState.DISPUTED]),
            )
            .order_by(EvaPacket.created_at.desc())
            .limit(1)
        )
        eva = EvaPacket(
            id=new_id(),
            packet_id=packet.id,
            packet_version=packet.version,
            cycle_id=cycle.id,
            preset_id=preset.id,
            field_name=preset.field_name,
            phase=preset.phase,
            plan=plan.name,
            item_keys=item_keys,
            target_hash=target,
            model_config=config,
            model_config_hash=config_hash,
            content_address=content_address,
            idempotency_key=key,
            state=EvaState(source.state) if source is not None else EvaState.PLANNED,
            escalation=escalation,
            escalated_from_id=escalated_from.id if escalated_from else None,
            reused_from_id=source.id if source is not None else None,
        )
        session.add(eva)
        record_event(
            session,
            actor=self.actor,
            action="eva_packet.reused" if source is not None else "eva_packet.planned",
            entity_type="eva_packet",
            entity_id=eva.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            after={
                "field": preset.field_name,
                "plan": plan.label,
                "escalation": escalation,
                "content_address": content_address,
                "reused_from": source.id if source is not None else None,
            },
        )
        session.flush()
        if source is not None:
            self._copy_passes(source, eva, cycle)
            report.reused.append(preset.field_name)
        report.fields[preset.field_name] = EvaState(eva.state).value
        return _Work(eva, preset, definition, info)

    def _copy_passes(self, source: EvaPacket, eva: EvaPacket, cycle: EvaluationCycle) -> None:
        """Content-addressed reuse: append copies of the source's passes under the new cycle."""
        passes = self.session.scalars(
            select(EvaluationPass).where(EvaluationPass.eva_packet_id == source.id)
        ).all()
        for old in passes:
            new = EvaluationPass(
                id=new_id(),
                packet_id=eva.packet_id,
                field_name=old.field_name,
                eval_cycle_id=cycle.id,
                eva_packet_id=eva.id,
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
            self.session.add(new)
            self.session.flush()
            for item in self.session.scalars(
                select(EvaluationPassItem).where(EvaluationPassItem.pass_id == old.id)
            ):
                self.session.add(
                    EvaluationPassItem(
                        id=new_id(),
                        pass_id=new.id,
                        eva_packet_id=eva.id,
                        pass_no=item.pass_no,
                        item_key=item.item_key,
                        value_norm=item.value_norm,
                        score=item.score,
                        supports=item.supports,
                        contradictions=item.contradictions,
                        unverified=item.unverified,
                    )
                )
        self.session.flush()

    def _escalate(self, work: _Work, cycle: EvaluationCycle, report: EvaluationReport) -> _Work:
        packet = self.session.get(Packet, work.eva.packet_id)
        assert packet is not None
        report.escalated.append(work.preset.field_name)
        return self._eva_packet(
            packet,
            cycle,
            work.preset,
            work.definition,
            work.eva.target_hash,
            report,
            plan_name=PassPlanName.SPLIT,
            escalation=work.eva.escalation + 1,
            escalated_from=work.eva,
        )

    # ------------------------------------------------------------------ running passes

    def _run(self, work: list[_Work], document: str) -> None:
        active = [
            w
            for w in work
            if EvaState(w.eva.state)
            in (EvaState.PLANNED, EvaState.EXTRACTING, EvaState.CROSS_SCORING)
        ]
        plan_passes = {w.eva.id: load_plan(w.eva.plan).passes_for(w.info.kind) for w in active}
        stages = ((1, 2), (3, 5, 4, 6))
        for stage in stages:
            for model in ("a", "b"):  # all model-A calls, then all model-B calls
                for w in active:
                    for spec in plan_passes[w.eva.id]:
                        if spec.no in stage and spec.model == model:
                            self._pass(w, spec, document)

    def _done(self, eva: EvaPacket, pass_no: int) -> EvaluationPass | None:
        return self.session.scalar(
            select(EvaluationPass).where(
                EvaluationPass.eva_packet_id == eva.id, EvaluationPass.pass_no == pass_no
            )
        )

    def _items(self, eva: EvaPacket, pass_no: int) -> dict[str, EvaluationPassItem]:
        rows = self.session.scalars(
            select(EvaluationPassItem).where(
                EvaluationPassItem.eva_packet_id == eva.id, EvaluationPassItem.pass_no == pass_no
            )
        )
        return {r.item_key: r for r in rows}

    def _check_current(self, eva: EvaPacket) -> None:
        """Stale-write guard (B9): the packet must still be the head with the same inputs."""
        packet = self.session.get(Packet, eva.packet_id)
        self.session.refresh(packet)  # type: ignore[arg-type]
        stale = packet is None or not is_head(self.session, packet) or packet.locked
        if not stale and eva.phase is EvaPhase.PRE:
            stale = packet.input_hash != eva.target_hash  # type: ignore[union-attr]
        if stale:
            self._set_state(eva, EvaState.SUPERSEDED, "the packet changed during evaluation")
            raise StaleEvaluation(f"{eva.field_name}: packet changed during evaluation")

    def _set_state(self, eva: EvaPacket, state: EvaState, reason: str | None = None) -> None:
        if eva.state is state:
            return
        before = EvaState(eva.state).value
        eva.state = state
        record_event(
            self.session,
            actor=self.actor,
            action="eva_packet.state",
            entity_type="eva_packet",
            entity_id=eva.id,
            entity_version=eva.packet_version,
            before={"state": before},
            after={"state": state.value, "field": eva.field_name},
            reason=reason,
        )
        self.session.flush()

    def _pass(self, w: _Work, spec: PassSpec, document: str) -> None:
        eva = w.eva
        if EvaState(eva.state) not in (
            EvaState.PLANNED,
            EvaState.EXTRACTING,
            EvaState.CROSS_SCORING,
        ) or self._done(eva, spec.no):
            return
        self._check_current(eva)
        kind = w.info.kind
        if spec.role == "extract":
            self._set_state(eva, EvaState.EXTRACTING)
            model = candidate_model(kind, list(w.info.vocabulary), w.info.max_chars)
            prompt = prompts.extraction_prompt(
                w.info, document, json_schema(model), self._exemplars(w.info.name)
            )
            try:
                parsed, run_id, attempt, latency = self._call(eva, spec, prompt, model, "extract")
            except PassFailed as exc:
                self._set_state(eva, EvaState.FAILED, str(exc))
                return
            self._check_current(eva)
            value = parsed.value  # type: ignore[attr-defined]
            if kind is FieldKind.TAGSET:
                items = {tag: "on" for tag in (value or [])}
                value = sorted(value) if value else None
            else:
                norm = normalise_value(kind, value)
                items = {"": norm} if norm is not None else {}
            row = self._write_pass(eva, spec, run_id, attempt, latency, value=value)
            for key, norm in items.items():
                self.session.add(
                    EvaluationPassItem(
                        id=new_id(),
                        pass_id=row.id,
                        eva_packet_id=eva.id,
                        pass_no=spec.no,
                        item_key=key,
                        value_norm=norm,
                        supports=0,
                        contradictions=0,
                        unverified=0,
                    )
                )
            self.session.flush()
            return

        # evaluation pass: skipped when the value it would evaluate is NULL (ruling 2)
        assert spec.target is not None
        target_items = self._items(eva, spec.target)
        if not target_items:
            return
        self._set_state(eva, EvaState.CROSS_SCORING)
        target_pass = self._done(eva, spec.target)
        candidate = target_pass.value if target_pass is not None else None
        keys = sorted(set(self._items(eva, 1)) | set(self._items(eva, 2)))
        applicable = sorted(target_items)
        plan = load_plan(eva.plan)
        merged: dict[str, dict[str, Any]] = {k: {} for k in applicable}
        latency_total, run_id, attempt = 0, None, 1
        for task in plan.calls:
            if kind is FieldKind.TAGSET:
                model: type[BaseModel] = tag_map_model(task, keys, set(applicable))
            else:
                model = EVALUATION_CONTRACTS[task]
            prompt = prompts.evaluation_prompt(
                task,
                w.info,
                document,
                candidate,
                json_schema(model),
                keys=keys,
                applicable=applicable,
            )
            try:
                parsed, run_id, attempt, latency = self._call(eva, spec, prompt, model, task)
            except PassFailed as exc:
                self._set_state(eva, EvaState.FAILED, str(exc))
                return
            latency_total += latency
            data = parsed.model_dump()
            per_key = data["items"] if kind is FieldKind.TAGSET else {"": data}
            for key in applicable:
                merged[key].update(per_key[key] or {})
        self._check_current(eva)

        evidence_rows: list[dict[str, Any]] = []
        counts: dict[str, tuple[int, int, int]] = {}
        for key, result in merged.items():
            sup = con = unv = 0
            for ev in result.get("evidence", []):
                verified = verify_quote(ev["quote"], document)
                evidence_rows.append({**ev, "item": key, "verified": verified})
                if not verified:
                    unv += 1
                elif ev["tag"] == "SUPPORT":
                    sup += 1
                else:
                    con += 1
            counts[key] = (sup, con, unv)
        scalar = merged.get("") if kind is not FieldKind.TAGSET else None
        reason = (
            scalar.get("reason")
            if scalar is not None
            else json.dumps({k: v.get("reason") for k, v in merged.items()})
        )
        row = self._write_pass(
            eva,
            spec,
            run_id,
            attempt,
            latency_total,
            score=scalar.get("accuracy_score") if scalar is not None else None,
            evidence=evidence_rows,
            reason=reason,
        )
        for key, result in merged.items():
            sup, con, unv = counts[key]
            self.session.add(
                EvaluationPassItem(
                    id=new_id(),
                    pass_id=row.id,
                    eva_packet_id=eva.id,
                    pass_no=spec.no,
                    item_key=key,
                    score=result.get("accuracy_score"),
                    supports=sup,
                    contradictions=con,
                    unverified=unv,
                )
            )
        self.session.flush()

    def _write_pass(
        self,
        eva: EvaPacket,
        spec: PassSpec,
        run_id: str | None,
        attempt: int,
        latency: int,
        *,
        value: Any = None,
        score: int | None = None,
        evidence: list | None = None,
        reason: str | None = None,
    ) -> EvaluationPass:
        row = EvaluationPass(
            id=new_id(),
            packet_id=eva.packet_id,
            field_name=eva.field_name,
            eval_cycle_id=eva.cycle_id,
            eva_packet_id=eva.id,
            run_id=run_id,
            model=self.specs[spec.model].name,
            pass_no=spec.no,
            role=spec.role,
            target_pass_no=spec.target,
            value=value,
            score=score,
            evidence=evidence or [],
            reason=reason,
            attempt=attempt,
            latency_ms=latency,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _exemplars(self, field_name: str) -> list[prompts.Exemplar]:
        rows = self.session.scalars(
            select(Exemplar)
            .where(Exemplar.field_name == field_name)
            .order_by(Exemplar.created_at)
            .limit(self.max_exemplars)
        )
        return [prompts.Exemplar(r.input_text, r.expected_value) for r in rows]

    def _call(
        self, eva: EvaPacket, spec: PassSpec, prompt: str, contract: type[BaseModel], task: str
    ) -> tuple[BaseModel, str, int, int]:
        """One model call with validation and targeted repairs; every attempt is a QE run."""
        model_spec = self.specs[spec.model]
        max_attempts = 1 + self.repair_attempts if eva.escalation == 0 else self.max_attempts_final
        schema = json_schema(contract)
        current_prompt = prompt
        latency_total = 0
        error = "no attempt made"
        for attempt in range(1, max_attempts + 1):
            run_id = f"QE{allocate(self.session, 'run:QE'):06d}"
            run = ChannelRun(
                id=new_id(),
                run_id=run_id,
                channel=Channel.QUICKFIRE_EVALUATION,
                packet_id=eva.packet_id,
                attempt=attempt,
                max_attempts=max_attempts,
                contract=f"{contract.__name__}@{CONTRACT_VERSION}",
                state="dispatched",
                idempotency_key=f"{eva.id}:{spec.no}:{task}:{attempt}",
                input_hash=hashlib.sha256(current_prompt.encode()).hexdigest(),
                policy_revision=eva.plan.value if hasattr(eva.plan, "value") else str(eva.plan),
                status=RunStatus.RUNNING,
                started_at=utcnow(),
            )
            log.info(
                "Quickfire Evaluation is verifying #%s [attempt %d/%d] %s P%d %s",
                run_id,
                attempt,
                max_attempts,
                eva.field_name,
                spec.no,
                task,
            )
            self.calls += 1
            parsed: BaseModel | None = None
            try:
                response = self.provider.complete(
                    current_prompt,
                    model=model_spec.name,
                    max_tokens=self.max_tokens,
                    temperature=float(model_spec.options.get("temperature", 0.0)),
                    json_mode=True,
                    schema=schema,
                    num_ctx=model_spec.num_ctx,
                    options=model_spec.options,
                )
                latency_total += response.latency_ms
                run.latency_ms = response.latency_ms
                run.output_hash = hashlib.sha256(response.text.encode()).hexdigest()
                parsed = contract.model_validate(extract_json(response.text))
                run.status, run.state = RunStatus.SUCCEEDED, "accepted"
            except (OutputFormatError, ValidationError, ValueError) as exc:
                error = concise_error(exc) if isinstance(exc, ValidationError) else str(exc)
                run.status, run.state, run.error = RunStatus.INVALID, "failed", error
            except ProviderError as exc:
                error = str(exc)[:240]
                run.status, run.state, run.error = RunStatus.FAILED, "failed", error
            run.finished_at = utcnow()
            self.session.add(run)
            record_event(
                self.session,
                actor=Actor.llm(
                    model_spec.name[:48].replace(":", "-").replace("/", "-") or "model"
                ),
                action="evaluation.attempt",
                entity_type="channel_run",
                entity_id=run.id,
                entity_version=eva.packet_version,
                after={
                    "run_id": run_id,
                    "field": eva.field_name,
                    "pass": spec.no,
                    "task": task,
                    "attempt": attempt,
                    "status": run.status.value,
                },
                reason=run.error,
            )
            self.session.flush()
            if parsed is not None:
                return parsed, run_id, attempt, latency_total
            current_prompt = prompt + prompts.correction_suffix(error)
        raise PassFailed(
            f"P{spec.no} {task}: no valid output after {max_attempts} attempts ({error})"
        )

    # ------------------------------------------------------------------ decisions

    def _finish(self, work: list[_Work], report: EvaluationReport) -> None:
        """Record the SQL decision as the evaPacket's terminal state."""
        for w in work:
            eva = w.eva
            if EvaState(eva.state) in (
                EvaState.FAILED,
                EvaState.SUPERSEDED,
                EvaState.CONSENSUS,
                EvaState.DISPUTED,
            ):
                report.fields[w.preset.field_name] = EvaState(eva.state).value
                continue
            statuses = (
                self.session.execute(
                    text("SELECT status FROM v_eva_item_consensus WHERE eva_packet_id = :id"),
                    {"id": eva.id},
                )
                .scalars()
                .all()
            )
            if not statuses or "pending" in statuses:
                self._set_state(eva, EvaState.FAILED, "incomplete after all passes")
            elif "disputed" in statuses:
                self._set_state(eva, EvaState.DISPUTED, "SQL consensus: disputed")
            else:
                self._set_state(eva, EvaState.CONSENSUS, "SQL consensus: accepted or null")
            report.fields[w.preset.field_name] = EvaState(eva.state).value

    def _after_pre(
        self, packet: Packet, target: str, unresolved_tier1: bool, report: EvaluationReport
    ) -> None:
        from orchestrator.evaluation.readiness import (
            evaluation_verdict,
            request_review,
            write_projections,
        )

        write_projections(self.session, packet, target, self.actor)
        packet.evaluations_count += 1  # governed update: evented below, guarded at flush
        record_event(
            self.session,
            actor=self.actor,
            action="packet.evaluated",
            entity_type="packet",
            entity_id=packet.id,
            entity_ref=packet.ref,
            entity_version=packet.version,
            after={"evaluations_count": packet.evaluations_count, "cycle": report.cycle_id},
        )
        self.session.flush()
        verdict = evaluation_verdict(self.session, packet, EvaPhase.PRE)
        if verdict.ok and not unresolved_tier1:
            self._transition(packet, PacketStatus.READY, "evaluation set complete and accepted")
        elif verdict.needs_review or unresolved_tier1:
            self._transition(
                packet,
                PacketStatus.NEEDS_REVIEW,
                "; ".join(verdict.blockers) or "tier-1 field unresolved",
            )
            request_review(self.session, packet, verdict, self.actor)

    def _transition(self, packet: Packet, status: PacketStatus, reason: str) -> None:
        from orchestrator.lifecycle import transition

        transition(packet, status, self.actor, reason, session=self.session)


def snapshot(packet: Packet) -> dict[str, Any]:  # re-exported for callers that log packets
    return packet_snapshot(packet)


# --------------------------------------------------------------------------- public API


def evaluate_packet(
    session: Session,
    packet: Packet,
    provider: LLMProvider,
    *,
    phase: EvaPhase = EvaPhase.PRE,
    fields: Iterable[str] | None = None,
    output: str | None = None,
    specs: dict[str, ModelSpec] | None = None,
    actor: Actor = EVALUATOR,
) -> EvaluationReport:
    return Evaluator(session, provider, specs=specs, actor=actor).evaluate(
        packet, phase=phase, fields=fields, output=output
    )


def pending_packets(session: Session, limit: int | None = None) -> list[Packet]:
    """Head genPackets whose pre-phase evaluation set is missing or stale (needs_review is left
    alone: no evaluation loop on a dispute — B9)."""
    from orchestrator.repository import _heads

    candidates = session.scalars(
        _heads(Packet)
        .where(
            Packet.locked.is_(False),
            Packet.status.in_(
                [
                    PacketStatus.DRAFT,
                    PacketStatus.EVALUATING_PRE,
                    PacketStatus.EVALUATING,
                    PacketStatus.READY,
                ]
            ),
        )
        .order_by(Packet.created_at, Packet.ref)
    ).all()
    result: list[Packet] = []
    for packet in candidates:
        _, target = packet_document(session, packet)
        stale = packet.input_hash != target or PacketStatus(packet.status) is not PacketStatus.READY
        if stale:
            result.append(packet)
        if limit is not None and len(result) >= limit:
            break
    return result


def evaluate_pending(
    session: Session,
    provider: LLMProvider,
    *,
    limit: int | None = None,
    specs: dict[str, ModelSpec] | None = None,
    actor: Actor = EVALUATOR,
) -> list[EvaluationReport]:
    evaluator = Evaluator(session, provider, specs=specs, actor=actor)
    return [evaluator.evaluate(p) for p in pending_packets(session, limit)]
