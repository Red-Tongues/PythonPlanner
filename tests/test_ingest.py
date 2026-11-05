from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.contracts import (
    AutonomyMode,
    PacketStatus,
    Stage,
)
from orchestrator.db.models import (
    Confirmation,
    Event,
    Packet,
)
from orchestrator.errors import (
    IngestionError,
)
from orchestrator.ingest import ingest_file, parse_text, read_document
from orchestrator.ingest.parser import (
    DESCRIPTION_MAX,
    UNTITLED_TASK,
    clean_inline,
    make_title,
    name_from_path,
    split_text,
)
from orchestrator.repository import (
    set_autonomy_mode,
)
from tests.conftest import USER

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "sample_project_plan.md"
DRAFT = ROOT / "docs" / "archive" / "Python_Planner_v0_draft.md"


def _shape(tree) -> list[tuple[str, list[str]]]:  # type: ignore[no-untyped-def]
    return [(o.name, [t.title for t in o.tasks]) for o in tree.objectives]


# --------------------------------------------------------------------------- parser


def test_tidy_markdown() -> None:
    text = (
        "# Rocket\n\nGo to space.\n\n"
        "## Engines\nMake them work.\n- Build the nozzle\n  - use alloy\n- Test fire\n\n"
        "## Launch\n### Pick a date\nAvoid storms.\n- check weather\n### Countdown\n"
    )
    tree = parse_text(text, fallback_name="x")
    assert tree.project.name == "Rocket"
    assert tree.project.brief == "Go to space."
    assert _shape(tree) == [
        ("Engines", ["Build the nozzle", "Test fire"]),
        ("Launch", ["Pick a date", "Countdown"]),
    ]
    engines, launch = tree.objectives
    assert engines.brief == "Make them work."
    assert "use alloy" in engines.tasks[0].prompt_description
    assert "Avoid storms." in launch.tasks[0].prompt_description
    assert "- check weather" in launch.tasks[0].prompt_description
    assert launch.tasks[1].prompt_description == "Countdown"  # empty body → title


def test_no_headings_degrades_to_one_objective() -> None:
    tree = parse_text("1. buy milk\n2. call mum\n3) fix bike\n", fallback_name="Chores")
    assert tree.project.name == "Chores"
    assert _shape(tree) == [("General", ["buy milk", "call mum", "fix bike"])]


def test_paragraphs_only_become_tasks() -> None:
    text = "We want a garden.\n\nDig the beds this weekend.\n\nOrder seeds from the shop."
    tree = parse_text(text, fallback_name="Garden")
    assert tree.project.brief == "We want a garden."
    assert _shape(tree) == [
        ("General", ["Dig the beds this weekend.", "Order seeds from the shop."])
    ]


def test_single_paragraph_is_still_a_task() -> None:
    tree = parse_text("just do the thing", fallback_name="Thing")
    assert tree.task_count == 1


def test_bold_pseudo_headings_and_todos() -> None:
    text = "**Backend**\n- API\nTODO: pick a database\n\n**Frontend:**\n- pages\n"
    tree = parse_text(text, fallback_name="App")
    assert _shape(tree) == [("Backend", ["API", "TODO: pick a database"]), ("Frontend", ["pages"])]


def test_multiple_h1s_are_objectives_under_the_first() -> None:
    text = "# Plan\nintro\n# Phase one\n## Do A\n# Phase two\n## Do B\n### detail\nmore\n"
    tree = parse_text(text, fallback_name="x")
    assert tree.project.name == "Plan"
    assert _shape(tree) == [("Phase one", ["Do A"]), ("Phase two", ["Do B"])]
    assert "detail:" in tree.objectives[1].tasks[0].prompt_description


def test_objective_without_tasks_gets_one_task_from_its_text() -> None:
    tree = parse_text("# P\n## Ideas\nMaybe a leaderboard.\n## Empty\n", fallback_name="x")
    assert _shape(tree) == [("Ideas", ["Ideas"]), ("Empty", [])]
    assert tree.objectives[0].tasks[0].prompt_description == "Maybe a leaderboard."


def test_code_fences_are_not_headings() -> None:
    tree = parse_text(
        "# P\n## O\n### T\n```\n# not a heading\n- not an item\n```\n", fallback_name="x"
    )
    assert _shape(tree) == [("O", ["T"])]
    assert "# not a heading" in tree.objectives[0].tasks[0].prompt_description


def test_setext_headings() -> None:
    tree = parse_text("Proj\n====\n\nObj\n---\n- task one\n", fallback_name="x")
    assert tree.project.name == "Proj"
    assert _shape(tree) == [("Obj", ["task one"])]


def test_project_name_override_and_fallback() -> None:
    assert parse_text("# A\n- t", fallback_name="x", project_name="B").project.name == "B"
    assert name_from_path("my_cool-plan.md") == "My cool plan"


def test_empty_document_raises() -> None:
    with pytest.raises(IngestionError):
        parse_text("  \n\n", fallback_name="x")


def test_title_cleaning() -> None:
    assert clean_inline("**Bold** [link](http://x) `code` \\[esc\\]:") == "Bold link code [esc]"
    assert clean_inline("[]{#_Toc1 .anchor}Project level") == "Project level"
    assert clean_inline("[ ] write docs") == "write docs"
    long = "word " * 60
    assert len(make_title(long)) <= 120


def test_sample_document() -> None:
    tree = parse_text(read_document(SAMPLE), fallback_name="sample")
    assert tree.project.name.startswith("Pantry Pal")
    names = [o.name for o in tree.objectives]
    assert names == [
        "Inventory",
        "Expiry alerts",
        "Accounts & households",
        "Random ideas (not committed)",
        "Tech / infra",
    ]
    inventory = dict(_shape(tree))["Inventory"]
    assert len(inventory) == 4 and inventory[-1].startswith("TODO")
    assert "bulk add" in tree.objectives[0].tasks[1].prompt_description
    assert tree.task_count >= 12


def test_archived_draft_smoke() -> None:
    tree = parse_text(read_document(DRAFT), fallback_name="draft")
    assert tree.project.name.startswith("Python Planner")
    assert tree.task_count > 5
    assert len(tree.objectives) >= 3


def test_docx_document(tmp_path) -> None:  # type: ignore[no-untyped-def]
    doc = Document()
    doc.add_heading("Bakery site", level=0)  # Title style
    doc.add_paragraph("An online shop for a small bakery.")
    doc.add_heading("Catalogue", level=1)
    doc.add_paragraph("Show products", style="List Bullet")
    doc.add_paragraph("with photos", style="List Bullet 2")
    doc.add_paragraph("Allow pre-orders", style="List Number")
    doc.add_heading("Payments", level=1)
    doc.add_heading("Card checkout", level=2)
    doc.add_paragraph("Use a hosted payment page.")
    bold = doc.add_paragraph()
    bold.add_run("Notes").bold = True
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "fees"
    table.rows[0].cells[1].text = "1.5%"
    path = tmp_path / "plan.docx"
    doc.save(path)

    tree = parse_text(read_document(path), fallback_name="x")
    assert tree.project.name == "Bakery site"
    assert tree.project.brief == "An online shop for a small bakery."
    assert _shape(tree) == [
        ("Catalogue", ["Show products", "Allow pre-orders"]),
        ("Payments", ["Card checkout", "Notes"]),
    ]
    assert "with photos" in tree.objectives[0].tasks[0].prompt_description
    assert "fees | 1.5%" in tree.objectives[1].tasks[1].prompt_description


def test_unsupported_and_missing_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    with pytest.raises(IngestionError, match="unsupported"):
        read_document(pdf)
    with pytest.raises(IngestionError, match="no such file"):
        read_document(tmp_path / "missing.md")


def test_txt_is_supported(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "todo.txt"
    path.write_text("- one\n- two\n")
    assert parse_text(read_document(path), fallback_name="t").task_count == 2


# --------------------------------------------------------------------------- persistence


def test_ingest_persists_drafts_events_and_confirmation(session: Session) -> None:
    result = ingest_file(session, SAMPLE, actor=USER)
    session.commit()
    assert result.project.ref == "P-0001"
    assert result.project.source_sha256 and result.project.tree_contract.startswith("ProjectTree@")
    assert [o.ref for o in result.objectives][:2] == ["O-0001-01", "O-0001-02"]
    assert result.packets[0].ref == "PP-000001"
    assert all(p.status is PacketStatus.DRAFT for p in result.packets)
    assert all(p.source == "deterministic_parser" for p in result.packets)
    assert all(p.root_id == p.id and p.depth == 0 for p in result.packets)
    assert all(p.response_contract == "markdown_document@1.0" for p in result.packets)

    created = session.scalar(
        select(func.count()).select_from(Event).where(Event.action == "packet.created")
    )
    assert created == len(result.packets)
    conf = session.scalar(select(Confirmation))
    assert conf is not None and conf.stage is Stage.INGESTION and conf.entity_ref == "P-0001"
    assert conf.payload["contract"].startswith("ProjectTree@")

    second = ingest_file(session, SAMPLE, actor=USER)
    assert second.project.ref == "P-0002"
    assert second.objectives[0].ref == "O-0002-01"
    assert second.packets[0].ref == f"PP-{len(result.packets) + 1:06d}"


def test_auto_ingestion_scope_skips_confirmation(session: Session) -> None:
    set_autonomy_mode(session, Stage.INGESTION, AutonomyMode.AUTO, actor=USER)
    result = ingest_file(session, SAMPLE, actor=USER)
    assert result.confirmation is None
    assert session.scalar(select(func.count()).select_from(Confirmation)) == 0
    assert session.scalar(select(func.count()).select_from(Packet)) == len(result.packets)


def test_ingest_archived_draft_persists(session: Session) -> None:
    result = ingest_file(session, DRAFT, actor=USER)
    assert result.packets


# ------------------------------------------------------------------ graceful degradation (R1 fix 9)


def test_untitled_fallback() -> None:
    tree = parse_text("# P\n## O\n- [ ]\n- real task\n", fallback_name="x")
    titles = [t.title for t in tree.objectives[0].tasks]
    assert titles == [UNTITLED_TASK, "real task"]


def test_long_descriptions_are_split_not_lost(tmp_path, session: Session) -> None:  # type: ignore[no-untyped-def]
    paragraphs = [f"Paragraph {i}: " + "word " * 900 for i in range(12)]
    doc = tmp_path / "long.md"
    doc.write_text("# Big\n## Spec\n### Everything\n" + "\n\n".join(paragraphs) + "\n")
    result = ingest_file(session, doc, actor=USER)
    parts = [p for p in result.packets if p.title.startswith("Everything (part")]
    assert len(parts) >= 2
    assert all(len(p.prompt_description) <= DESCRIPTION_MAX for p in parts)
    joined = "\n\n".join(p.prompt_description.split("\n\n", 1)[1] for p in parts)
    assert all(para.strip() in joined for para in paragraphs)
    assert parts[0].title.endswith(f"(part 1/{len(parts)})")


def test_split_text_hard_cuts_only_inside_one_huge_line() -> None:
    chunks = split_text("a" * 50_000, budget=20_000)
    assert [len(c) for c in chunks] == [20_000, 20_000, 10_000]
