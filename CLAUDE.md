# CLAUDE.md — PythonPlanner / Python Orchestrator

Guidance for any coding agent (Claude Code is the main engineer) working in this repository.

## What this is

The **Python Orchestrator** is the custom Python kernel of an autonomous project/software-development stack. It turns messy planning documents into a standardised database of `Project > Objective > Task (Prompt Packet)`, has small local LLMs score and tag every packet (six-pass "quickfire" evaluation), uses SQL to turn those evaluations into deterministic decisions (consensus, priority, routing, branching), and dispatches work to channels (local generation, the juggler.studio workbench), writing accepted output to a separate production git repo that SonarQube checks.

**Spec of record:** `docs/PROJECT_PLAN.md`. Milestone specs: `docs/milestones/`. Session log: `docs/PROGRESS.md`. The original CFO draft is archived (read-only) in `docs/archive/`.

## The Key Idea filter (apply to every change)

Build an autonomous project software dev stack with a custom Python kernel that organises and advances project work while the user is at their day job. Every change must serve at least one of:
K1 advances work unattended · K2 governed (recorded, scored, evidenced, gated by autonomy scopes) · K3 deterministic where possible (LLMs propose, SQL/Python decide) · K4 readable · K5 safe history (completed work immutable).

## Golden rules

1. **Completed work is immutable (Requirement F, reworded per R6 OQ26).** Developing packets and upstream context (projects, objectives) mutate in place, and every change of an audited field is captured by triggers into `field_revisions`. Completed runs seal their claim-time snapshot into `sealed_bundles` and spawn a child genPacket via `parent_id`/`root_id`; a sealed (locked) genPacket is never edited or re-opened. Never write code that updates or deletes locked packets, accepted artifacts, sealed bundles, field revisions or any row in append-only tables (`evaluation_passes`, `events`, …). The DB triggers that enforce this must never be disabled, including in tests.
2. **LLMs propose, SQL/Python decide.** No decision may depend on LLM output that has not passed Pydantic validation and SQL aggregation.
3. **Stateless LLM calls.** Every prompt is self-contained; no chat history between calls.
4. **Tests need no network, GPU or real LLM.** Use `FakeProvider`. PostgreSQL tests are opt-in via `ORCH_TEST_POSTGRES_URL` (CI provides it).
5. **Every state change writes an event** (actor, action, entity, before/after summary, reason).
6. **Readable by default.** Anything the kernel does must be visible in the DB, the CLI and (from M3) the GUI.
7. **Portable SQL, one master schema.** Views and triggers must exist for both SQLite and PostgreSQL. The schema is defined once: tables in `db/models.py`, triggers in `db/ddl.py` (all dialect-specific DDL), views in `sql/views.py`, assembled by `db/schema.py`. The single baseline migration is generated from it (`python scripts/generate_baseline.py`); `tests/test_schema.py` fails if they differ. While there is no live data, a schema change edits the master and regenerates the baseline (never hand-edit it, never add patch-on-patch revisions); once a database must be kept, freeze the baseline and add new revisions.

## Commands

```bash
pip install -e ".[dev]"          # add ,postgres for psycopg
pytest -q                        # full test suite (SQLite)
ORCH_TEST_POSTGRES_URL=postgresql+psycopg://... pytest -q -m postgres
ruff check . && ruff format --check .
python scripts/generate_baseline.py   # after any change to models.py, ddl.py or sql/views.py
orchestrator --help
```

## Workflow

- Implement milestone specs **in order** and only the milestone you were asked for.
- Work on a feature branch and open a PR into `main`. In the PR description, list every acceptance criterion with ✅ / ❌ and any deviation from the spec with its reason.
- At the end of every session update `docs/PROGRESS.md`: what changed, decisions, deviations, known issues, next steps.
- Keep commits focused, with imperative messages.

## Style

- Type hints everywhere; Pydantic v2; SQLAlchemy 2.0 style (`Mapped[...]`, `select()`); `pydantic-settings` for config.
- Standard-library `logging`; no `print` outside the CLI layer.
- Ruff, line length 100. Small modules with clear names; avoid global state except settings.
- Version: `1.0.0.0.devN` until the v1.0.0.0 acceptance criteria are met.
