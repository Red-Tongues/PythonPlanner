# Progress log

Newest entries first. Every agent session appends an entry: what changed, decisions, deviations from spec (with reasons), review triage, known issues, next steps.

---

## 2026-11-03 — M1: kernel foundation, including R0 amendments A1–A7 (Claude Code)

Branch `claude/optimistic-allen-ts03d8`; `origin/main` merged in at 29cd714 (R0 amendments) partway through the session.

### What was built
- **Packaging**: `pyproject.toml` (hatchling), distribution `pythonplanner-orchestrator`, package `orchestrator` in `src/`, version `1.0.0.0.dev1`, Python ≥ 3.11, console script `orchestrator`. `orchestrator.config.Settings` (env prefix `ORCH_`).
- **Contracts** (`orchestrator.contracts`): the enums in the spec, plus `PacketKind`, `ThresholdOperator`, `ThresholdAction`, `RunStatus` and `ConfirmationStatus`. `ProjectCore` / `ObjectiveCore` / `TaskCore` / `ObjectiveNode` / `ProjectTree`, `CandidateFieldDefinition` with per-kind rules, and a response-structure registry (`markdown_document`, `json_object`, `subtask_list`, `project_tree`). An unknown name raises `UnknownResponseStructureError` and the message lists the valid names.
- **Field registry** (`orchestrator.fields`): `candidate_fields.yaml` holds all 35 draft fields (6 task tag sets, 8 task scores, 21 content scores). The loader validates them, and `init-db` syncs them to the DB.
- **Schema** (`orchestrator.db`): all 15 tables from §6, the A2/A3 columns, three lineage-head views, and every trigger, kept in one module (`ddl.py`).
- **Migrations (A4)**: Alembic, with its version table named `schema_version`. Three revisions: `0001` schema, `0002` triggers and views, `0003` seed defaults. `init-db` runs the migrations and then syncs the registry.
- **Immutability (F, A1)**: enforced by triggers on both dialects and by an ORM `before_flush` guard (`ImmutableRecordError`). Rows are corrected with `supersede_packet` / `supersede_objective` / `supersede_project`.
- **Lifecycle** (`orchestrator.lifecycle`): an explicit transition table. Each transition writes one event; an illegal one raises `InvalidTransitionError`.
- **Ingestion** (`orchestrator.ingest`): reads `.md` / `.txt` / `.docx` and parses them deterministically into a `ProjectTree`. Packets are stored as `draft` with `source = "deterministic_parser"`, every row gets an event, and a confirmation row is created when the ingestion scope is `confirm`. `examples/sample_project_plan.md` is a deliberately messy plan for "Pantry Pal"; it ingests as 5 objectives and 15 packets.
- **CLI**: `version`, `init-db`, `ingest`, `tree`, `fields`, `packet show`, `packet complete`, `events`, plus a global `--database-url`.
- **Tests and CI**: 113 test items. All pass on SQLite, and on PostgreSQL 16 when `ORCH_TEST_POSTGRES_URL` is set. Verified in this session on Python 3.11 and 3.12 and against a local PostgreSQL 16. `.github/workflows/ci.yml` runs ruff, pytest and a CLI smoke test on 3.11 and 3.12, plus a `postgres:16` job running `pytest -m postgres`.

### R0 amendments A1–A7: how each was met
- **A1** On `projects`, `objectives` and `packets`, a locked row rejects UPDATE and DELETE, so `locked` can never go back from true to false. On packets, INSERT and UPDATE triggers require `status ∈ {completed, archived}` ⇒ `locked` and `completed_at` set. Artifacts: once accepted they reject UPDATE/DELETE, and INSERT/UPDATE require `accepted ⇒ accepted_at`. Raw-SQL tests run on both dialects.
- **A2** `projects` and `objectives` gain `version`, `supersedes_id` (unique, so history is linear) and `locked`, with the same lock triggers. `candidate_field_definitions.version` is bumped by every registry sync change; `settings.version` is bumped by `set_setting`. `supersede_*` never edits the old row and never rewrites `parent_id` / `root_id` / `project_id` / `objective_id`. Views `v_packet_heads` (plus `v_project_heads` and `v_objective_heads`) follow `supersedes_id` with a recursive CTE written once in portable SQL.
- **A3** All the listed columns are added. Unique constraints: `(ref, version)` on the three lineage tables; `channel_runs.idempotency_key`; `evaluation_passes(eval_cycle_id, field_name, pass_no, attempt)`; `artifacts(packet_id, path, sha256)`; and a partial unique index allowing at most one *pending* confirmation per (stage, entity).
- **A4** Alembic with `version_table="schema_version"`. Tests check that an upgrade from an empty DB reaches head on both dialects, that rerunning it changes nothing, and that `compare_metadata` finds no drift between the models and the migrations.
- **A5** Every SQLite connection gets `foreign_keys=ON`, `journal_mode=WAL` and `busy_timeout=5000`, and a test covers it.
- **A6** The `db_url` fixture is parametrised over `sqlite` and `postgresql`. The PostgreSQL param is skipped without `ORCH_TEST_POSTGRES_URL` and also carries the `postgres` marker, so CI's `-m postgres` selects exactly those runs. Each PostgreSQL test gets a fresh `public` schema.
- **A7** All contracts derive from `Contract`, which sets `extra="forbid"` and `strict=True`, and expose `CONTRACT_VERSION` / `contract_id()`. The version is recorded in `packets.response_contract` (`markdown_document@1.0`), `projects.tree_contract`, `candidate_field_definitions.contract`, the confirmation payload, the `ingest.completed` event, and `channel_runs.contract` (by convention, from M2).

### Decisions (ambiguities resolved with the Key Idea filter)
1. **Refs name a lineage (K4, K5).** A new version keeps its ref (`PP-000001` v2), and `(ref, version)` is unique. A bare ref resolves to the head; `REF@N` addresses one version. This reads better than issuing a new ref per correction, and it makes "the packet" and "its history" the same name.
2. **Manual completion (K2, Requirement E).** The kernel must follow the transition table. A *user* actor (`user` / `user:*`) may also complete or archive any non-terminal packet, because `manual` autonomy means the user does the work. Without this, `packet complete` could never be used in M1, where no channels exist.
3. **Completing or archiving locks the packet in the same flush**, which is one UPDATE statement. The trigger also checks `completed_at` (spec §5), which goes slightly beyond A1.
4. **Seeding only inserts missing rows**, so the user's edits survive re-running `init-db`. The field registry is the exception: the YAML is the source of truth, so a sync updates changed fields (and bumps their version), disables fields that were removed, and records an event.
5. **Registry**: the draft's "Fields" field is named `domains` to avoid a clash with the word "field". Every tag vocabulary gets `other`, because every draft list ends in "etc". Key fields are the six used by the §8 routing and branching rules, plus `requirement_alignment` (marked "(m)" in the draft), `correctness` and `security_risk`.
6. **Threshold rows** have the shape field / operator / value / action / precedence. Rows with the same action combine with OR. `@key_fields` is a pseudo-field meaning "any key field" (used by the disputed rule). The fallback "otherwise → Local Generation" belongs to router logic in M3, not to a row.
7. **Parser rules**, documented in `ingest/parser.py`:
   - The shallowest heading after the project H1 is the objective level. This handles documents with several H1s, such as the archived draft.
   - Under a task heading, everything becomes its description. Fine-grained splitting is left to M2 branching (`task_parsability`), so no text is lost and the parser stays predictable.
   - `TODO` / `FIXME` / `ACTION` lines and checkbox items are tasks.
   - A bold line acts as a pseudo-heading.
   - With no headings at all, the tasks are the list items, or failing those the paragraphs.
   - An objective with text but no tasks gets one task containing that text.
8. **Ingestion** always stores packets as `draft`; the autonomy scope only decides whether a confirmation row is created. If the scope row is missing, the parser assumes `confirm`, the safe default.
9. **Strict contracts and enums (A7).** Enum-typed fields accept their string values, because the vocabulary is closed and YAML/JSON can only carry strings. Every other type is strict.
10. **`channel_runs.state`** is a free string defaulting to `pending`. The run state machine itself belongs to M3 (C1).
11. **Default log level is `WARNING`**, so CLI output stays readable; `ORCH_LOG_LEVEL=INFO` shows kernel activity.
12. **The ORM guard is registered once on a `GuardedSession` class.** Registering per `sessionmaker` proved unreliable: a guard was silently skipped when a garbage-collected factory's `id()` was reused. A test caught this.
13. **Extra DB guards** beyond the spec, all cheap and in `ddl.py`: fixed autonomy scopes (the objective-level `confirm`) cannot be changed or deleted, and PostgreSQL rejects `TRUNCATE` on the append-only tables.

### Deviations from the spec
- **§4 `init_db(engine)`** no longer calls `create_all`. Tables, triggers, views and seeds are created by migrations (A4 supersedes this part of the spec). `init_db` runs them and then syncs the registry.
- **§4 human-readable refs** are unique per `(ref, version)`, not on their own (A2, decision 1).
- **§4 settings seeds** cover the listed aggregation and attempt settings only. Priority weights (§8) have no agreed defaults yet; they belong to M2.
- **`orchestrator fields`** also works before `init-db`: it then reads the YAML seed, which helps readability (K4).
- `.markdown` files are accepted in addition to `.md`, `.txt` and `.docx`.

### Known issues
- Superseded (non-head) rows that are not locked are protected from edits by the application layer (`ImmutableRecordError`), not by a trigger. A DB-level rule would need a subquery trigger on every update; consider it in M2 once evaluation cycles (B1) bind to versions.
- Migrations `0002` and `0003` call the live `ddl` and `seed` modules. Any later change to triggers, views or seeds must go in a **new** revision that calls them again (`ddl.install` is idempotent). Never edit an applied revision.
- Refs are allocated with `max()+1`, which assumes a single writer. Two concurrent ingestions would collide on the unique constraint and fail loudly, without corrupting data. M3's atomic claims (C1) address concurrency.
- Events are ordered by `created_at` (microsecond resolution), so ties are possible in theory.
- There is no CLI to approve or reject the ingestion confirmation yet; that is M2's `confirmations` commands.
- Inside an objective, a bold pseudo-heading becomes a task (e.g. "Meal suggestions" in the sample plan). This is deterministic and documented, but a human might have meant it as an objective.
- SQLite returns naive datetimes, while PostgreSQL returns timezone-aware ones. All values written are UTC.
- Alembic downgrades exist but are not tested.

### Next steps
- PM review and Copilot triage of the M1 PR; merge.
- M2 (evaluation engine) builds on `eval_cycle_id`, the field-definition and setting versions, `@key_fields` thresholds and the `subtask_list` contract. Add the confirmation approve/reject CLI there.

---

## 2026-11-03 — M0: plan and repository seeded (PM)

- Converted the CFO's draft `Python Planner.docx` into `docs/PROJECT_PLAN.md` (plan v0.2): Key Idea decision filter, A–F traceability, glossary, architecture, data model, six-pass design, routing, channels, autonomy scopes, GUI, integrations, decisions, scope, milestones, risks, budget, open questions.
- Archived the original draft and flowchart in `docs/archive/` (read-only provenance).
- Wrote the agent guide `CLAUDE.md` and milestone specs M1–M3 in `docs/milestones/`.
- Findings that shaped the plan:
  - juggler.studio has no documented REST API (JS extension SDK only) → file-drop workbench adapter in v1, Juggler extension in v1.1. Core licence AGPL-3.0, SDK Apache-2.0.
  - SonarQube Community Build analyses a single branch, with no PR decoration or secrets detection → per-branch project keys; separate secrets scanner later.
  - The draft's six-pass numbering is internally inconsistent → resolved as 2 extractions + 4 combined evaluations (decision D3, pending CFO).
- **Next:** M1 kernel foundation with Claude Code.
