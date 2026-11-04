# D4 — Migration map for B9 (GitHub Copilot, grounded in the M1 code, 2026-11-04)

> **Numbering:** GitHub Copilot wrote "0004", but M1.1 already shipped revision 0004 (governance fixes). **B9's migration is 0005.**

## Five SQLite/PostgreSQL risks
1. **Foreign keys on populated `evaluation_passes`.** Existing `eval_cycle_id` values may have no cycle rows. Backfill compatible legacy cycles (or preserve legacy rows) before adding the FK.
2. **SQLite table rebuilds.** New CHECKs, FKs and `UNIQUE(id, version)` on `packets` may force a batch rebuild. Verify that indexes, *existing triggers* and FKs survive; Alembic batch mode differs from PostgreSQL's ALTER TABLE.
3. **Content addressing and NULLs.** Both databases allow multiple NULLs in unique keys, so make every new-work key column NOT NULL. JSON handling differs between dialects; test idempotency with real data on both.
4. **Trigger portability.** SQLite row triggers with `RAISE(ABORT, …)` versus PostgreSQL PL/pgSQL functions (which can also block TRUNCATE). Implement and fingerprint both, including append-only tables and the ban on new `content_eval` rows.
5. **Claim races.** Recheck readiness atomically at claim, with input hash and packet version in the guarded UPDATE. Test "edit just before claim" and concurrent claims on both dialects.

## Suggested commit sequence
1. Schema/contract design: columns, enum vocabularies, override scopes, legacy `content_eval` policy. Record any unresolved choices first.
2. Migration-only commit: frozen **0005** with dialect DDL, safe backfills, indexes, FKs, triggers. Never edit 0001–0004; update `tests/migration_manifest.json` only through the documented regeneration flow.
3. ORM and lifecycle: `models.py`, `contracts/enums.py`, `lifecycle.py`, `LOCKING_STATUSES`, session guards.
4. Evaluation and readiness: cycles, evaPacket creation, content-hash reuse, consensus projections, waivers, pure-Python readiness at admission and claim.
5. Queue and generation: override precedence BLOCK > HOLD > FORCE > PIN > REORDER > NORMAL; FORCE never bypasses gates; frozen `generation_runs.evaluation_snapshot`.
6. Cross-dialect tests and docs: populated-M1 upgrade, legacy mappings, stale writes, revocation, transitions, immutability, content-addressed uniqueness, claim races, B9/B10 cases.
