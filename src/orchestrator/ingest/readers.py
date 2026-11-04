"""Document readers: ``.md``, ``.txt`` and ``.docx`` → Markdown-ish text for the parser.

``.docx`` is converted structurally: Title/Heading N styles → ``#`` headings, list paragraphs
(list styles or Word numbering) → ``-`` items with indentation, fully bold paragraphs →
``**pseudo-headings**``, tables → ``| cell | cell |`` lines. Body order is preserved.
"""

from __future__ import annotations

import re
from pathlib import Path

from orchestrator.errors import IngestionError

SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".docx"})
_HEADING_STYLE = re.compile(r"^Heading\s+(\d)$", re.IGNORECASE)
_LIST_STYLE_LEVEL = re.compile(r"\s(\d)$")


def read_document(path: str | Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise IngestionError(f"no such file: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise IngestionError(
            f"unsupported file type {suffix!r}; supported: {sorted(SUPPORTED_SUFFIXES)}"
        )
    if suffix == ".docx":
        return docx_to_markdown(path)
    return path.read_text(encoding="utf-8", errors="replace")


def docx_to_markdown(path: str | Path) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            lines.extend(_paragraph_lines(Paragraph(child, document)))
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines)


def _paragraph_lines(paragraph) -> list[str]:  # type: ignore[no-untyped-def]
    text = paragraph.text.strip()
    if not text:
        return [""]
    style = paragraph.style.name if paragraph.style is not None else ""
    if style == "Title":
        return [f"# {text}", ""]
    heading = _HEADING_STYLE.match(style)
    if heading:
        level = min(int(heading.group(1)), 6)
        return ["#" * level + f" {text}", ""]
    list_level = _list_level(paragraph, style)
    if list_level is not None:
        return ["  " * list_level + f"- {text}"]
    runs = [r for r in paragraph.runs if r.text.strip()]
    if runs and all(r.bold for r in runs):
        return [f"**{text}**", ""]
    return [text, ""]


def _list_level(paragraph, style: str) -> int | None:  # type: ignore[no-untyped-def]
    p_pr = paragraph._p.pPr
    if p_pr is not None and p_pr.numPr is not None:
        ilvl = p_pr.numPr.ilvl
        return int(ilvl.val) if ilvl is not None and ilvl.val is not None else 0
    if style.startswith("List"):
        match = _LIST_STYLE_LEVEL.search(style)
        return int(match.group(1)) - 1 if match else 0
    return None
