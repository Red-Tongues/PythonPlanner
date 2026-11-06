from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from orchestrator import __version__
from orchestrator.cli import app

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_project_plan.md"
runner = CliRunner()


def cli(db_url: str, *args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["--database-url", db_url, *args])


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_uninitialised_database_is_explained(db_url: str) -> None:
    result = cli(db_url, "tree")
    assert result.exit_code == 1
    assert "init-db" in result.output


def test_end_to_end(db_url: str) -> None:
    result = cli(db_url, "init-db")
    assert result.exit_code == 0, result.output
    assert "40 inserted" in result.output
    assert "0 inserted" in cli(db_url, "init-db").output  # idempotent

    result = cli(db_url, "ingest", str(SAMPLE))
    assert result.exit_code == 0, result.output
    assert "P-0001" in result.output and "awaiting confirmation" in result.output

    result = cli(db_url, "tree")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("P-0001  Pantry Pal")
    assert lines[1].startswith("  O-0001-01  Inventory")
    assert lines[2].startswith("    PP-000001  [draft]  Barcode scanning")
    assert cli(db_url, "tree", "P-0001").output == result.output

    result = cli(db_url, "packet", "show", "PP-000001")
    assert result.exit_code == 0, result.output
    assert "Barcode scanning" in result.output and "draft" in result.output

    # M2.1 fix 5: completing needs evidence; closing without it is closed_unverified
    result = cli(db_url, "packet", "complete", "PP-000001")
    assert result.exit_code == 1 and "draft → completed is not in the packet table" in result.output
    result = cli(db_url, "packet", "close", "PP-000001", "--reason", "done by hand")
    assert result.exit_code == 0, result.output
    assert "closed (unverified) and locked" in result.output
    result = cli(db_url, "packet", "close", "PP-000001", "--reason", "again")
    assert result.exit_code == 1
    assert "locked" in result.output
    assert "[closed_unverified, locked]" in cli(db_url, "tree").output

    result = cli(db_url, "events", "--limit", "3")
    assert result.exit_code == 0
    assert "packet.transition" in result.output.splitlines()[0]

    result = cli(db_url, "packet", "show", "PP-999999")
    assert result.exit_code == 1 and "no packet" in result.output


def test_fields_listing(db_url: str) -> None:
    # works from the YAML seed before init-db, and from the DB after
    result = cli(db_url, "fields")
    assert result.exit_code == 0 and "40 fields" in result.output
    cli(db_url, "init-db")
    result = cli(db_url, "fields", "--level", "content")
    assert result.exit_code == 0
    assert "21 fields" in result.output and "licence_and_provenance_risk" in result.output
    assert "task_parsability" not in result.output
