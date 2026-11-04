"""Command-line interface (Typer). The only layer that prints."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from orchestrator import __version__
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

CLI_ACTOR = "user:cli"


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
        f"{len(fields['disabled'])} disabled"
    )


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(help="Planning document (.md, .txt or .docx).")],
    project_name: Annotated[
        str | None, typer.Option("--project-name", help="Override the project name.")
    ] = None,
) -> None:
    """Parse a planning document into Project > Objective > Task (draft packets)."""
    from orchestrator.ingest import ingest_file

    with _session() as session:
        result = ingest_file(session, path, project_name=project_name, actor=CLI_ACTOR)
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
    """Mark a packet completed. This LOCKS it: it can never be edited again, only superseded."""
    from orchestrator.lifecycle import transition
    from orchestrator.repository import get_packet

    with _session() as session:
        packet = get_packet(session, ref)
        transition(packet, PacketStatus.COMPLETED, CLI_ACTOR, reason)
        typer.echo(f"{packet.ref} v{packet.version} completed and locked")


if __name__ == "__main__":  # pragma: no cover
    app()
