"""Command-line interface (Typer). The only layer that prints."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from orchestrator import __version__
from orchestrator.actors import Actor
from orchestrator.config import configure_logging, get_settings
from orchestrator.contracts import FieldLevel, PacketStatus
from orchestrator.db import init_db, make_engine, make_session_factory, session_scope
from orchestrator.db.ddl import is_immutability_violation
from orchestrator.db.migrate import current_revision, head_revision
from orchestrator.db.models import CandidateFieldDefinitionRow, Packet
from orchestrator.errors import OrchestratorError
from orchestrator.events import recent_events
from orchestrator.fields import load_candidate_fields

app = typer.Typer(
    help="Python Orchestrator — governed kernel of an autonomous project/software-dev stack.",
    no_args_is_help=True,
    add_completion=False,
)
packet_app = typer.Typer(help="Inspect and manage individual Prompt Packets.", no_args_is_help=True)
app.add_typer(packet_app, name="packet")
branch_app = typer.Typer(help="Task branching (split rules).", no_args_is_help=True)
app.add_typer(branch_app, name="branch")
confirm_app = typer.Typer(help="The approval queue.", no_args_is_help=True)
app.add_typer(confirm_app, name="confirmations")

#: The CLI is a trusted boundary: a person at a terminal (R1 fix 11).
CLI_ACTOR = Actor.user("cli")


class _State:
    database_url: str | None = None


_state = _State()


@app.callback()
def main(
    database_url: Annotated[
        str | None,
        typer.Option("--database-url", help="Override ORCH_DATABASE_URL for this command."),
    ] = None,
) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    _state.database_url = database_url or settings.database_url


def _engine() -> Engine:
    return make_engine(_state.database_url or get_settings().database_url)


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@contextmanager
def _session(require_schema: bool = True) -> Iterator[Session]:
    engine = _engine()
    try:
        if require_schema and current_revision(engine) != head_revision():
            _fail("database is not initialised or not up to date — run `orchestrator init-db`")
        with session_scope(make_session_factory(engine)) as session:
            yield session
    except OrchestratorError as exc:
        _fail(str(exc))
    except DBAPIError as exc:
        if is_immutability_violation(exc):
            _fail(f"rejected by the database (immutable record): {exc.orig}")
        raise
    finally:
        engine.dispose()


def _status_label(packet: Packet) -> str:
    status = PacketStatus(packet.status).value
    return f"{status}, locked" if packet.locked else status


def _version_label(row: Any) -> str:
    return f" (v{row.version})" if row.version > 1 else ""


# --------------------------------------------------------------------------- commands


@app.command()
def version() -> None:
    """Print the orchestrator version."""
    typer.echo(f"orchestrator {__version__}")


@app.command("init-db")
def init_db_command() -> None:
    """Create/upgrade the schema, install immutability triggers and seed defaults."""
    engine = _engine()
    try:
        summary = init_db(engine)
    finally:
        engine.dispose()
    fields = summary["fields"]
    before = summary["revision_before"] or "empty"
    typer.echo(f"database ready at schema revision {summary['revision']} (was {before})")
    typer.echo(
        "candidate fields: "
        f"{len(fields['inserted'])} inserted, {len(fields['updated'])} updated, "
        f"{len(fields['disabled'])} disabled; registry revision {summary['registry_revision']}"
    )


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(help="Planning document (.md, .txt or .docx).")],
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Override the project name.")
    ] = None,
    llm: Annotated[
        bool, typer.Option("--llm", help="Extract the tree with the LLM instead of the parser.")
    ] = False,
) -> None:
    """Parse a planning document into Project > Objective > Task (draft packets)."""
    from orchestrator.ingest import ingest_file

    with _session() as session:
        result = ingest_file(
            session,
            path,
            project_name=project_name,
            actor=CLI_ACTOR,
            provider=_provider() if llm else None,
        )
        typer.echo(
            f"created {result.project.ref} '{result.project.name}': "
            f"{len(result.objectives)} objectives, {len(result.packets)} draft packets"
        )
        if result.confirmation is not None:
            typer.echo(
                f"awaiting confirmation {result.confirmation.id} (ingestion scope is 'confirm')"
            )


@app.command()
def tree(
    project_ref: Annotated[str | None, typer.Argument(help="Project ref, e.g. P-0001.")] = None,
) -> None:
    """Show Project > Objective > Task with refs and statuses."""
    from orchestrator.repository import get_project, list_projects, objectives_of, packets_of

    with _session() as session:
        projects = [get_project(session, project_ref)] if project_ref else list_projects(session)
        if not projects:
            typer.echo(
                "no projects yet — try `orchestrator ingest examples/sample_project_plan.md`"
            )
        for project in projects:
            typer.secho(f"{project.ref}  {project.name}{_version_label(project)}", bold=True)
            for objective in objectives_of(session, project):
                typer.echo(f"  {objective.ref}  {objective.name}{_version_label(objective)}")
                for packet in packets_of(session, objective):
                    typer.echo(
                        f"    {packet.ref}  [{_status_label(packet)}]  "
                        f"{packet.title}{_version_label(packet)}"
                    )


@app.command()
def fields(
    level: Annotated[
        FieldLevel | None, typer.Option("--level", help="Only task- or content-level fields.")
    ] = None,
) -> None:
    """List the candidate-field registry (from the database, or the YAML seed if not init'd)."""
    engine = _engine()
    try:
        initialised = current_revision(engine) is not None
    finally:
        engine.dispose()
    rows: list[tuple[str, str, str, bool, str]] = []
    if initialised:
        from sqlalchemy import select

        with _session() as session:
            for r in session.scalars(
                select(CandidateFieldDefinitionRow)
                .where(CandidateFieldDefinitionRow.enabled.is_(True))
                .order_by(CandidateFieldDefinitionRow.level, CandidateFieldDefinitionRow.name)
            ):
                detail = r.scale or ", ".join(r.vocabulary or []) or f"max {r.max_chars} chars"
                rows.append((r.name, str(r.level), str(r.kind), r.is_key_field, detail))
    else:
        for d in load_candidate_fields():
            detail = d.scale or ", ".join(d.vocabulary or []) or f"max {d.max_chars} chars"
            rows.append((d.name, d.level.value, d.kind.value, d.is_key_field, detail))
    if level is not None:
        rows = [r for r in rows if r[1] == level.value]
    width = max((len(r[0]) for r in rows), default=10)
    for name, lvl, kind, key, detail in rows:
        mark = "*" if key else " "
        typer.echo(f"{mark} {name:<{width}}  {lvl:<7}  {kind:<6}  {detail}")
    typer.echo(f"{len(rows)} fields (* = key field)")


@app.command()
def events(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help="How many events.")] = 20,
) -> None:
    """Show the most recent audit events (newest first)."""
    with _session() as session:
        for event in recent_events(session, limit=limit):
            when = event.created_at.strftime("%Y-%m-%d %H:%M:%S")
            entity = event.entity_ref or event.entity_type
            reason = f"  — {event.reason}" if event.reason else ""
            typer.echo(f"{when}  {event.actor:<16} {event.action:<22} {entity}{reason}")


# --------------------------------------------------------------------------- packet commands


@packet_app.command("show")
def packet_show(
    ref: Annotated[str, typer.Argument(help="Packet ref, e.g. PP-000001[@N].")],
) -> None:
    """Show a packet's core and governance fields, versions and recent events."""
    from orchestrator.repository import get_packet, versions_of

    with _session() as session:
        p = get_packet(session, ref)
        objective = p.objective.ref if p.objective else "-"
        lines: list[tuple[str, Any]] = [
            ("ref", f"{p.ref} v{p.version}"),
            ("title", p.title),
            ("status", _status_label(p)),
            ("project / objective", f"{p.project.ref} / {objective}"),
            ("kind", p.packet_kind),
            ("response structure", p.response_contract),
            ("channel", p.channel or "-"),
            ("priority level", p.priority_level if p.priority_level is not None else "-"),
            ("evaluations", p.evaluations_count),
            ("repo target path", p.repo_target_path or "-"),
            ("depth / branch reason", f"{p.depth} / {p.branch_reason or '-'}"),
            ("source", p.source),
            ("created", p.created_at),
            ("completed", p.completed_at or "-"),
        ]
        for label, value in lines:
            typer.echo(f"{label:>22}: {value}")
        typer.echo(f"{'prompt description':>22}:")
        for line in p.prompt_description.splitlines():
            typer.echo(f"    {line}")
        versions = versions_of(session, Packet, p.ref)
        if len(versions) > 1:
            typer.echo("versions: " + ", ".join(f"v{v.version}" for v in versions))
        typer.echo("recent events:")
        for version in versions:
            for event in reversed(recent_events(session, limit=10, entity_id=version.id)):
                typer.echo(
                    f"    {event.created_at:%Y-%m-%d %H:%M:%S}  {event.actor}  "
                    f"{event.action}  v{event.entity_version or '?'}"
                )


@packet_app.command("complete")
def packet_complete(
    ref: Annotated[str, typer.Argument(help="Packet ref, e.g. PP-000001.")],
    reason: Annotated[
        str, typer.Option("--reason", help="Why the packet is complete.")
    ] = "completed manually via CLI",
) -> None:
    """Mark a packet completed. Only allowed when the completion gate passes (post-evaluation of
    its newest artifact, or all children completed); otherwise use `packet close`. LOCKS it."""
    from orchestrator.lifecycle import transition
    from orchestrator.repository import get_packet

    with _session() as session:
        packet = get_packet(session, ref)
        transition(packet, PacketStatus.COMPLETED, CLI_ACTOR, reason)
        typer.echo(f"{packet.ref} v{packet.version} completed and locked")


@packet_app.command("close")
def packet_close(
    ref: Annotated[str, typer.Argument(help="Packet ref, e.g. PP-000001.")],
    reason: Annotated[str, typer.Option("--reason", help="Why it is closed (required).")],
) -> None:
    """Close a packet WITHOUT a passing evaluation (closed_unverified). LOCKS it; never counts
    as completed."""
    from orchestrator.lifecycle import transition
    from orchestrator.repository import get_packet

    with _session() as session:
        packet = get_packet(session, ref)
        transition(packet, PacketStatus.CLOSED_UNVERIFIED, CLI_ACTOR, reason)
        typer.echo(f"{packet.ref} v{packet.version} closed (unverified) and locked")


# --------------------------------------------------------------------------- M2 commands


def _provider():  # type: ignore[no-untyped-def]
    from orchestrator.llm import make_provider

    return make_provider(get_settings())


def _fmt_conf(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


@app.command()
def evaluate(
    packet: Annotated[
        str | None, typer.Option("--packet", help="Evaluate one packet (REF).")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="At most N pending packets.")
    ] = None,
) -> None:
    """Run the six-pass quickfire evaluation (FakeProvider unless a model endpoint is set)."""
    from orchestrator.evaluation import evaluate_packet, evaluate_pending
    from orchestrator.repository import get_packet

    provider = _provider()
    with _session() as session:
        if packet:
            reports = [evaluate_packet(session, get_packet(session, packet), provider)]
        else:
            reports = evaluate_pending(session, provider, limit=limit)
        if not reports:
            typer.echo("nothing to evaluate — every packet has a current evaluation set")
        for r in reports:
            states = list(r.fields.values())
            summary = ", ".join(f"{states.count(s)} {s}" for s in sorted(set(states)))
            extra = f"; reused {len(r.reused)}" if r.reused else ""
            extra += f"; escalated {', '.join(r.escalated)}" if r.escalated else ""
            stale = " (stale: the packet changed)" if r.stale else ""
            typer.echo(
                f"{r.packet_ref} v{r.packet_version} → {r.status or '-'}: {summary}{extra} "
                f"[{r.calls} calls]{stale}"
            )


@app.command()
def consensus(
    ref: Annotated[str, typer.Argument(help="Packet ref, e.g. PP-000001.")],
    evidence: Annotated[
        bool, typer.Option("--evidence/--no-evidence", help="Show verified quotes.")
    ] = True,
) -> None:
    """Show each field's final value, confidence, status and evidence (v_field_consensus)."""
    from sqlalchemy import text

    from orchestrator.repository import get_packet

    with _session() as session:
        packet = get_packet(session, ref)
        rows = (
            session.execute(
                text(
                    "SELECT * FROM v_field_consensus WHERE packet_id = :id "
                    "ORDER BY phase DESC, field_name, item_key"
                ),
                {"id": packet.id},
            )
            .mappings()
            .all()
        )
        typer.secho(f"{packet.ref} v{packet.version}  [{_status_label(packet)}]", bold=True)
        if not rows:
            typer.echo("no evaluation yet — run `orchestrator evaluate --packet " + ref + "`")
        shown_tags: dict[str, list[str]] = {}
        for r in rows:
            if r["kind"] == "tagset" and r["status"] == "null":
                continue  # tags neither model chose
            name = r["field_name"] + (f"[{r['item_key']}]" if r["item_key"] else "")
            value = r["final_value"] if r["final_value"] is not None else "-"
            typer.echo(
                f"  {r['phase']:<4} {name:<46} {r['status']:<9} conf {_fmt_conf(r['confidence'])}"
                f"  value {value}  (S{r['supports']}/C{r['contradictions']}"
                f"{'/U' + str(r['unverified']) if r['unverified'] else ''})"
            )
            if evidence and r["eva_packet_id"] not in shown_tags.setdefault(r["field_name"], []):
                shown_tags[r["field_name"]].append(r["eva_packet_id"])
                quotes = session.execute(
                    text(
                        "SELECT pass_no, evidence FROM evaluation_passes "
                        "WHERE eva_packet_id = :e AND pass_no >= 3 ORDER BY pass_no"
                    ),
                    {"e": r["eva_packet_id"]},
                ).all()
                seen: set[str] = set()
                for _pass_no, ev in quotes:
                    for item in (json.loads(ev) if isinstance(ev, str) else ev) or []:
                        if item.get("verified") and item["quote"] not in seen:
                            seen.add(item["quote"])
                            typer.echo(f'        {item["tag"]}: "{item["quote"]}"')


@app.command()
def stats(
    ref: Annotated[
        str | None, typer.Argument(help="Packet ref (default: all head packets).")
    ] = None,
) -> None:
    """Packet statistics (v_packet_stats) and federated branch statistics (v_branch_stats)."""
    from sqlalchemy import text

    with _session() as session:
        sql = (
            "SELECT s.* FROM v_packet_stats s WHERE NOT EXISTS "
            "(SELECT 1 FROM packets n WHERE n.supersedes_id = s.packet_id)"
        )
        params: dict[str, Any] = {}
        if ref:
            sql += " AND s.packet_ref = :ref"
            params["ref"] = ref.split("@")[0]
        typer.echo(
            f"{'packet':<12} {'words':>5} {'evals':>5} {'calls':>5} {'eval ms':>8} "
            f"{'hours':>7} {'>=thr':>5} {'evid':>5}"
        )
        for r in session.execute(text(sql + " ORDER BY s.packet_ref"), params).mappings():
            typer.echo(
                f"{r['packet_ref']:<12} {r['prompt_word_count']:>5} {r['evaluation_runs']:>5} "
                f"{r['evaluation_attempts']:>5} {r['evaluation_ms']:>8} "
                f"{float(r['lifecycle_hours']):>7.2f} {r['scores_above_threshold']:>5} "
                f"{r['evidence_items']:>5}"
            )
        branches = session.execute(text("SELECT * FROM v_branch_stats ORDER BY root_ref"))
        for b in branches.mappings():
            variance = b["score_variance"]
            typer.echo(
                f"branch {b['root_ref']}: {b['descendant_count']} descendants, "
                f"{b['revision_count']} revisions, {b['failed_runs']} failed runs, "
                f"{b['contradiction_count']} contradictions, "
                f"score variance {_fmt_conf(variance)}, {b['distinct_models']} models"
            )


@app.command()
def queue(
    channel: Annotated[
        str | None, typer.Option("--channel", help="Only packets routed to this channel.")
    ] = None,
    all_rows: Annotated[
        bool, typer.Option("--all", help="Include ineligible packets with the reason.")
    ] = False,
) -> None:
    """genQueue (R6): position, score and floor (v_gen_queue)."""
    from orchestrator.queue import gen_queue_rows

    with _session() as session:
        rows = gen_queue_rows(session)
        if channel:
            wanted = {p.id for p in session.query(Packet).filter(Packet.channel == channel)}
            rows = [r for r in rows if r.packet_id in wanted]
        if not rows:
            typer.echo("the queue is empty — evaluate packets first (`orchestrator evaluate`)")
        for r in rows:
            typer.echo(
                f"{r.gen_position:>3}  {r.packet_ref}  {r.status:<7} score {r.score_e4:>6} "
                f"floor {r.floor_e4}  wait {r.wait_sec}s  "
                f"(grade {r.parent_grade_e4}, centrality {r.centrality_e4}, "
                f"questioned -{r.question_score_e4}, feedback {r.feedback_e4:+d}, "
                f"ageing +{r.ageing_e4})"
            )


@branch_app.command("apply")
def branch_apply() -> None:
    """Apply fired split rules (respects the branching autonomy scope and B5 limits)."""
    from orchestrator.branching import apply_branch_triggers, rollup_trunks

    with _session() as session:
        outcomes = apply_branch_triggers(session, _provider(), actor=CLI_ACTOR)
        if not outcomes:
            typer.echo("no split rule fires on any packet")
        for o in outcomes:
            detail = ", ".join(o.children) if o.children else o.reason
            typer.echo(f"{o.packet_ref}  {o.rule}: {o.action}  {detail}")
        for ref in rollup_trunks(session, actor=CLI_ACTOR):
            typer.echo(f"{ref}: all children completed → trunk completed")


@confirm_app.command("list")
def confirmations_list(
    all_rows: Annotated[bool, typer.Option("--all", help="Include decided ones.")] = False,
) -> None:
    """Pending approvals (oldest first)."""
    from orchestrator.confirmations import list_confirmations

    with _session() as session:
        rows = list_confirmations(session, pending_only=not all_rows)
        if not rows:
            typer.echo("nothing waiting for you")
        for c in rows:
            typer.echo(f"{c.id[:8]}  {c.stage.value:<20} {c.status.value:<8} {c.summary}")


@confirm_app.command("approve")
def confirmations_approve(
    confirmation_id: Annotated[str, typer.Argument(help="Id (or unique prefix).")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Approve and carry out a confirmation."""
    from orchestrator.confirmations import approve

    with _session() as session:
        outcome = approve(
            session, confirmation_id, actor=CLI_ACTOR, provider=_provider(), reason=reason
        )
        typer.echo(f"approved: {outcome or 'recorded'}")


@confirm_app.command("reject")
def confirmations_reject(
    confirmation_id: Annotated[str, typer.Argument(help="Id (or unique prefix).")],
    reason: Annotated[str, typer.Option("--reason")] = "rejected via CLI",
) -> None:
    """Reject a confirmation."""
    from orchestrator.confirmations import reject

    with _session() as session:
        reject(session, confirmation_id, actor=CLI_ACTOR, reason=reason)
        typer.echo("rejected")


@app.command()
def waive(
    ref: Annotated[str, typer.Argument(help="Packet ref.")],
    field: Annotated[str, typer.Argument(help="Soft candidate field to accept.")],
    reason: Annotated[str, typer.Option("--reason", help="Why (required).")],
) -> None:
    """Accept a disputed/low-confidence SOFT field for this packet version only (B9)."""
    from orchestrator.governance import grant_waiver

    with _session() as session:
        w = grant_waiver(session, ref, field, actor=CLI_ACTOR, reason=reason)
        typer.echo(f"waiver {w.id[:8]}: {field} on {w.packet_ref} v{w.target_version}")


@app.command()
def override(
    ref: Annotated[str, typer.Argument(help="Packet ref.")],
    kind: Annotated[str, typer.Argument(help="block | hold | force | pin | reorder")],
    reason: Annotated[str, typer.Option("--reason", help="Why (required).")],
    scope: Annotated[str, typer.Option("--scope", help="version | lineage")] = "version",
    value: Annotated[float | None, typer.Option("--value", help="REORDER delta")] = None,
) -> None:
    """Change dispatch order or stop a packet. FORCE is priority only, never a waiver."""
    from orchestrator.governance import add_override

    with _session() as session:
        o = add_override(
            session, ref, kind, actor=CLI_ACTOR, reason=reason, scope=scope, value=value
        )
        typer.echo(f"override {o.id[:8]}: {o.kind.value} on {o.packet_ref} ({o.scope.value})")


@app.command()
def revoke(
    target_type: Annotated[str, typer.Argument(help="waiver | override")],
    target_id: Annotated[str, typer.Argument(help="Its id.")],
    reason: Annotated[str, typer.Option("--reason")] = "revoked via CLI",
) -> None:
    """Revoke a waiver or override (recorded; the original is never edited)."""
    from orchestrator.governance import revoke as do_revoke

    with _session() as session:
        do_revoke(session, target_type, target_id, actor=CLI_ACTOR, reason=reason)
        typer.echo("revoked")


@app.command()
def status() -> None:
    """Schema revision, evaluator models and their pinning, and packet counts."""
    from sqlalchemy import func, select

    from orchestrator.db.models import Event
    from orchestrator.llm import model_specs

    settings = get_settings()
    with _session() as session:
        typer.echo(f"schema revision {current_revision(session.get_bind())}")
        provider = _provider()
        typer.echo(f"provider: {provider.name}")
        for role, spec in model_specs(settings).items():
            typer.echo(f"  model {role}: {spec.name}  digest {spec.digest or '-'}")
        last = session.scalar(
            select(Event)
            .where(
                Event.action.in_(
                    [
                        "evaluation.model_pinning_failed",
                        "evaluation.models_verified",
                        "evaluation.fake_provider",
                    ]
                )
            )
            .order_by(Event.created_at.desc())
            .limit(1)
        )
        if last is not None:
            mark = {
                "evaluation.models_verified": "OK (digests verified)",
                "evaluation.fake_provider": "not applicable (FakeProvider)",
            }.get(last.action, "REFUSED")
            detail = (last.after or {}).get("error", "")
            typer.echo(f"model pinning: {mark} at {last.created_at:%Y-%m-%d %H:%M} {detail}")
        counts = session.execute(
            select(Packet.status, func.count()).group_by(Packet.status).order_by(Packet.status)
        ).all()
        typer.echo("packets: " + ", ".join(f"{PacketStatus(s).value} {n}" for s, n in counts))


if __name__ == "__main__":  # pragma: no cover
    app()
