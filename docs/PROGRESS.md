# Progress log

Newest entries first. Every agent session appends an entry: what changed, decisions, deviations from spec (with reasons), review triage, known issues, next steps.

---

## 2026-11-04 — M2: evaluation engine, genPackets and evaPackets (Claude Code)

Branch `claude/clever-keller-jogsc2` from `main` at 3068e35, with `main` fast-forwarded to 5666780 (B12) during the session. All of M2 is built, including amendments B1–B12, the D1–D5 records and the R1 deferrals GH6 and GE6. `docs/PROJECT_PLAN.md` was not touched.

**Suite:** 350 passed and 3 skipped on SQLite plus PostgreSQL 16 locally (Python 3.13); ruff check and format are clean. CI adds the M2 commands to the CLI smoke step.

### What was built
| Area | Where |
|---|---|
| Migration **0005** (frozen): `evaluation_cycles`, `eva_presets`, `eva_packets`, `evaluation_pass_items`, `queue_overrides`, `waivers`, `revocations`, `packet_dependencies`, `generation_runs`, `queue_admissions`, `branch_applications`. Also: packet `tags`/`scores`/`input_hash`, the B9 statuses, `thresholds.version`, `evaluation_passes.eva_packet_id` and FKs, append-only and snapshot triggers, the `content_eval` ban, M2 settings and presets, and all views | `db/migrations/versions/0005_eva_packets.py`; manifest regenerated through the documented flow |
| SQL layer (authoring source, frozen into 0005) | `sql/views.py`; `scripts/freeze_views.py`; `tests/test_sql_views.py` keeps the two identical |
| Providers: protocol, FakeProvider (rule-based, scripted, fail-next-N, noisy), OpenAI-compatible, Ollama with digest pinning | `llm/` |
| Evaluator: cycles, tiered lazy gating, content-addressed reuse, A-then-B batching, repairs, B12 escalation, stale-write guard | `evaluation/evaluator.py`, `plan.py` + `pass_plans.yaml`, `prompts.py`, `contracts.py`, `normalise.py` |
| Consensus oracle (pure) and readiness predicate (pure, authoritative) | `evaluation/consensus.py`, `evaluation/readiness.py` |
| Queue (rank, fair admission, atomic claim with frozen snapshot); waivers/overrides/dependencies; branching, roll-up and promotion; approval actions | `queue.py`, `governance.py`, `branching.py`, `confirmations.py` |
| LLM tree extraction (`ingest --llm`) | `ingest/llm_extract.py` |
| CLI: `evaluate`, `consensus`, `stats`, `queue`, `branch apply`, `confirmations list/approve/reject`, `waive`, `override`, `revoke`, `status` | `cli.py` |
| GH6 UTC-aware datetimes; GE6 re-seeding on init-db | `db/types.py`; `db/seed.py` `ensure_defaults` |
| Technical docs: B2 decision table, B6 view contracts, SQLite technique | `docs/ARCHITECTURE.md` |

### Judgement calls
1. **SQLite constraint changes in 0005 use `writable_schema`, not a table rebuild.** Copy-drop-rename under `foreign_keys = ON` failed: dropping a referenced table left deferred violations, and renames rewrote child FKs even in legacy mode. SQLite documents a procedure for format-neutral changes, which is what 0005 does: widen the status CHECK, append defaulted columns at the end (exactly what `ADD COLUMN` does) and add FK clauses, which `foreign_key_check` verifies at once. No row moves and every 0002/0004 trigger and index is untouched. The downgrade uses `DROP COLUMN` (SQLite 3.35+) and refuses while M2 statuses are in use. A populated M1 database and a head → 0003 → head round trip are both tested.
2. **The `content_eval` ban is a BEFORE INSERT trigger**, not the CHECK named in B9. A CHECK would also reject the legacy rows that D1 says must stay as history.
3. **Per-item pass rows** are a new append-only table, `evaluation_pass_items`: one row per pass × tag (or `""`), holding the normalised value, score and verified S/C/U counts. This keeps the views plain joins on both dialects. The raw pass, including every quote and its `verified` flag, stays in `evaluation_passes`.
4. **Disputed agreement reports the mean confidence** (ruling 1), not max(conf_a, conf_b) (ruling 5). The mean is the value the decision was taken on. Disagreement and one-sided rows follow ruling 5.
5. **B12 attempts:** a combined call gets 1 attempt + 1 repair (`evaluation.repair_attempts`). The single split re-run (`evaluation.max_escalations`) gets `attempts.quickfire_evaluation` (4) attempts per call. Each escalation has its own cycle row bound to the same versions, because pass rows are unique per cycle (R1 fix 5).
6. **Default presets** (seeded by 0005 and re-seeded by `ensure_defaults`):
   - pre-phase tier 1: `safety_clear` (hard, gate `= true`), `ambiguity_quotient`, `task_parsability`;
   - pre-phase tier 2: the routing fields;
   - pre-phase tier 3: everything else;
   - post-phase tier 1: `requirement_alignment ≥ 6` and `correctness ≥ 6` (soft), `security_risk ≤ 6` (hard); the other content fields are tier 3.
   `safety_clear` is a new task-level bool field (registry revision 2 in existing databases), so the D1 tier-1 "safety" gate has something real to check.
7. **Edits.** `supersede_packet` still creates a `draft`; the evaluator moves it to `evaluating_pre` when it starts. `update_packet` now refuses in-place core-field edits (title, description) once a packet has left `draft` (B9: an edit is a new version). Governance fields stay editable in place.
8. **Version and hash binding:** pre-phase consensus is current only for the same packet row (= version) **and** `target_hash = packets.input_hash`. Post-phase evaPackets bind to the version, and the completion gate checks that they target the newest artifact's hash when one exists. M3 makes artifacts mandatory.
9. **User completion keeps M1's manual path:** a user may still complete by hand. The post-evaluation gate applies to system actors (`evaluating_post → completed`), and roll-up to waiting trunks.
10. **Promotion** writes the routing suggestion (`channel`) only. Priority is computed live in `v_queue`, so `priority_level` is not promoted. `require_approval` rules (ambiguity ≥ 8, disputed key field) create a routing confirmation instead; while it is pending the packet cannot be admitted.
11. **Simulated time:** `v_queue` uses `CURRENT_TIMESTAMP`; `queue.queue_rows(now=…)` runs the *same generated SQL* with a bound `now`. This is SQL, not a Python fallback (B6).
12. **One `channel_runs` row per attempt**, each with its own `QE…` id. The pass row records the run id of its successful attempt.
13. **OpenAI-compatible servers cannot report digests,** so that provider refuses pinned specs rather than trusting them. Ollama is the pinned path (B11).
14. **Thresholds are now versioned** (the flush guard requires a version bump), which makes the B5 key (packet version × rule × rule version) meaningful.
15. **M1 tests were updated for the B9 lifecycle** (happy path, retry path, LLM cannot skip) and for 36 registry fields. Test cycle rows come from a `make_cycle` helper because `evaluation_passes.eval_cycle_id` is now a foreign key.
16. **GE6:** the live defaults in `db/seed.py` are copies of the frozen 0003/0005 seeds. `test_live_defaults_equal_the_frozen_migration_seeds` fails if the two ever drift.

### D2 truth table (B10) — integer rewrites and flags
Every case's **status** matches Gemini's table. Some reachable integer **confidences** differ from the table:

| Case | Table | Ours | Why |
|---|---|---|---|
| TC-02 | 0.59 | 0.59 | (5,6,6,7) |
| TC-07 | – | A 0.70 vs B 0.54 | margin 0.16 |
| TC-09 | 0.69 | 0.68 | 0.69 is unreachable |
| TC-18 | 0.69 | 0.68 | 0.69 is unreachable |
| TC-25 | 0.65 | 0.66 | nearest reachable |
| TC-26 | – | t2 0.76 | – |
| TC-30 | 0.59 | 0.55 | text confidences move in 0.05 steps |
| TC-31 | – | 0.70 vs 0.50 | – |
| TC-32 | 0.14 margin | 0.10 | 0.14 is unreachable for text |

TC-12 is re-derived as 0.70 vs 0.60 (margin 0.10). TC-22's "ignored quotes" cannot exist, because both NULLs skip every evaluation pass. Exact boundaries (0.70, margin 0.15, rounding at 4 dp) are unit-tested on `meets` and `margin_ok` (D5 point 1).

**One code/rulings disagreement was found and fixed.** The randomised SQL-versus-oracle parity test caught the oracle counting P5/P6 evidence for text fields, which have no such passes (B3). The SQL was right.

### Known issues
- **Evaluation is slow:** about 0.8 s per packet with the fake on SQLite. Each pass re-reads and refreshes the packet for the stale-write guard. This is fine at deliberate pace (D14); batch the checks if real models make it matter.
- **FakeProvider values are keyword heuristics:** good enough to exercise every path, but not a quality signal. Real-model benchmarking is D3/M5 work.
- **`v_packet_stats.prompt_word_count` is approximate:** it counts single spaces.
- **`v_branch_stats` treats each trunk version row as a root:** descendants attach to the version they were split from.
- **0005 assumes a non-defensive SQLite connection:** the Python default. `writable_schema` is unavailable if `SQLITE_DBCONFIG_DEFENSIVE` is on.
- **CI runs Python 3.11 and 3.12;** this session ran 3.13 locally.

### Next steps
- PM verification of the PR; panel review R2.
- M3: the kernel loop around evaluate → admit → claim → generate → post-evaluate → complete, plus channels, artifacts, the pace governor (D14) and the GUI.

---

## 2026-11-04 — M1.1: review R1 fix list complete (Claude Code)

All 12 fixes from `docs/reviews/R1_consensus.md` are on the PR #1 branch. Following the consensus, **MC2 and MC8 were not acted on** (verified false positives), and **GH6 (UTC-aware datetimes) and GE6 (re-seeding all defaults on init-db) remain deferred to M2**. origin/main was merged in twice this session (c8dc3d3 R1 records; 9fc63bd plan v0.5 and the D1 brief). `docs/PROJECT_PLAN.md` was not touched.

### The 12 fixes
| # | Fix | Where | Tests |
|---|---|---|---|
| 1 | Superseded history is immutable in the DB; the ORM guard checks head status | 0004 triggers on projects, objectives and packets, both dialects; `guards.assert_mutable(…, session)` | `test_fix1_*` (raw SQL ×3 tables, ORM) |
| 2 | Lineage invariants: v1 start; a successor keeps the ref, has version+1 and the same ancestry; ref/version/`supersedes_id`/ancestry never change; one successor per row; no deleting successors or rows with dependents | 0004 triggers; `guards.assert_deletable`; `repository.delete_packet` + `dependents_of` | `test_fix2_*` |
| 3 | SQLite `recursive_triggers = ON` | `db/session.py` | `test_fix3_*`: INSERT OR REPLACE on a locked packet, an event and an evaluation pass. A mutation check confirmed the test fails without the pragma. |
| 4 | Frozen migrations, with per-revision assertions and exact seeds | 0002/0003 frozen (checkpoint); 0004 written frozen | `test_migrations_frozen.py`; manifest now covers 0001–0004 |
| 5 | Downgrade and constraint hygiene | 0002 downgrade drops its objects; 0004 makes `eval_cycle_id` NOT NULL with unique `(packet_id, eval_cycle_id, field_name, pass_no, attempt)`; every downgrade is clean | `test_fix5_*`: head → 0003 → 0002 → head → base → head, with the fingerprint checked against the manifest at each step |
| 6 | Atomic ref allocation | `ref_counters` + `db/refs.allocate` (`INSERT … ON CONFLICT DO NOTHING`, then `UPDATE … RETURNING`); 0004 initialises counters from existing refs | `test_fix6_*`: two concurrent writers; refs never reused |
| 7 | Evented writes only | `GuardedSession` `before_flush`: every insert, update or delete of a domain row needs an `Event` in the same flush (one naming its primary key, or a batch event for the type); settings and field definitions must bump `version`; ORM bulk update/delete of domain models is rejected | `test_fix7_*` |
| 8 | `supersede_packet` keyword collision | `response_contract` now comes only from the merged field dict | `test_fix8_*` |
| 9 | Ingestion degrades gracefully | "Untitled task" fallback; descriptions over 20,000 characters split into "Title (part i/n)" packets with a note; leftover `ValidationError` → `IngestionError` | `test_fix9_*` |
| 10 | Missing tests | Artifact INSERT with `accepted` and no `accepted_at`, plus the tests listed for fixes 1, 5 and 8 | `test_fix10_*` |
| 11 | Typed actor (partial, per the panel) | `orchestrator/actors.py`: frozen `Actor(kind: user/system/llm, id)`. Repository, lifecycle, ingestion, field sync and `record_event` require it; only `user` actors complete or archive by hand. | `test_fix11_*` |
| 12 | Registry revision (partial, per the panel) | Append-only `field_registry_revisions` (revision, sha256, full JSON snapshot). `init-db` keeps the YAML sync and records a new revision plus a `fields.registry_revision` event whenever the content hash changes. | `test_fix12_*` |

Suite: SQLite 106 passed / 63 skipped (the PostgreSQL params). With PostgreSQL 16: 167 passed / 2 skipped (one SQLite-only test, one PostgreSQL-only test). Python 3.11 and 3.12. Ruff is clean. The CLI chain works at schema revision 0004 with registry revision 1.

### Judgement calls
1. **0002 and 0003 were frozen in place** (checkpoint) rather than superseded, because the PR is unmerged and the frozen SQL is identical to what the generator produced, so databases already at 0003 stay valid. Everything new went into **0004**. From now on, applied revisions are never edited.
2. **`ddl.py` is the authoring reference**, not the applied DDL. Revisions carry frozen literal SQL and never import it (`test_migrations_frozen.py` enforces this). This is how I reconcile golden rule 7 with fix 4.
3. **Fix 7 rejects rather than auto-emits.** The flush layer cannot know the actor or the reason, so an auto-generated event would be a poor audit record. Instead the governed APIs record their event *before* flushing, and any ad-hoc ORM write fails with `UnauditedWriteError`. Batch events (`entity_id` NULL) cover multi-row operations such as the field sync.
   - `Event` and `EvaluationPass` are exempt: they are append-only records themselves.
   - Raw `text()` SQL is outside the ORM guard, but immutability is still enforced by the triggers.
4. **"Ancestry"** for fix 2 means `parent_id`, `root_id`, `project_id` and `objective_id` for packets, and `project_id` for objectives. A packet cannot silently move to another objective.
5. **"Dependents"** that block a delete are: child packets (through `parent_id`/`root_id`, ignoring the trunk's self-reference), evaluation passes, artifacts, channel runs and quality reports for packets; packets for objectives; objectives or packets for projects. The error says "archive instead".
6. **`events.entity_id` widened to 128 characters**, so fix 7 can match setting keys.
7. **Fix 9 splits instead of raising**, so unattended ingestion keeps going and no text is lost.
8. **Fix 12 keeps a full JSON snapshot** of the registry with each revision, not just the hash, so an M2 evaluation cycle can cite exactly what it used. The hash is deliberately *not* unique: reverting A → B → A records a third revision, and history stays linear.
9. **`set_autonomy_mode`** (new, evented) accepts only user actors and refuses fixed scopes.
10. The old `test_default_seeds` was removed; the exact-seed test in `test_migrations_frozen.py` supersedes it.
11. Following the D1 note, `packet_kind` / `content_eval` was **not** extended. It will be reshaped into genPackets and evaPackets in M2.

### Known issues
- On SQLite, a transaction that *reads* before allocating a ref can get "database is locked" (WAL snapshot conflict) instead of waiting. It fails loudly with no corruption. Every current write path allocates first.
- The ORM guard runs one successor query per modified lineage row per flush. That is fine at M1 volumes; batch it if M2 bulk updates make it measurable.
- Seeds written by 0003 are not re-applied by `init-db` if someone deletes them (GE6, deferred to M2).

### Next steps
- PM verification of PR #1 and merge.
- M2: genPacket/evaPacket model (D1), evaluation cycles referencing `field_registry_revisions.id`, GH6 and GE6.

---

## 2026-11-04 — M1.1 checkpoint (paused for the usage limit) (Claude Code)

Working from the fix list in `docs/reviews/R1_consensus.md`, on the PR #1 branch (`claude/optimistic-allen-ts03d8`). origin/main was merged in at c8dc3d3, which added `docs/reviews/R1_*`. MC2 and MC8 are verified false positives and will **not** be acted on; GH6 and GE6 are deferred to M2.

### Status of the 12 fixes
| # | Fix | Status |
|---|---|---|
| 1 | Superseded history is immutable in the DB, and the ORM guard checks head status | ⏳ to do |
| 2 | Lineage invariants in the DB (keep ref, version+1, immutable ancestry, refuse deleting successors or rows with dependents) | ⏳ to do |
| 3 | SQLite `recursive_triggers = ON`, plus `INSERT OR REPLACE` tests | ⏳ to do |
| 4 | Frozen migrations, with per-revision assertions and exact seeds | ✅ done (commit 02fb8f2) |
| 5 | Downgrade and constraint hygiene | 🟡 partly done: 0002's downgrade now drops all its triggers, functions and views (frozen as part of fix 4). The evaluation-pass uniqueness change and the downgrade → re-upgrade test remain. |
| 6 | Atomic ref allocation (counter table, two-writer test) | ⏳ to do |
| 7 | Evented writes only (enforced in the flush layer) | ⏳ to do |
| 8 | `supersede_packet` keyword collision, plus a test | ⏳ to do |
| 9 | Ingestion degrades gracefully (untitled fallback, over-long descriptions) | ⏳ to do |
| 10 | Missing tests (artifact INSERT with `accepted` and no `accepted_at`, plus the tests for fixes 1, 5 and 8) | ⏳ to do |
| 11 | Typed `Actor` (user / system / llm) | ⏳ to do |
| 12 | Registry revision hash plus an event on change | ⏳ to do |

### What fix 4 changed
- `0002` now holds literal SQL for both dialects: 21 SQLite and 18 PostgreSQL statements, identical to what the generator in `ddl.py` produced, so databases already at 0003 stay valid. Its downgrade drops every trigger, function and view it created.
- `0003` holds literal seed rows, written through local `sa.table` definitions. It inserts only missing rows and writes one `seed.defaults` event per table, as before. Its downgrade is a deliberate no-op: seed rows are governance state, and re-upgrading is idempotent.
- `tests/test_migrations_frozen.py` checks three things:
  - revisions import nothing from `orchestrator`;
  - upgrading empty → each revision yields exactly the trigger, view and function fingerprints in `tests/migration_manifest.json` (whitespace-normalised SHA-256, both dialects);
  - the seeds after empty → head are exact.
- `ddl.py` is now the **authoring reference** only; revisions never import it. Judgement call: this is how I read golden rule 7 ("dialect-specific DDL in one module") alongside fix 4.
- Long literal SQL lines in revision files are exempt from E501 (`pyproject.toml` per-file-ignores).
- Suite at this checkpoint: SQLite 79 passed / 39 skipped (PostgreSQL params); with PostgreSQL 16, 117 passed / 1 skipped. Ruff is clean.

### Plan for resuming (decided, not yet built)
- **One new revision `0004` ("M1.1 governance hardening")** carries all remaining schema work, so databases already at 0003 can upgrade. 0001–0003 are never edited again.
  - Fixes 1 and 2: per-table lineage triggers on projects, objectives and packets, for SQLite and a PL/pgSQL function per table.
    - A row with a successor rejects UPDATE and DELETE.
    - DELETE is refused when `supersedes_id IS NOT NULL` or when the row has dependents (packets referenced through `parent_id`/`root_id` other than themselves, objectives with packets, projects with objectives), with the message "archive instead".
    - INSERT requires `version = 1` without a predecessor; with one, the predecessor must have the same ref and `version + 1`, and the same `parent_id` / `root_id` / `project_id` / `objective_id` (same `project_id` for objectives).
    - UPDATE may not change ref, version, `supersedes_id` or ancestry.
  - Fix 5: `evaluation_passes.eval_cycle_id` becomes NOT NULL, with unique `(packet_id, eval_cycle_id, field_name, pass_no, attempt)` via `batch_alter_table`. **Note:** a SQLite batch rebuild drops the table's triggers, so 0004 must recreate `evaluation_passes`' append-only triggers on SQLite, and its downgrade must recreate 0002's versions. Add a head → base → head test on both dialects.
  - Fix 6: a `ref_counters(name PK, value)` table, initialised in 0004 from existing max refs. Allocate with `INSERT … ON CONFLICT DO NOTHING` followed by `UPDATE … RETURNING`. Two-writer test using threads, relying on PostgreSQL row locks and SQLite's `busy_timeout`.
  - Fix 12: an append-only `field_registry_revisions(id, revision, sha256, definitions JSON, created_at)` table, with a new row and an event whenever the YAML hash changes.
  - Widen `events.entity_id` to 128 characters so setting keys fit (needed for fix 7). This is also a batch rebuild on SQLite, so recreate the `events` triggers too.
  - After 0004, regenerate the manifest and add per-revision expectations.
- **Fix 3:** add `recursive_triggers = ON` to `SQLITE_PRAGMAS` in `db/session.py`. Tests use `INSERT OR REPLACE` on a locked packet, an event and an evaluation pass.
- **Fix 7:** `before_flush` rejects (new `UnauditedWriteError`) any insert, update or delete of a domain row unless the same flush contains a new `Event` for that entity type, either with its id or as a batch event with `entity_id IS NULL`. Settings and field definitions must also bump their version. A `do_orm_execute` hook rejects ORM bulk update/delete on domain models.
  - Repository, lifecycle and ingestion must add their event **before** flushing, and `persist_tree` must assign ids up front.
  - Tests that edit rows directly need evented helpers: `set_autonomy_mode` in the repository, and events for test artifacts.
- **Fix 8:** in `supersede_packet`, drop the explicit `response_contract=` keyword and take it from the merged dict. Add a test that changes `response_structure`.
- **Fix 9:** use an "Untitled task" fallback for empty titles. Descriptions over 20,000 characters are split at paragraph boundaries into "Title (part i/n)" packets, each with a note (judgement call: split rather than raise, so unattended ingestion keeps going and loses nothing). Any remaining `ValidationError` is wrapped in `IngestionError`.
- **Fix 11:** a frozen `Actor` dataclass (`kind` ∈ user | system | llm, plus `id`), built with `Actor.user("cli")`, `Actor.system(...)` and `Actor.llm(...)`. `str(actor)` is what goes into `events.actor`. Only `kind == user` may complete or archive outside the table. All repository, lifecycle and ingestion signatures take an `Actor`.
- Then add an M1.1 entry here, update the PR #1 description checklist with the 12 fixes, and confirm CI is green on all three jobs.

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

### Proposed plan updates (for the PM; `docs/PROJECT_PLAN.md` is PM-owned)
- §16: M1 status → "Implemented incl. R0 amendments A1–A7; PR [redtongues/PythonPlanner#1](https://github.com/redtongues/PythonPlanner/pull/1) in review".
- §21: 2026-11-04 — M1 implemented by Claude Code with A1–A7 on `claude/optimistic-allen-ts03d8`; tests green on SQLite and PostgreSQL 16; PR opened.
- §6: record that refs name a lineage (`(ref, version)` unique, `REF@N` addresses a version) and that `v_project_heads` / `v_objective_heads` exist alongside `v_packet_heads`.

### Next steps
- PM review and panel triage of the M1 PR; merge.
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
