"""Deterministic heading-based parser: Markdown-ish text → validated ``ProjectTree``.

Rules (M1 spec §7, with the graceful-degradation choices recorded in docs/PROGRESS.md):

1. If the first heading is an H1 it names the project; otherwise ``--project-name`` or the
   file name does. Text before the first objective heading is the project brief.
2. The shallowest remaining heading level is the *objective* level (H2 in a tidy plan; H1 in a
   document with several H1s). One level deeper is the *task* level (H3 in a tidy plan).
3. Under an objective, every task heading is a task whose ``prompt_description`` is everything
   beneath it (text, lists, deeper headings). Top-level list items and ``TODO:`` lines directly
   under an objective are tasks too; their nested items and continuation lines are folded into
   the description. Unindented paragraphs between tasks belong to the objective brief.
   Fine-grained splitting is left to M2's branching (``task_parsability``), so nothing is lost.
4. Bold-only lines (``**Backend**``) are pseudo-headings: an objective outside any objective, a
   task inside one, or an objective if the document has no real objective headings.
5. Messy input never fails: no headings → one "General" objective whose tasks are the list items
   or, failing those, the paragraphs; an objective with text but no tasks gets one task carrying
   that text. Only an empty document raises ``IngestionError``.
6. Limits degrade gracefully (R1 fix 9): a title that cleans to nothing becomes "Untitled task";
   a description over ``DESCRIPTION_MAX`` is split at paragraph (then line, then hard) boundaries
   into "Title (part i/n)" tasks, each carrying a note, so no text is lost and unattended
   ingestion keeps going. Anything still failing contract validation raises ``IngestionError``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from orchestrator.contracts import ObjectiveNode, ProjectCore, ProjectTree, TaskCore
from orchestrator.errors import IngestionError

TITLE_MAX = 120
DESCRIPTION_MAX = 20_000  # TaskCore.prompt_description limit
PART_BUDGET = DESCRIPTION_MAX - 200  # room for the part note
GENERAL_OBJECTIVE = "General"
UNTITLED_TASK = "Untitled task"

_ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HRULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_PSEUDO = re.compile(r"^\s*(\*\*|__)(?P<text>[^*_].*?)\1\s*:?\s*$")
_ITEM = re.compile(r"^(?P<indent>\s*)(?:[-*+•▪◦]|\d{1,3}[.)]|[A-Za-z][.)])\s+(?P<text>.*)$")
_TODO = re.compile(r"^\s*(?:TODO|FIXME|ACTION|TASK)\b\s*[:\-–]?\s*(?P<text>.+)$", re.IGNORECASE)
_CHECKBOX = re.compile(r"^\[[ xX]\]\s*")


# --------------------------------------------------------------------------- tokens


@dataclass
class _Line:
    kind: str  # heading | item | text | blank
    text: str = ""
    level: int = 0  # heading level
    indent: int = 0
    pseudo: bool = False
    raw: str = ""


def _indent_width(prefix: str) -> int:
    return len(prefix.expandtabs(4))


def tokenize(text: str) -> list[_Line]:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[_Line] = []
    in_fence = False
    for raw in raw_lines:
        line = raw.rstrip()
        if _FENCE.match(line):
            in_fence = not in_fence
            lines.append(_Line("text", line, raw=line))
            continue
        if in_fence:
            lines.append(_Line("text", line, raw=line))
            continue
        if not line.strip():
            lines.append(_Line("blank", raw=line))
            continue
        # setext underline: turn the previous text line into a heading
        setext = _SETEXT.match(line)
        if setext and lines and lines[-1].kind == "text" and lines[-1].indent == 0:
            prev = lines[-1]
            lines[-1] = _Line("heading", prev.text, level=1 if line.strip()[0] == "=" else 2)
            continue
        if _HRULE.match(line):
            lines.append(_Line("blank", raw=line))
            continue
        if m := _ATX.match(line):
            if m.group(2).strip():
                lines.append(_Line("heading", m.group(2), level=len(m.group(1)), raw=line))
            continue
        if m := _PSEUDO.match(line):
            lines.append(_Line("heading", m.group("text"), pseudo=True, raw=line))
            continue
        if m := _TODO.match(line):
            lines.append(_Line("item", line.strip(), indent=0, raw=line))
            continue
        if m := _ITEM.match(line):
            lines.append(
                _Line("item", m.group("text"), indent=_indent_width(m.group("indent")), raw=line)
            )
            continue
        stripped = line.lstrip()
        indent = _indent_width(line[: len(line) - len(stripped)])
        lines.append(_Line("text", stripped, indent=indent, raw=line))
    return lines


# --------------------------------------------------------------------------- text cleaning


def clean_inline(text: str) -> str:
    """Strip Markdown/pandoc decoration from a heading or item for use as a name/title."""
    t = text
    t = re.sub(r"\[\]\{[^}]*\}", "", t)  # pandoc anchors []{#_Toc..}
    t = re.sub(r"\{[^}]*\}", "", t)  # attribute blocks {width=..}
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)  # images
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)  # links
    t = _CHECKBOX.sub("", t.strip())
    t = re.sub(r"\\([\[\]\\*_#`>-])", r"\1", t)  # escapes
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)", r"\1", t)
    t = t.replace("`", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t.rstrip(":").strip()


def make_title(text: str) -> str:
    title = clean_inline(text)
    if len(title) <= TITLE_MAX:
        return title
    sentence = re.split(r"(?<=[.!?])\s", title, maxsplit=1)[0]
    if len(sentence) <= TITLE_MAX:
        return sentence
    cut = title[: TITLE_MAX - 1].rsplit(" ", 1)[0]
    return cut + "…"


def _join(lines: list[str]) -> str:
    text = "\n".join(lines).strip("\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- tree building


@dataclass
class _Task:
    title: str
    body: list[str] = field(default_factory=list)
    from_heading: bool = False
    indent: int = 0


@dataclass
class _Objective:
    name: str
    brief: list[str] = field(default_factory=list)
    tasks: list[_Task] = field(default_factory=list)
    implicit: bool = False


class _Builder:
    def __init__(self) -> None:
        self.brief: list[str] = []
        self.objectives: list[_Objective] = []
        self.objective: _Objective | None = None
        self.task: _Task | None = None
        self.prev_blank = False

    def open_objective(self, name: str, implicit: bool = False) -> _Objective:
        self.objective = _Objective(name=name, implicit=implicit)
        self.objectives.append(self.objective)
        self.task = None
        return self.objective

    def ensure_objective(self) -> _Objective:
        return self.objective or self.open_objective(GENERAL_OBJECTIVE, implicit=True)

    def open_task(self, title: str, *, from_heading: bool, indent: int = 0, body: str = "") -> None:
        objective = self.ensure_objective()
        self.task = _Task(title=title, from_heading=from_heading, indent=indent)
        if body:
            self.task.body.append(body)
        objective.tasks.append(self.task)


def _levels(lines: list[_Line]) -> tuple[_Line | None, int | None]:
    headings = [ln for ln in lines if ln.kind == "heading" and not ln.pseudo]
    project = headings[0] if headings and headings[0].level == 1 else None
    rest = [h.level for h in headings if h is not project]
    return project, (min(rest) if rest else None)


def parse_text(text: str, *, fallback_name: str, project_name: str | None = None) -> ProjectTree:
    """Parse Markdown-ish text into a validated ``ProjectTree`` (see module docstring)."""
    lines = tokenize(text)
    if not any(ln.kind != "blank" and ln.text.strip() for ln in lines):
        raise IngestionError("document is empty")

    project_heading, objective_level = _levels(lines)
    has_real_objectives = objective_level is not None
    objective_level = objective_level or 2
    task_level = objective_level + 1
    b = _Builder()

    for line in lines:
        if line is project_heading:
            continue
        if line.kind == "blank":
            b.prev_blank = True
            # keep paragraph breaks wherever the text is going
            if b.task is not None and b.task.from_heading:
                b.task.body.append("")
            elif b.task is None:
                (b.objective.brief if b.objective else b.brief).append("")
            continue

        if line.kind == "heading":
            level = line.level
            if line.pseudo:
                level = task_level if (b.objective and has_real_objectives) else objective_level
            name = clean_inline(line.text)
            if not name:
                continue
            if level <= objective_level:
                b.open_objective(name)
            elif level == task_level or b.task is None:
                b.open_task(make_title(name), from_heading=True)
            else:  # deeper heading inside a task → part of its description
                b.task.body.append(f"{name}:")
            b.prev_blank = False
            continue

        if line.kind == "item":
            task = b.task
            if task is not None and (task.from_heading or line.indent > task.indent):
                rel = max(line.indent - (0 if task.from_heading else task.indent), 0)
                task.body.append(" " * rel + f"- {line.text.strip()}")
            else:
                # the item's full text opens the description; nested items follow it
                b.open_task(
                    make_title(line.text),
                    from_heading=False,
                    indent=line.indent,
                    body=line.text.strip(),
                )
            b.prev_blank = False
            continue

        # plain text
        task = b.task
        if task is not None and (
            task.from_heading or line.indent > task.indent or not b.prev_blank
        ):
            task.body.append(line.text)
        elif b.objective is not None:
            b.task = None
            b.objective.brief.append(line.text)
        else:
            b.brief.append(line.text)
        b.prev_blank = False

    try:
        return _finish(b, project_heading, fallback_name, project_name)
    except ValidationError as exc:  # pragma: no cover - defensive; rules above avoid it
        raise IngestionError(
            f"document could not be turned into a valid ProjectTree: {exc}"
        ) from exc


def _paragraphs(lines: list[str]) -> list[str]:
    paras, current = [], []
    for line in [*lines, ""]:
        if line.strip():
            current.append(line)
        elif current:
            paras.append(" ".join(s.strip() for s in current))
            current = []
    return paras


def split_text(text: str, budget: int = PART_BUDGET) -> list[str]:
    """Split ``text`` into chunks of at most ``budget`` characters, preferring paragraph, then line
    boundaries, and cutting hard only inside a single over-long line."""
    if len(text) <= budget:
        return [text]
    chunks: list[str] = []
    current = ""
    pieces = re.split(r"(\n\s*\n)", text)  # keep paragraph separators
    for piece in pieces:
        candidates = [piece] if len(piece) <= budget else re.split(r"(\n)", piece)
        for part in candidates:
            while len(part) > budget:  # a single line longer than the budget
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(part[:budget])
                part = part[budget:]
            if len(current) + len(part) > budget:
                chunks.append(current)
                current = ""
            current += part
    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def _tasks_for(title: str, description: str) -> list[TaskCore]:
    title = title or UNTITLED_TASK
    parts = split_text(description)
    if len(parts) == 1:
        return [TaskCore(title=title, prompt_description=parts[0])]
    n = len(parts)
    return [
        TaskCore(
            title=f"{title[: TITLE_MAX - 14]} (part {i}/{n})",
            prompt_description=(
                f"[Part {i} of {n} of one section that exceeded {DESCRIPTION_MAX} characters; "
                f"split at paragraph boundaries by the deterministic parser.]\n\n{part}"
            ),
        )
        for i, part in enumerate(parts, start=1)
    ]


def _finish(
    b: _Builder, project_heading: _Line | None, fallback_name: str, project_name: str | None
) -> ProjectTree:
    name = project_name or (clean_inline(project_heading.text) if project_heading else "")
    name = name or fallback_name

    if not b.objectives:
        # no headings, no list items: the paragraphs are the tasks
        paras = _paragraphs(b.brief)
        objective = b.open_objective(GENERAL_OBJECTIVE, implicit=True)
        brief_para, task_paras = (paras[0], paras[1:]) if len(paras) > 1 else ("", paras)
        b.brief = [brief_para] if brief_para else []
        for para in task_paras:
            objective.tasks.append(_Task(title=make_title(para), body=[para]))

    nodes: list[ObjectiveNode] = []
    for obj in b.objectives:
        tasks: list[TaskCore] = []
        for task in obj.tasks:
            description = _join(task.body) or task.title or UNTITLED_TASK
            tasks.extend(_tasks_for(task.title, description))
        brief = _join(obj.brief)
        if not tasks and brief:
            tasks.extend(_tasks_for(make_title(obj.name), brief))
        if obj.implicit and not brief:
            brief = "Tasks found outside any objective heading."
        nodes.append(ObjectiveNode(name=obj.name[:200], brief=brief, tasks=tasks))

    return ProjectTree(project=ProjectCore(name=name[:200], brief=_join(b.brief)), objectives=nodes)


def name_from_path(path: str | Path) -> str:
    """``sample_project-plan.md`` → ``Sample project plan``."""
    stem = re.sub(r"[\s_-]+", " ", Path(path).stem).strip()
    return (stem[:1].upper() + stem[1:]) if stem else "Untitled"
