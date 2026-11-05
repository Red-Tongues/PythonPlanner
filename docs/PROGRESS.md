# Progress log

Newest entries first. Every agent session appends an entry: what changed, decisions, deviations from spec (with reasons), review triage, known issues, next steps.

---

## 2026-11-05 (evening) — CFO directive folded in: T1-C v3, Q1 v2, M3a spec draft (Claude Code, docs only)

Handover from the PM after the CFO's directive (`docs/notes/2026-11-05_CFO_T1C_v2_Directive_and_Review.docx`). The work is **docs only**: no `src/` changes and no M3a code. Branch `docs/t1c-v3-m3a-spec`, one commit per task, draft PR into `main` (not merged). PR #3 was already merged (6b89a18), so the directive's Step 1 is done.

**What changed**
- **Directive .md** (9e7a147): the committed Markdown is now a verbatim pandoc-checked conversion of the .docx.
  - The earlier conversion had added a "Subject:" line, dropped the C.ii intro sentence, shortened Sketch 2's heading and changed `sonar_Hotspot_alignment`.
  - A conversion note says the .docx is authoritative.
- **PROJECT_PLAN §21** (e231aa9): dated entries for the directive, decisions Q1–Q7 and the PM note that Step 1 is satisfied.
- **T1-C v3 (Final)** (`docs/notes/2026-11-05_T1C_v3_final.md`, dcf2e3e).
  - **Kept:** the outbox, `field_revisions`, `content_blobs`, `sealed_bundles`, the trigger proof (T1–T6) and fencing.
  - **Retired:**
    - `supersedes_id` cloning of genPackets;
    - `require_current_context` hard invalidation;
    - `enforce_failure_budgets` and the K = 3 breaker (plus the lineage retry budget and the 10% prior);
    - `binding_mismatch` staling on `gate_revision` / upstream `input_hash`;
    - v2's "re-opening" and episode numbers. A genPacket now seals once and continues as a child.
- **Q1 v2** (`docs/notes/2026-11-04_Q1_queue_maths_brainstorm.md`, fe6cbee):
  - genQueue with evaQueue as a subset;
  - continuous Ready/Queued enrichment;
  - the 0–2 lockout;
  - mandatory fields gate Ready, and relational score alone gates Queued;
  - feedback as a weight;
  - crashes counted apart from grades.
  - Steps 1–3 are kept as history, each mapped to what Q1 v2 does with it.
- **M3a spec draft** (`docs/milestones/M3a_genpacket_loop.md`, b56375d):
  - scope = Step 3 (a)–(e);
  - module/view change table (directive C.i, re-located to current line numbers);
  - 35 acceptance criteria for SQLite + PostgreSQL 16;
  - a table of 10 state-machine conflicts with ARCHITECTURE §7 / D8;
  - **28 open questions for the CFO**.

**Decisions / deviations**
- Treated the directive's sketches as non-production references. Every gap was logged as an open question (OQ1–OQ28) rather than decided. Each one is referenced from T1-C v3 and Q1 v2 where it bites.
- The biggest ones:
  - **OQ6:** with the sketch's maths, an isolated root packet never reaches the 3,500 floor.
  - **OQ13:** sealing every graded parent as `completed` would let poor output satisfy dependencies.
  - **OQ14:** "a child regardless of grade" means a lineage has no stop rule.
  - **OQ5:** `question_context` raises rank, which pushes packets into the lockout where they can't be refreshed.
  - **OQ15:** today's repo write happens before post-evaluation.
- Not edited: CLAUDE.md Golden Rule 1 and Requirement F. Their rewording waits for the CFO (OQ26).

**Known issues:** the directive's C.i line numbers predate PR #3's final tree; the M3a table uses current approximate locations.

**Next:**
1. The CFO reviews the draft PR and answers or delegates OQ1–OQ28.
2. The PM folds the answers into the M3a spec and T1-C v3.
3. Then M3a implementation (directive Step 4) on a feature branch, with the baseline regenerated and both dialects green.

---

## 2026-11-05 09:00–09:30 — R4 audit (Argon) J1–J3 fixed (Claude Code)

Argon's independent audit of PR #3 was a **conditional pass: fix J1 and J2, then merge**. The PM decided J1–J3 under sprint autonomy (the CFO is busy). J4/J5 are informational, so no change. Not merged; the PM merges.

- **J1 [HIGH] (3e2c5b0):** a rejected split now goes `awaiting_confirmation → held` with the reason (D8 row 61).
  - Removed the user `awaiting_confirmation → ready` row from `transitions.py`, the regenerated baseline (so the DB trigger rejects it too) and ARCHITECTURE §7.
  - `confirmations.reject` refuses a blank reason (D8: "reason required").
  - The kernel `ready → awaiting_confirmation` split-confirmation row is kept and documented in deviation **P1** as a D8 gap-fill alongside the failure-budget one.
  - Removed the stale §7.3 row saying a confirmed split is recorded as `system:brancher`; since MR5 it is the approving user's move (D8 row 60).
  - The branching test asserts `held`, the user actor and reason, the blank-reason refusal, and that release + readiness still never re-proposes the split.
- **J2 [MEDIUM] (56c7994):** in `stale_run`'s keep-state branch, the ownership release changes the ORM object and records `packet.run_released` only when its UPDATE matched one row.
  - **Choice on 0 rows:** re-read the packet. If this run no longer owns it, record nothing (logged at info), because the release's goal is already met. If it somehow still does, raise `LostRace`.
  - **Why not always raise `LostRace`:** it would roll back the run's own audited stale disposition (MR9), which is still correct.
  - The test injects the concurrent release between the read and the UPDATE, and fails without the fix.
  - The residual `LostRace` branch is untested; it is unreachable without a concurrent writer restoring the same ownership.
- **J3 [LOW] (c2a6499):** both `reconcile_runs` scans are ordered by `(packet_id, id)`.

**Tests:** 599 passed, 4 skipped locally on SQLite + PostgreSQL 16; ruff clean. CI: see the PR.

**Next:** the PM merges PR #3; then the M3a spec once the CFO signs off T1-C v2.

---

## 2026-11-05 08:20–09:00 — R4 verification on PostgreSQL, fixes and review (Claude Code)

Fresh session on PR [redtongues/PythonPlanner#3](https://github.com/redtongues/PythonPlanner/pull/3) (`claude/elegant-davinci-hcny5t`). It covers the Copilot agent's groups 3–11 (d205356, 66076d0). No new features, no T1-C/M3a work; not merged.

**What changed**
- **Why CI never ran for 66076d0:** PR #3 conflicted with the force-updated `main` (PROGRESS.md only), so GitHub built no merge ref and ran no `pull_request` workflow. Merged `origin/main` (docs only) in fa203b8.
- **PostgreSQL 16 (local):** 4 failures; SQLite had 1 of them.
  - **One real bug, fixed (75e801e, MR9).** A torn read under READ COMMITTED let a late approval stale a parked run that a concurrent rejection had just kept parked. The stale disposition now retires its own confirmation with the once-only CAS first, so a decided confirmation makes the approval lose. A deterministic regression test injects the torn read.
  - **Three agent test bugs, fixed (95cb2f1):** `"duplicate"` outcome rejected; a pause test that deadlocks on PG; savepoint/`pytest.raises` order.
- **PM observations:**
  - (b) the `input_hash` rewrite is now in the failure-budget event's before/after (ad66ca3).
  - (c) `budget.reserved` is now recorded only after every demand succeeds (39f5385). The old order was safe, because no `src/` code catches `BudgetExhausted`, but it relied on the caller rolling back.
  - MR5 now has an actor assertion (c8f444a).
- **PR description:** a 16-row R4 table (code → test → panel / code / test status). 15 of 16 are test-proven. GA-R1 is code-verified only: its guard is unreachable behind `_bump_pair`'s full-binding CAS in the same transaction.

**Decisions / deviations**
- **D8 deviation flagged, not changed:** split rejection `awaiting_confirmation → ready` (user).
  - Recommend removing it: D8 row 61 sends every rejection to `held`, and a user `→ ready` skips the kernel's readiness decision.
  - If the CFO wants no extra owner step, use the review pattern instead (user → `evaluating_pre`, readiness admits).
- **Kept (recommend):** kernel `ready → awaiting_confirmation` for a split request. D8 row 60 needs the packet there; it is the same kind of gap-fill as P1.

**Tests:** 597 passed, 4 skipped on SQLite + PostgreSQL 16 locally; the concurrency suite was green on 5/5 repeat runs. Ruff clean. CI: see the PR.

**Known issues:** `main`'s own CI runs have been failing since the 5 Nov 02:15 PM pushes (not investigated; outside this PR's scope).

**Next:** the CFO decides the split-rejection target; Argon (or the GitHub Copilot auditor) audits the R4 diff and the 16-item map.

---

## 2026-11-05 02:10–03:00 — Sprint: T1-C v2 and R4 status (PM)

- **R4 groups 3–11:** done by the GitHub Copilot cloud agent (SQLite 361 passed / 234 skipped; PostgreSQL not run). **Not pushed.** Waiting for the CFO to press *Create pull request*.
  - PR #3 head (groups 1–2) is green in CI on PostgreSQL 16.
  - PM read the visible diff. One D8 deviation: `awaiting_confirmation → ready` on split rejection. One minor gap: the failure-budget resume changes `input_hash` without that change appearing in the event's before/after (Golden Rule 5).
- **T1-C → v2** (`docs/notes/2026-11-05_T1C_v2.md`): Argon G-audit → v2 → GPT-6.1 review → Argon audit #2, verdict *conditional pass, ready for CFO sign-off*.
  - Trigger proof-of-concept executed on SQLite and PostgreSQL; one SQLite producer-attribution defect fixed.
- **Q1 step 3** (integer ranking policy) recorded in the Q1 note.
- **Next:**
  1. The CFO creates the PR.
  2. A fresh Claude Code session merges it into PR #3, runs the PostgreSQL suite and maps all 16 items (handover in `docs/notes/2026-11-05_sprint_plan.md`).
  3. The CFO answers T1-C v2's 7 questions.
  4. Then the M3a spec.

---

## 2026-11-04 (late) — R4 fixes completed (Copilot)

Continued the approved fix list on PR #3 from Claude Code's checkpoint (groups 1–2 already done). Completed groups 3–11; no M3a/Stage B feature work.

- **Run lifecycle:** approved resumes atomically renew the packet and run leases; retries require a failed/invalid/timed-out outcome or recorded deadline expiry; dispatch sequence numbers are separate from chargeable attempts, so exit-3 resume consumes no attempt budget.
- **Branching:** a confirm-scope split moves the packet into `awaiting_confirmation`; approval keeps the owner actor for the permitted transition, and rejection returns the packet to `ready`.
- **Recovery and confirmations:** stale restricted runs retain an explicit deferred-recovery marker and are reconciled after restrictions clear; legacy `claimed` runs are reconciled; stale parked runs retire their pending confirmation and recover the packet instead of rolling the disposition back.
- **Guarded decisions:** `cancel_run` and `reject_run` guard packet ownership and fencing token; failure-budget approval binds the confirmation to the current packet version and applies the status/gate change in one guarded transaction; trunk packets include their own branch scope.
- **Budgets and races:** demands, usage and database rows reject negative values; reservation events are kept with multi-demand inserts under `no_autoflush`; race helpers propagate unexpected worker failures and enforce timeouts; an effect-counting adapter test covers dispatch budget and stale acceptance.
- Updated the generated baseline and architecture transition tables for the recovery marker, retired confirmation status, budget CHECK constraints and branch-confirmation transitions.

**Verification:** full SQLite suite `361 passed, 234 skipped`; Ruff check/format and `git diff --check` clean. PostgreSQL cases were skipped because `ORCH_TEST_POSTGRES_URL` is not configured in this environment.

**Next:** run the opt-in PostgreSQL suite in CI; no known SQLite failures. No commits or pushes made.

---

## 2026-11-04 (late) — R4 fixes: PAUSED at the CFO's request (Claude Code)

Working from `docs/reviews/R4_consensus.md` (16 items) on PR [redtongues/PythonPlanner#3](https://github.com/redtongues/PythonPlanner/pull/3), one commit per fix group. The CFO paused the sprint until 23:59; I stopped at a clean point.

**Done (committed, full suite green on SQLite and PostgreSQL 16: 573 passed, 4 skipped):**
- **Group 1, MR1** (6dae604): every binding-dependent run write (dispatch, accept, publish, park, the run-targeted gate bump) re-checks the packet row in one compare-and-set (`runs.guard_bindings`). A two-session barrier test at the `before_scopes` seam fails on PostgreSQL without the guard.
- **Group 2, MR2 + MR3** (c83d407):
  - kernel dispatch, accept, publish and park need a fenced `KernelLock`;
  - a user dispatch is only `approve_run`'s resume with the run-bound, just-approved confirmation, while the packet is running with no HOLD/BLOCK;
  - tests take the lock through `runfixtures.held()`;
  - on SQLite the fence serialises a concurrent governance write behind the dispatch, and the barrier test asserts whichever serialisation happened.

**Next, in order (not started):**
- group 3: MR4 lease renewal on resume, MR6 retry eligibility, MR7 dispatch sequence separate from chargeable attempts;
- group 4: MR5 split continuation;
- group 5: MR8 deferred recovery, MR10 legacy reconciliation;
- group 6: MR9 stale-approval disposition (a `retired` confirmation status);
- group 7: GR1, GA-R1 guarded `cancel_run` / `reject_run`;
- group 8: GA-R2 failure-budget atomicity;
- group 9: GA-R3 trunk branch scope;
- group 10: MR11, GA-R5 budget validation, CHECKs and audit ordering (`reserve` with two demands currently autoflushes before its event);
- group 11: MR12, MR13 race-test hardening with an effect-counting fake adapter.

Then PROGRESS, the PR checklist and the report.

---

## 2026-11-04 (evening) — Build-process tidy (CFO decision Q4) (Claude Code)

On PR [redtongues/PythonPlanner#3](https://github.com/redtongues/PythonPlanner/pull/3), as five focused commits, after merging `main` at e4234bb (the CFO–Argon clarification). The PM asked for no R4 fixes and no M3a work until the data model is settled, so this is a **build process** change only. The **product** rule is unchanged: immutability, append-only and transition triggers stay, are never disabled, and keep their tests on both dialects.

**Tests:** 564 items before (560 passed, 4 skipped) → **571 after (567 passed, 4 skipped)** on SQLite plus PostgreSQL 16. No test was dropped. The frozen-migration tests were replaced by baseline drift and schema checks (+7 items), and the test move kept all 571 items, with the same names and parameters apart from the dropped `fixN_` prefixes. Ruff is clean.

### What changed
| # | Change | Where |
|---|---|---|
| 1 | **One contracts location**: `evaluation/contracts.py` → `contracts/evaluation.py` | `contracts/` |
| 2 | **One master schema**: tables in `db/models.py` (now also holding the 4 CHECK constraints and 7 server defaults that existed only in migrations); **every** trigger in `db/ddl.py` (including the lineage and evaPacket/run triggers that existed only inside 0004 and 0005); every view in `sql/views.py`; `db/schema.py` assembles them per dialect | `db/`, `sql/views.py` |
| 3 | **Squashed migrations**: 0001–0007 → one generated `0001_baseline` (literal DDL, written by `scripts/generate_baseline.py`). The frozen manifest, its test and `scripts/freeze_views.py` are gone. `tests/test_schema.py` is the drift check: baseline = master schema (both dialects); installed triggers/views/functions = master; models vs DB (`compare_metadata`); downgrade round trip | `migrations/versions/`, `scripts/`, `tests/test_schema.py` |
| 4 | **Tests by capability**: `test_r1_fixes`, `test_m21_fixes`, `test_m3_races`, `test_m3_schema` → `test_immutability`, `test_versioning`, `test_schema`, `test_refs`, `test_audit`, `test_ingest`, `test_actors`, `test_fields`, `test_gates`, `test_concurrency`, `test_evaluator`, `test_providers`, `test_queue`, `test_db_guards`. Shared helpers → `tests/dbhelpers.py`, `tests/govfixtures.py`; the `superseded` fixture → `conftest.py` | `tests/` |
| 5 | CLAUDE.md golden rule 7 now states the master-schema rule and the regenerate command; README and ARCHITECTURE §6 updated | docs |

### Equivalence check (done before deleting 0001–0007)
I snapshotted the old head schema on both dialects: every table, column, type, nullability, default, PK, FK, unique, index, CHECK, trigger, function and view. I then compared it with the database the baseline builds.
- **SQLite:** identical.
- **PostgreSQL:** identical except:
  1. `control_state.id` no longer has a sequence (it is a single-row table and the id is always written explicitly);
  2. a status CHECK lists its values in a different order;
  3. `v_ready_candidates` now includes `fencing_token` and `active_run_id`. Its `p.*` had been expanded when 0006 created the view, before those columns existed, so the old view silently lacked them.

### Decisions (please check)
1. **The baseline is literal, generated text, not a migration that imports the models at runtime.** A new column is still written once (in `models.py`), and you regenerate. I chose this so the baseline can simply be frozen as it stands the day a database must be kept.
2. **Seeds are no longer in a migration.** `init-db` (`ensure_defaults`) is the only seeding path, as it already was for re-seeding (GE6). `migrate.upgrade()` on its own now builds an empty schema; the CLI always goes through `init-db`. The fresh-database seed events are now one batch per table (five, not six).
3. **A database with history refuses to downgrade.** Downgrading to empty would drop the append-only `events` table in one statement, bypassing its row triggers.
4. **New revision id `0001_baseline`.** Any local database at the old `0007` must be recreated (`orchestrator init-db` on a fresh file). There is no live data, so I treated this as acceptable.
5. **Replaced revision-specific tests.**
   - The populated "0004 → 0005" and "0006 → 0007" upgrade tests have no upgrade path left. They became "a populated database survives `init-db`" and "legacy M1 statuses stay valid and only re-enter evaluation" (deviation P3).
   - The manifest round trip became a baseline downgrade round trip plus "re-init keeps the schema and ref numbering".
6. **Two SQLAlchemy pitfalls are handled in `db/schema.py`.**
   - The one FK cycle (`generation_runs.channel_run_id`, marked `use_alter`) is kept inline on SQLite. `create_all` would silently drop it there.
   - `AddConstraint` permanently marks that FK as not inline, so its effect is undone after the PostgreSQL `ALTER` is compiled.
   - The output is checked to be identical across hash seeds and dialect order.
7. **This supersedes my Stage A note that "0007 stays amendable until merge".** The 0007 schema now lives in the master schema and the baseline.

### Not done (as instructed)
- `docs/reviews`, `docs/notes`, `docs/milestones` untouched. Older PROGRESS entries still name the old migrations and test files; they are history and were not rewritten.
- No R4 fixes, no M3a/Stage B features.

---

## 2026-11-04 — M3 Stage A: transition tables, migration 0007, governance atomicity, fencing (Claude Code)

Branch `claude/elegant-davinci-hcny5t`, draft PR [redtongues/PythonPlanner#3](https://github.com/redtongues/PythonPlanner/pull/3). `main` was merged in three times during the session (D8 errata E1–E11; D9 with ruling D9-R1; D10 with rulings Q1–Q16), all docs only. PM-owned docs were not edited (see the ruff note below). Stage A stops here for the PM checkpoint; no channels, kernel loop, GUI or integrations yet.

**Suite:** 560 passed and 4 skipped (564 items: 346 SQLite, 218 PostgreSQL) on SQLite plus PostgreSQL 16 locally (Python 3.13), up from 410 + 4 at the start of the session. The 4 skips are the existing dialect-specific tests. The race tests passed 3× on PostgreSQL and 5× on SQLite. Ruff check and format are clean; the CI CLI smoke chain runs.

### What was built
| Piece | Where | Tests |
|---|---|---|
| Packet and run transition tables (C23), from D8 + errata E1–E11 + D9-R1 + D10 Q14, with actor kinds per row | `orchestrator/transitions.py` (single guard); `lifecycle.transition` applies it | `tests/test_transitions.py`: every unlisted pair × every actor kind rejected; D8 §4 list; errata rows |
| Published tables + deviations | `docs/ARCHITECTURE.md` §7 (generated by `scripts/transition_tables.py`; a test fails on drift), §8 (C25/C26 mechanisms) | `test_architecture_doc_publishes_the_tables` |
| Migration **0007** (frozen literal DDL, `orchestrator.db.ddl.m3_statements` is the authoring source) | `db/migrations/versions/0007_m3_runs_and_control.py`, `db/models.py` | `tests/test_m3_schema.py`; manifest regenerated (0001–0006 entries unchanged) |
| C25 scope-wide gate bumps | `gates.bump_scope`, wired into autonomy scopes, settings, project/objective supersession, confirmation decisions | `tests/test_runs.py` |
| C26 fencing, dispatch guard (E3), pause serialisation, kernel lock | `queue.claim`, `runs.py`, `control.py`, `budgets.py` (minimal C36 reservation primitive used by E3) | `tests/test_runs.py`, `tests/test_m3_races.py` |
| R1 / E2 / Q5 parked-run decisions; cancellation | `runs.park_run`, `approve_run`, `reject_run`, `cancel_run` | both files |
| Rulings 4–5 kernel claim handling | `runs.try_claim` | `tests/test_runs.py` |

### 0007 contents
- `generation_runs`: state set widened to the C24 machine (`planned`, `dispatched`, `awaiting_result`, `validating`, `accepted`, `failed`, `stale`, `awaiting_confirmation`; legacy `claimed`/`succeeded`/`abandoned` kept). New columns: channel, fencing token, live gate revision, lease and heartbeat, attempts, max attempts, deadline, resume key, model identity and `model_verified` (C29), manifest hash, commit SHA, expected HEAD, failure kind, `charged`, stale reason, finished at.
- `channel_runs`: append-only (D10 Q3). New columns `generation_run_id`, `eva_packet_id`, `operation_id` (unique index), `fencing_token`. Outcomes go to the new append-only `channel_run_outcomes` (unique per attempt). View `v_run_attempts` shows each run's latest attempt and outcome for the console.
- `packets`: `fencing_token`, `active_run_id` (E7).
- New: `kernel_leases`, `governor_states`, append-only `governor_events` (with a unique `observation_key`: each observation counts once), `budget_counters`, append-only `budget_reservations`, `budget_reconciliations`, `budget_topups`.
- Triggers (both dialects): **the packet and run transition tables are enforced in the database** (any status change not in `orchestrator.transitions` is rejected, marker `orchestrator-transition`); a finished run, or an accepted run whose commit is recorded, never changes; run bindings are frozen (channel, F, claimed_by, created_at), the run's G moves only by +1, manifest hash and commit SHA are write-once; the new append-only tables reject UPDATE/DELETE (PostgreSQL also TRUNCATE); control rows are never deleted, the lease epoch never decreases and a governor state change must raise its revision.
- C22 test: a populated 0006 database (running and completed packets, a superseded version, an M2 `claimed` run, a QE attempt row) upgrades with ids, hashes, lineage, statuses and gate revisions unchanged; locked rows stay immutable; new `content_eval` rows are still rejected; the M2 run can only go stale; down to 0006 and back up works, and the downgrade refuses while M3 run states are in use.

### Judgement calls (please check)
1. **The DB enforces the transition tables**, not only the application (C23 asks for tests that unlisted transitions are rejected; the DB is the guarantee, as for immutability). Raw-SQL tests in `test_immutability.py` now walk the table before testing the lock invariant.
2. **Deviations from D8** are P1–P6 and R1 in ARCHITECTURE §7.3 (each with its reason): P1 `ready → awaiting_confirmation` kept for the M2.1 #12 sweep; P2 users may complete from `evaluating_post`/`waiting_for_children` (same gate; keeps `packet complete`); P3 legacy M1 statuses only re-enter at `evaluating_pre`; P4 `archived` has no entries (replaced by `closed_unverified`); P5 new versions start in `draft`; P6 `blocked → evaluating_post` (D10 Q14); R1 legacy `claimed` runs may only go stale.
3. **M2 behaviour changed to follow the table** (tests updated, not weakened): a review approval goes `needs_review → evaluating_pre` (owner) and then readiness `→ ready` as the kernel actor `system:readiness` in the same transaction; failure-budget approval → `queued` (or `evaluating_pre` if H changed); failure-budget rejection → `held` (C27 supersedes M2.1 #12's `failed`); a context change no longer sends `needs_review` back to evaluation (only the owner does, so a dispute never loops); completing straight from `draft` is rejected even with evidence; closing unverified requires a reason in the API too.
4. **Settings changes bump every open packet's gate** (global scope). C25 does not list settings, but they hold thresholds, budgets and weights that readiness and eligibility read; this is the fail-closed reading. Autonomy-scope changes, project/objective supersession and the ingestion confirmation's decision bump their scopes as C25 lists.
5. **Scope ids are refs** (stable across versions): `*`, `P-…`, `O-…`, `PP-…`; a branch is its trunk's ref plus every packet whose `root_id` is a version of it. Scope rows in `governor_states` are created lazily (insert-if-absent).
6. **Pause serialisation:** a dispatch touches every governing scope row with `UPDATE … SET revision = revision + 1 WHERE status = 'normal'` in a fixed order (deadlock-free). That bumps `governor_states.revision` on each dispatch without an event of its own; the dispatch's attempt event records it. Pause/resume are evented and never bump packet gates (D10 Q4).
7. **Kernel lock heartbeats write no event** (liveness, not a state change); acquisition, takeover and release do. `fence` doubles as the heartbeat inside each guarded transaction.
8. **Parking a run bumps packet G and run G together.** Creating a confirmation is a governance write (M2.1 bumps on creation), which would otherwise stale the very run it parks. R1 covers approval and D10 Q5 rejection; creation is my extension of the same rule.
9. **Approving a parked run fails closed** if any dispatch gate refuses (paused scope, budget, stale binding): the whole approval rolls back and the confirmation stays pending, rather than recording an approval that cannot resume (C27: approval revalidates the dispatch gates).
10. **Cancelling a parked run requires its confirmation to be decided first** (cancellation follows rejection, C27/E2).
11. **Every confirmation decision is now a compare-and-set** (`WHERE status = 'pending'`), not only parked-run ones. This closes a latent M2 race where two concurrent decisions could both apply.
12. **`channel_runs.operation_id` is a unique index**, not a unique constraint, so 0007 can add it to the existing SQLite table without a rebuild. A unique index on the model keeps the drift check exact.
13. **Budget columns are `period` / `period_start`**, not `window`, which is reserved in PostgreSQL.
14. **Budget limits are passed by the caller** in Stage A (`budgets.Demand.limit`). Wiring them to settings, windows and top-ups is the Stage B governor.
15. **The legacy `generation_runs.channel_run_id` FK is marked `use_alter`** in the model, to break the FK cycle with `channel_runs.generation_run_id`; otherwise the schema drift check silently skipped both tables' foreign keys.
16. **0007 stays amendable until the M3 PR merges,** as 0002/0003 did during M1.1: nobody applies an unmerged revision, and C22 asks for one M3 migration. Stage B will extend 0007 (operation journal per D10 §1/§4, artifacts/manifests, scoped overrides C35) rather than add 0008. It is frozen at merge.
17. **ruff excludes `docs/`** (`pyproject.toml`). ruff 0.16 formats Python snippets inside Markdown: `ruff format .` rewrote the PM's D10 doc, and CI's unpinned ruff would have failed `ruff format --check .` on main's docs. I restored D10 to main's version.

### Spec points that look ambiguous or wrong (for the PM)
- **Failure budget semantics vs `v_queue`.** C27 says approval grants a *bounded* allowance (`failure_budget_topup`, default 1) and counts failed *runs*; `v_queue` (0006) still implements M2.1 #12 (a fresh budget, counting failed LG/WP `channel_runs` *attempts*). With one row per attempt from M3, the view would count retries as failures. Stage B must change the view, which means a view change in 0007.
- **C23's state set omits `archived`** and the M1 legacy statuses, but existing rows use them. Handled as P3/P4.
- **D8 has no `ready → awaiting_confirmation`,** but the M2.1 #12 sweep needs it (P1). Otherwise a packet that reaches `ready` with an exhausted budget is stranded.
- **Exit-3 confirmation creation vs R1.** R1 covers approval and Q5 covers rejection, but not the creation, which is also a gate write (judgement 8).
- **Approval while paused.** C27 says approval revalidates the dispatch gates, but not whether a refused approval should stay pending or be recorded for a later resume. I chose pending (judgement 9).
- **C22 "more only if unavoidable" vs staged delivery** (judgement 16).

### Known issues / not in Stage A
- C38 "crash after git commit, before the DB acknowledgement" needs the repository writer (Stage B).
- E11 deadline handling, E4/E5/E6 guards, budget settings/windows and the governor ladder are in the tables and guard text only; they are wired in Stage B.
- `generation_runs.channel` is taken from `packets.channel` at claim and may be NULL for an unrouted packet; the dispatch guard then refuses (fail closed). Stage B routing should make the claim itself refuse unrouted packets.
- Rows with the legacy `claimed` state cannot be dispatched; `runs.reconcile_runs` stales them.

### Next steps
- PM checkpoint review of Stage A; then Stage B (kernel loop, channels, adapters, repo writer) per D10.

---

## 2026-11-04 — M2.1: review R2 fix list (Claude Code)

All 12 fixes from `docs/reviews/R2_consensus.md` are on PR #2 (branch `claude/clever-keller-jogsc2`), after merging `origin/main` at 7795b16 (docs only). GE7 is deferred to M3; GE8 and GE10 were declined and not implemented. Migrations 0001–0005 are untouched: all schema work is in the new frozen **0006**, and its trigger and view DDL live in the dialect modules (`db/ddl.py`, `sql/views.py`), with tests that the frozen literals equal them. Earlier the helper script `scripts/ask_gemini.py` was reverted (0e83b02) so the PR stays strictly M2.

**Suite:** 410 passed and 4 skipped on SQLite plus PostgreSQL 16 locally. The 4 skipped tests are dialect-specific and skipped on the other dialect. Ruff is clean.

### The 12 fixes
| # | Fix | Where | Tests |
|---|---|---|---|
| 1 | Context binding: the fingerprint is recomputed from the current packet, project and objective heads at every evaluation write, at admission and at claim. A mismatch → `evaluating_pre` plus a `packet.context_changed` event. The guarded UPDATE also requires those heads to still be heads | `gates.require_current_context`, `evaluator._check_current`, `queue` | `test_fix1_*` |
| 2 | Gate revision: waiver, override, revocation, dependency and confirmation writers bump `packets.gate_revision`. Admission and claim read it before the check, build the snapshot in the same transaction, and compare-and-set it | 0006; `gates.bump_gate`, `guarded_status_update` | `test_fix2_*`, including two-session barrier races (waiver revoked, BLOCK added) on both dialects |
| 3 | Post-phase consensus binds the sha256 of the newest artifact; completion needs a passing post-phase verdict for exactly that artifact; post-evaluation reads the artifact itself | `v_field_consensus`; `readiness.completion_blockers`; evaluator | `test_post_consensus_binds_the_exact_newest_artifact` (A accepted, then B incomplete → blocks), TC-52 |
| 4 | The content address includes the field-definition fingerprint and the exemplars. Consensus is current only for the active preset revision and the current definition version; anything else is `pending`. `evaluate_pending` re-evaluates `ready` packets whose results went stale | evaluator; 0006 `eva_packets.definition_version`; view | `test_fix4_*` (definition only, exemplars only, preset only) |
| 5 | The completion gate applies to users too. Hard gates are never bypassed; soft gates pass only through a version- and phase-bound waiver. Closing without evidence is `closed_unverified` (locked, never a success; CLI `packet close`) | `lifecycle`, `cli` | lifecycle tests; `test_soft_post_field_passes_only_with_a_post_phase_waiver`; `test_fix5_*` |
| 6 | Frozen evaluation-cycle bindings and evaPacket identity columns; append-only `packet_dependencies` (changes go through revocations); PostgreSQL TRUNCATE guards on `generation_runs`, `packet_dependencies`, `evaluation_cycles` and `eva_packets`. The other M2 append-only tables already had them from 0005 and are re-tested | 0006 triggers; `guards` | `test_fix6_*`, raw SQL on both dialects |
| 7 | Integer fixed point (×10⁴, half up) for weights, thresholds and confidences in the oracle and both dialects | `consensus.py`, `v_eva_item_consensus` | MC5's exact case (oracle and SQL); random parity under three weightings |
| 8 | Pinning fails closed: only FakeProvider runs unpinned; a provider that cannot report digests refuses; every spec needs a digest the server confirms; unverified is never recorded as verified | `Evaluator.verify_models`, `OpenAICompatibleProvider` | `test_fix8_*` |
| 9 | An unresolved split escalation sends the packet to `needs_review` at any tier | evaluator | `test_fix9_*` (tier 3) |
| 10 | Ruling 5: disputed rows, including disputed agreement, report max(conf_a, conf_b); an accepted agreement keeps the mean | oracle and view | TC-02 and TC-30 now 0.60; unit test |
| 11 | `admit_next` uses the guarded UPDATE with a rowcount check | `queue.admit_next` | `test_fix11_*`, including two concurrent ticks |
| 12 | An exhausted failure budget → audited `awaiting_confirmation` plus a `failure_budget` confirmation. Approve = a fresh budget (failures before the approval stop counting); reject = `failed` | `queue.enforce_failure_budgets`, `confirmations`, `v_queue` | `test_fix12_*` |

### Bugs found while doing this (fixed)
- `approve()` ignored `failure_budget` confirmations: it only loaded the packet for `packet`/`packet_review`.
- The guarded UPDATE wrote the raw status string back onto the ORM object (`'running'` instead of the enum).

### Judgement calls (please check)
1. **Registry binding is per field.** "Current consensus requires the registry revision" is implemented as: the evaPacket's `definition_version` must equal that field's current definition version. Editing *another* field (a new registry revision) does not invalidate every packet's consensus. I read the intent as "evaluated against the definition now in force"; binding the global revision would force re-evaluating everything on any YAML edit.
2. **Exemplars only change the content address** (no reuse); they do not make existing consensus `pending`, because the PM text lists only preset and registry for currency.
3. **`closed_unverified` is a locking terminal status.** It is user-only, from any non-terminal status.
4. **A context mismatch at claim raises `ContextChanged`** (a `ClaimError`) with the audited revert to `evaluating_pre` pending in the session; the caller commits it. At admission the packet is simply skipped.
5. **On SQLite the losing side of a race can surface as a database lock or snapshot error** rather than a `ClaimError` (single writer, WAL). Tests accept either; no claim or admission is ever duplicated. On PostgreSQL the compare-and-set returns 0 rows.
6. **A gate bump skips a locked head** (terminal work has no gate). Overrides are bound by ref, so they bump the lineage head.
7. **Settings are quantised to 4 dp** by the fixed point. Values with more decimals are rounded half up once (validation on write is GE7, deferred to M3).
8. **Failure budgets are enforced by sweeping `ready`/`queued` packets** at each admission tick. Failures of `running` packets arrive with M3's channels.
9. **The transition table gains `queued → awaiting_confirmation`** for fix 12.

### Next steps
- PM verification; merge PR #2 when CI is green.
- M3 backlog: GE7 (validate numeric settings on write).

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
