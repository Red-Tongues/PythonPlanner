# Review R0 — panel consensus and adopted amendments

**Subject:** plan v0.2 + milestone specs M1–M3 (commit 9a6f345) · **Date:** 2026-11-03
**Panel:** GH = GitHub Copilot · MC = Microsoft Copilot (GPT-6.1) · GE = Gemini (Argon) · **Aggregator:** PM agent
**Findings:** [`R0_findings.md`](R0_findings.md)

## Method — the six-pass design applied to the review panel

The panel uses the same evaluation logic the orchestrator will apply to Prompt Packets (PROJECT_PLAN §7):

1. **Extraction (passes 1–2 analogue):** each reviewer independently produced 8 findings from the same material.
2. **Cross- and self-evaluation (passes 3–6 analogue):** each reviewer scored all 24 findings 0–10, its own included, tagged SUPPORT or CONTRADICTION, with a reason.
3. **Deterministic aggregation (the SQL step):** `confidence = (0.6 × mean(cross scores) + 0.4 × self score) / 10`.
   - **ACCEPT**: confidence ≥ 0.70 and no contradiction.
   - **ACCEPT-AMENDED**: confidence ≥ 0.70 with a contradiction. The uncontested part is adopted and the PM reconciles the contested part.
   - **PARTIAL**: 0.50–0.70 with a contradiction. Only the parts that survive the contradiction are adopted.
   - **DEFER**: 0.50–0.70, no contradiction.
   - **DECLINE**: < 0.50.
4. **Key Idea filter:** the PM schedules adopted changes against K1–K5 and the v1 scope.

## Scores

| ID | Self | Cross mean | Contradictions | Confidence | Verdict | Outcome |
|---|---|---|---|---|---|---|
| GH1 | 9 | 7.0 | 1 | 0.78 | ACCEPT-AMENDED | Merged with MC2 → **A1** (lock invariants on INSERT *and* UPDATE) |
| GH2 | 8 | 8.5 | 0 | 0.83 | ACCEPT | **B2** |
| GH3 | 8 | 7.0 | 0 | 0.74 | ACCEPT | **B4** |
| GH4 | 8 | 9.0 | 0 | 0.86 | ACCEPT | **B6** |
| GH5 | 9 | 9.0 | 0 | 0.90 | ACCEPT | **C1** |
| GH6 | 9 | 8.5 | 0 | 0.87 | ACCEPT | **A3**, **C1** |
| GH7 | 6 | 7.5 | 0 | 0.69 | DEFER | Substance adopted through MC5 → **A7**, **C3** |
| GH8 | 6 | 7.0 | 0 | 0.66 | DEFER | Substance adopted through MC8 / GE8 → **A6**, **C6** |
| MC1 | 8 | 7.0 | 0 | 0.74 | ACCEPT | **A4** (migrations), **B6** |
| MC2 | 9 | 8.5 | 0 | 0.87 | ACCEPT | **A1**, **A2**, **C3**, **C7** |
| MC3 | 10 | 9.0 | 0 | 0.94 | ACCEPT | **B1**, **B2** |
| MC4 | 9 | 8.5 | 0 | 0.87 | ACCEPT | **B4**, **B5** |
| MC5 | 9 | 7.0 | 0 | 0.78 | ACCEPT | **A7**, **B8**, **C3** |
| MC6 | 10 | 8.5 | 0 | 0.91 | ACCEPT | **C1**, **C2** |
| MC7 | 8 | 5.5 | 1 | 0.65 | PARTIAL | **C5** (honest labelling; no native consumer in v1) |
| MC8 | 8 | 6.5 | 0 | 0.71 | ACCEPT | **C6**; multi-hour soak + real-model benchmark → M4 |
| GE1 | 9 | 8.0 | 0 | 0.84 | ACCEPT | **B6** |
| GE2 | 9 | 6.0 | 1 | 0.72 | ACCEPT-AMENDED | **A2**: context versioning yes; no re-pointing of children (see reconciliation) |
| GE3 | 10 | 9.0 | 0 | 0.94 | ACCEPT | **B1**, **B2**, **B3** |
| GE4 | 9 | 9.0 | 0 | 0.90 | ACCEPT | **A5**, **C1**, **C2** |
| GE5 | 9 | 8.0 | 0 | 0.84 | ACCEPT | **A3**, **B4** |
| GE6 | 8 | 5.5 | 1 | 0.65 | PARTIAL | **B7**, **C4** (conservative normalisation only) |
| GE7 | 9 | 9.0 | 0 | 0.90 | ACCEPT | **A3**, **B5** |
| GE8 | 8 | 7.0 | 0 | 0.74 | ACCEPT | **A6**, **B7** |

Totals: 18 ACCEPT · 2 ACCEPT-AMENDED · 2 PARTIAL · 2 DEFER (both subsumed) · 0 DECLINE. Mean confidence by author: GH 0.79 · MC 0.81 · GE 0.82.

## PM reconciliation of contested findings

- **GH1 (MC contradiction: "direct-insert invariants still need explicit coverage").** Agreed. The lock invariant is enforced on INSERT as well as UPDATE (A1).
- **GE2 (MC contradiction: "editing locked predecessors or re-pointing historical children undermines immutable lineage").** MC is right. Superseding never edits a locked row and never rewrites `parent_id` / `root_id`. The current head of a lineage is *derived* by a view (`v_packet_heads`) that follows `supersedes_id`. Context versioning for projects/objectives is adopted.
- **GE6 (MC contradiction: "broader normalisation weakens quotation integrity").** Exactness is kept. Normalisation is limited to whitespace, typographic quotes/dashes and markdown emphasis markers. Unverified quotes are excluded from aggregation and never fail the parse. Concise retry messages and fenced code extraction are adopted.
- **MC7 (GE contradiction: "v1 already scopes the workbench to file-drop").** True, but MC7's governance point stands. Complex production is labelled *externally blocked* when no workbench consumer is attached, blocked reasons are visible while other work continues, scans are bound to the exact commit, and the skip/failure policy is defined.

## Adopted amendments

### M1 — kernel foundation (amendments A1–A7, sent to the main engineer during M1)
- **A1 Lock invariants in the DB.** Triggers on INSERT and UPDATE: `status ∈ {completed, archived} ⇒ locked = true`, otherwise reject. A locked row rejects UPDATE/DELETE, and `locked` can never go from true to false. Raw-SQL tests on both databases.
- **A2 Versioned context.** `projects` and `objectives` gain `version`, `supersedes_id`, `locked`. `candidate_field_definitions` and `settings` carry a `version`. Superseding never edits locked rows or re-points children. The `v_packet_heads` view resolves current heads.
- **A3 Schema for later milestones.** `packets`: `depth`, `packet_kind` (`task` / `content_eval`), `manual_boost`, `claimed_by`, `lease_expires_at`, `heartbeat_at`. `objectives`: `priority_level`. `evaluation_passes`: `eval_cycle_id`. `channel_runs`: `state`, `idempotency_key` (unique), `input_hash`, `output_hash`, `policy_revision`, `deadline_at`. Unique constraints where duplicates would mean duplicated work.
- **A4 Versioned migrations.** Alembic (or an equivalent ordered runner with a `schema_version` table) owns tables, triggers, views and seeds, with an upgrade test from an empty DB.
- **A5 SQLite pragmas** on every connection: `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`.
- **A6 Dual-dialect fixture.** The DB fixture is parametrised so every DB/SQL test runs on SQLite *and* on PostgreSQL when `ORCH_TEST_POSTGRES_URL` is set (not only `-m postgres` tests).
- **A7 Strict contracts.** Pydantic models use `extra="forbid"` and strict types; every contract has a version string recorded wherever it is used.

### M2 — evaluation engine (B1–B8)
- **B1 Evaluation cycles.** `eval_cycle_id` binds packet version, context versions, field-definition version, model identities and pass-plan version. Consensus uses only the latest *complete* cycle.
- **B2 Consensus decision table** (documented and truth-table tested on both DBs):
  - agreement accepted only if confidence ≥ 0.60;
  - a disagreement winner needs ≥ 0.70 and a margin ≥ 0.15, otherwise `disputed`;
  - one model NULL → accept the non-null value if its confidence ≥ 0.70, else `disputed`;
  - both NULL → `null`; incomplete cycle → `pending`; ties → `disputed`;
  - acceptance requires ≥ 1 verified SUPPORT quote (except `null`); contradictions > supports → `disputed`;
  - tag sets are scored per tag.
- **B3 Text fields:** 4 passes (P1–P4, cross-checks only); confidence = cross/10.
- **B4 Priority.** All inputs normalised to 0–1 with `COALESCE` defaults. Ageing saturates at `max_age_hours`. *Eligibility* (ready, unclaimed, approvals met, within failure budget) is separate from *rank*. Retry backoff; lifetime failure budget (default 3) → `awaiting_confirmation`. A max-wait rule serves any eligible packet waiting longer than `max_wait_hours` next.
- **B5 Branching.** Idempotent on (packet version, rule id, rule version). `max_branch_depth` = 2, plus a descendant cap. A split trunk moves to `waiting_for_children` and completes by deterministic roll-up. `content_eval` packets never branch or route to generation.
- **B6 View portability.** One documented semantic contract per view, dialect-specific DDL in one module, cross-dialect identical-output tests. No Python fallback for decision-making views.
- **B7 Small-model robustness.** Tolerant output parsing (code fences, preambles); conservative quote normalisation (whitespace, typographic quotes/dashes, markdown emphasis); concise one-line retry corrections; a "noisy" FakeProvider mode.
- **B8 Schemas in prompts.** Compact JSON schema passed to the model; provider capability flags (JSON mode / grammar) with fallback.

### M3 — orchestration & interface (C1–C7)
- **C1 Durable runs.** Persisted run state machine; intent recorded before dispatch; idempotency keys; atomic claims with expiring leases + heartbeats; startup reconciliation of DB, files and git.
- **C2 Non-blocking channels.** Workbench runs wait in `awaiting_inbox` and are checked each tick until `timeout_at`. Per-operation deadlines; one blocked channel never halts the others. Pause = drain. Stale completions are rejected (results and approvals bound to input hash and policy revision).
- **C3 Artifact safety.** Quarantine until checks pass; paths confined under the repo root (no traversal or symlink escape); accepted artifacts bound to git commit SHA + content hash; repeatable writes and commits.
- **C4 Code outputs** as fenced blocks with front-matter, not JSON-escaped strings.
- **C5 Honest autonomy.** With no workbench consumer attached, complex production is shown as *externally blocked* while other work continues. SonarQube scans are bound to commit SHA. Documented skip/failure policy: a failed quality gate flags the content-evaluation packet and raises a confirmation.
- **C6 Adversarial tests.** Fault injection at every durable boundary (e.g. crash after commit, before DB acknowledgement); concurrent claims; late or duplicate inbox results; approval races; branch replay; starvation; retry exhaustion; a short randomised-fault soak run.
- **C7 Trust boundary** (documented in the RUNBOOK). The runtime DB role has no DDL rights on PostgreSQL; generated work never receives DB credentials.

### Deferred to M4
Multi-hour soak and real-model benchmarks (MC8) · native Juggler consumer (MC7, already v1.1) · automated PostgreSQL role provisioning (MC2).
