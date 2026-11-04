# Review R1 — consensus on M1 code (PR #1) and the M1.1 fix list

**Subject:** PR #1 @ 98e4780 (M1 kernel foundation + R0 amendments A1–A7) · **Date:** 2026-11-04
**Panel:** GH = GitHub Copilot · MC = Microsoft Copilot · GE = Gemini (Argon) · **Method:** as R0 — independent findings → every reviewer scores all 24 → `confidence = (0.6 × mean cross + 0.4 × self) / 10` → verdict → PM verification against the code → Key Idea filter.
**CI at review time:** green on all three jobs: SQLite on Python 3.11 and 3.12, and PostgreSQL 16 (commit 1beb37f).

## Scores

| ID | Self | Cross | Contra. | Conf. | Verdict | PM note |
|---|---|---|---|---|---|---|
| GH1 | 9 | 10.0 | 0 | 0.96 | ACCEPT | Fix 1 |
| GH2 | 8 | 9.0 | 0 | 0.86 | ACCEPT | Fix 3; verified: SQLite pragmas lack `recursive_triggers` |
| GH3 | 8 | 5.5 | 1 | 0.65 | PARTIAL | Fix 11 (typed actor; full auth with the M3 GUI) |
| GH4 | 7 | 7.5 | 0 | 0.73 | ACCEPT | Fix 7 |
| GH5 | 9 | 9.5 | 0 | 0.93 | ACCEPT | Fix 4 |
| GH6 | 7 | 6.5 | 0 | 0.67 | DEFER | → M2 (needed before leases and ageing) |
| GH7 | 8 | 6.5 | 0 | 0.71 | ACCEPT | Fix 6 |
| GH8 | 8 | 8.0 | 0 | 0.80 | ACCEPT | Fix 5; verified: 0002 downgrade drops views only |
| MC1 | 10 | 9.0 | 0 | 0.94 | ACCEPT | Fix 4 |
| MC2 | 9 | 0.0 | 2 | 0.36 | **DECLINE** | **False positive, verified:** `current_revision()` runs after the `engine.begin()` block commits |
| MC3 | 10 | 9.0 | 0 | 0.94 | ACCEPT | Fix 1 |
| MC4 | 9 | 4.5 | 1 | 0.63 | PARTIAL | Fix 12 (keep init-db sync per spec; add registry revision) |
| MC5 | 8 | 6.5 | 0 | 0.71 | ACCEPT | Fix 6 |
| MC6 | 10 | 8.0 | 0 | 0.88 | ACCEPT | Fix 2 |
| MC7 | 8 | 6.5 | 0 | 0.71 | ACCEPT | Fix 4 (per-revision assertions) |
| MC8 | 6 | 1.0 | 2 | 0.30 | **DECLINE** | **False positive, verified:** PostgreSQL params carry the `postgres` mark, so `-m postgres` runs every DB-fixture test on PostgreSQL |
| GE1 | 10 | 9.5 | 0 | 0.97 | ACCEPT | Fix 8; verified: explicit `response_contract=` plus `**{**carried, **cleaned}` |
| GE2 | 10 | 9.5 | 0 | 0.97 | ACCEPT | Fix 1 |
| GE3 | 9 | 9.5 | 0 | 0.93 | ACCEPT | Fix 4 |
| GE4 | 8 | 6.5 | 0 | 0.71 | ACCEPT | Fix 5 |
| GE5 | 8 | 7.0 | 0 | 0.74 | ACCEPT | Fix 9 |
| GE6 | 7 | 6.0 | 0 | 0.64 | DEFER | → M2 (idempotent re-seeding once migrations are frozen) |
| GE7 | 8 | 8.5 | 0 | 0.83 | ACCEPT | Fix 2 |
| GE8 | 8 | 7.0 | 0 | 0.74 | ACCEPT | Fix 10 |

Totals: 18 ACCEPT · 2 PARTIAL · 2 DEFER · 2 DECLINE. **The cross-evaluation removed two plausible but wrong findings before they reached the engineer**, which is the behaviour the six-pass design is meant to produce.

## M1.1 fix list (sent to the main engineer; part of PR #1 before merge)

1. **Superseded history is immutable in the database** (GH1, MC3, GE2). Triggers on both dialects reject UPDATE/DELETE of any project, objective or packet that has a successor. The ORM guard checks head status too. Raw-SQL and ORM tests.
2. **Lineage invariants in the database** (MC6, GE7):
   - a successor keeps `ref`, has `version = predecessor + 1` and immutable ancestry (`parent_id`, `root_id`);
   - at most one successor per predecessor (unique `supersedes_id`);
   - deleting a superseding version or a row with dependents is refused ("archive instead").
3. **SQLite `PRAGMA recursive_triggers = ON`** on every connection (GH2), with `INSERT OR REPLACE` tests on locked packets, events and evaluation passes.
4. **Frozen migrations** (GH5, MC1, GE3, MC7). Revisions never import live `ddl`, `seed` or ORM modules: SQL and seed data are frozen inside each revision (`op.execute`, `sa.table` + `op.bulk_insert`), and future changes go in new revisions. Add per-revision assertions of expected triggers and views (normalised definitions or hashes) and exact seeds after an empty → head upgrade, on both dialects.
5. **Downgrade and constraint hygiene** (GH8, GE4). 0002 downgrade drops its triggers and functions. The evaluation-pass uniqueness includes `packet_id` and handles NULL `eval_cycle_id` (NOT NULL or a partial index). Test downgrade → re-upgrade on both dialects.
6. **Atomic ref allocation** (GH7, MC5): a counter table incremented transactionally, with a two-writer test on both dialects.
7. **Evented writes only** (GH4). Direct ORM mutation of domain rows must emit an event (and a version bump for settings) centrally in the flush layer, or be rejected.
8. **`supersede_packet` keyword collision** (GE1) fixed, with a test that changes `response_structure`.
9. **Ingestion degrades gracefully** (GE5): "Untitled task" fallback; over-long descriptions raise `IngestionError` (or are split with a note) instead of an unhandled `ValidationError`.
10. **Missing tests** (GE8): artifact INSERT with `accepted = true` and no `accepted_at`, plus the tests named in fixes 1, 5 and 8.
11. **Typed actor, partial** (GH3). Replace the `user:` string-prefix check with a typed `Actor` (kind: user | system | llm, plus id). `llm` and `system` actors can never complete or archive outside the transition table. Full authentication comes with the M3 GUI.
12. **Registry revision, partial** (MC4). Keep the init-db YAML sync (spec), but record a registry revision hash in the DB with an event on every change; M2 evaluation cycles must reference it.

**Deferred to M2:** UTC-aware datetimes across dialects (GH6) · idempotent re-seeding of all defaults on init-db (GE6).
