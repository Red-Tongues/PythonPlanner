# M1 — Kernel foundation

**Goal:** a clean, installable Python package with the governed data model at its core: Pydantic contracts, a seeded candidate-field registry, a SQLAlchemy schema that works on SQLite and PostgreSQL, database-enforced immutability, an evented packet lifecycle, deterministic ingestion of planning documents, a CLI, tests and CI.

Serves requirements **A** (parser), **E** (schema), **F** (immutability) · Key Idea K2, K3, K4, K5.

## Deliverables

### 1. Packaging and configuration
- `pyproject.toml` (hatchling), distribution `pythonplanner-orchestrator`, import package `orchestrator` in `src/`, version `1.0.0.0.dev1`, Python ≥ 3.11.
- Dependencies: `pydantic>=2`, `pydantic-settings`, `sqlalchemy>=2`, `typer`, `python-docx`, `pyyaml`. Extras: `postgres` → `psycopg[binary]>=3`; `dev` → `pytest`, `pytest-cov`, `ruff`.
- `orchestrator.config.Settings` (pydantic-settings, env prefix `ORCH_`): `database_url` (default `sqlite:///./orchestrator.db`), `production_repo_path` (default `./production_repo`), `workbench_path` (default `./workbench`), `log_level`.
- Console script `orchestrator`.

### 2. Domain contracts (`orchestrator.contracts`)
- Enums: `PacketStatus` (`draft`, `queued`, `evaluating`, `ready`, `in_progress`, `awaiting_confirmation`, `processed`, `completed`, `archived`, `failed`), `Channel` (`quickfire_evaluation`, `local_generation`, `workbench_production`), `AutonomyMode` (`manual`, `confirm`, `auto`), `Stage` (the nine stages in PROJECT_PLAN §11), `FieldKind` (`tagset`, `score`, `text`, `bool`), `FieldLevel` (`task`, `content`).
- Models: `ProjectCore`, `ObjectiveCore`, `TaskCore` and `ProjectTree` (project → objectives → tasks) used as the contract for any ingestion path; `CandidateFieldDefinition`.
- A `ResponseStructure` registry stub: a name → Pydantic model mapping with at least `markdown_document`, `json_object`, `subtask_list`, `project_tree`; unknown names raise a clear error.

### 3. Candidate-field registry (`orchestrator.fields`)
- Seed file `src/orchestrator/fields/candidate_fields.yaml` containing **every** field listed in the archived draft:
  - task-level tag sets with vocabularies: artifact request classes, representation formats, repository roles, fields (domains), styles, tools needed;
  - task-level scores 0–10: task parsability, context volume, context independence, web independence, coding intensity, reasoning intensity, output wordcount, ambiguity quotient;
  - content-level scores 0–10: testability, requirement alignment, correctness, internal consistency, functional completeness, repo consistency, maintainability, security risk, duplication quotient, technical debt, acceptance-criteria coverage, dependency validity, determinism, observability, reversibility, documentation adequacy, performance risk, portability, data integrity risk, privacy risk, licence and provenance risk.
- Each entry: `name` (snake_case), `label`, `level`, `kind`, `vocabulary` (tag sets), `scale` (`0-10` for scores), `max_chars` and `max_passes` (text: 3), `prompt_hint` (the draft's one-line description), `is_key_field`.
- Loader validates the YAML against `CandidateFieldDefinition` and syncs it into the DB table on `init-db`.

### 4. Database schema (`orchestrator.db`)
- SQLAlchemy 2.0 models for every table in PROJECT_PLAN §6: `projects`, `objectives`, `packets`, `candidate_field_definitions`, `evaluation_passes`, `thresholds`, `settings`, `channel_runs`, `artifacts`, `quality_reports`, `autonomy_scopes`, `confirmations`, `events`, `control_state`, `exemplars`. Columns per §6, plus `created_at`/`updated_at` where meaningful.
- Primary keys: UUID strings. Human-readable refs: projects `P-0001`, objectives `O-0001-01`, packets `PP-000001`.
- `packets` has `parent_id`, `root_id`, `version`, `supersedes_id`, `locked`, `branch_reason`, plus all master governance fields.
- `init_db(engine)` creates tables, then installs dialect-specific DDL from **one module** (`orchestrator.db.ddl`): triggers for SQLite and PL/pgSQL functions + triggers for PostgreSQL.
- Seed defaults on `init-db`: autonomy scopes (§11 defaults; objective-level LLM values fixed at `confirm`), settings (aggregation weights 0.6/0.4, dispute margin 0.15, max attempts QE 4 / LG 3 / WP 2), default threshold rules (§8). Idempotent.

### 5. Immutability (Requirement F) — must be enforced by the database
- `packets`: when `OLD.locked` is true → UPDATE and DELETE raise. Setting status to `completed` or `archived` sets `locked = true` and `completed_at` in the same statement (app) and is verified by a trigger.
- `artifacts`: once `accepted` is true → UPDATE and DELETE raise.
- `evaluation_passes`, `events`: UPDATE and DELETE always raise.
- Application guard: the repository layer raises `ImmutableRecordError` before touching a locked row.
- `supersede_packet(old_ref, changes)` creates a new version (`version + 1`, `supersedes_id`) and never edits the old one.

### 6. Packet lifecycle (`orchestrator.lifecycle`)
- Explicit allowed-transition table for `PacketStatus`; `transition(packet, to_status, actor, reason)` validates, applies and writes an `events` row. Illegal transitions raise `InvalidTransitionError`.

### 7. Deterministic ingestion (`orchestrator.ingest`)
- Read `.md`, `.txt` and `.docx`. Heading-based parsing: first H1 (or filename) → project name; text before the first H2 → project brief; H2 → objectives; H3 headings and list items under an objective → tasks (`TaskCore.title`, `prompt_description` from the following text). Messy input (no headings, numbered lists, bold pseudo-headings) must degrade gracefully into one objective with tasks rather than fail.
- Output: a validated `ProjectTree`; persist with packets in `draft` status and `source = "deterministic_parser"`; write events. If the `ingestion` autonomy scope is `confirm`, create a `confirmations` row for the tree.
- Add `examples/sample_project_plan.md`: a deliberately messy planning document for a small fictional product (mixed headings, bullets, TODOs, notes).
- Smoke test: ingesting `docs/archive/Python_Planner_v0_draft.md` must succeed and produce a non-empty tree.

### 8. CLI (`orchestrator.cli`, Typer)
`orchestrator version` · `init-db` · `ingest PATH [--project-name NAME]` · `tree [PROJECT_REF]` (indented Project > Objective > Task with refs and statuses) · `fields [--level task|content]` · `packet show REF` · `packet complete REF` (locks it) · `events [--limit N]`.

### 9. Tests and CI
- pytest with a fixture that builds a fresh SQLite DB per test; a `postgres` marker for tests that run only when `ORCH_TEST_POSTGRES_URL` is set.
- Required coverage: contract validation; registry loads all draft fields; triggers block UPDATE/DELETE on locked packets, accepted artifacts and append-only tables (SQLite always, PostgreSQL when available); `supersede_packet`; lifecycle transitions and events; ingestion of the sample doc, a `.docx` built in the test, and the archived draft; CLI smoke tests via `typer.testing.CliRunner`.
- GitHub Actions `.github/workflows/ci.yml`: ruff + pytest on Python 3.11 and 3.12 (SQLite); a second job with a `postgres:16` service running `pytest -m postgres`.

### 10. Docs
- `README.md`: what it is, quickstart (install, init-db, ingest, tree), link to the plan.
- Update `docs/PROGRESS.md` (what was built, decisions, deviations, known issues, next steps).

## Acceptance criteria
1. `pip install -e ".[dev]"` succeeds on a clean Python 3.11+ environment.
2. `pytest -q` passes; `ruff check .` is clean.
3. `orchestrator init-db && orchestrator ingest examples/sample_project_plan.md && orchestrator tree` prints a Project > Objective > Task tree.
4. Updating or deleting a completed packet fails **at the database level** on SQLite (test bypasses the app guard with raw SQL) and on PostgreSQL in CI.
5. Every lifecycle transition produces an `events` row; `events` rows cannot be updated or deleted.
6. All candidate fields from the draft are present in the registry.
7. `docs/PROGRESS.md` is updated and the PR lists each criterion with ✅/❌.
