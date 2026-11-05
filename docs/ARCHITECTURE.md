# Architecture — the evaluation engine (M2)

Engineer-owned technical reference. The spec of record is `docs/PROJECT_PLAN.md`; this file
explains how the code implements it. Rule of thumb: **LLMs propose, SQL decides, Python records**.

## 1. Flow of one evaluation cycle

```
genPacket version ──► evaluation_cycles (bound to packet/project/objective versions,
                       registry revision, pass plan, model configs)          [B1]
   tier 1 → tier 2 → tier 3 (stop after tier 1 if a gating field is unresolved)   [D1]
      per field: eva_packets (content-addressed; reuse = copy passes)              [B9]
        P1 A-extract, P2 B-extract   ──► all model-A calls, then all model-B calls [D1]
        P3 A→b, P4 B→a, P5 A→a, P6 B→b (text: P3, P4 only)                         [B3]
        each call: prompt (+schema) → tolerant JSON → strict Pydantic → 1 repair  [B7, B12]
        every attempt → channel_runs (QE…); every valid pass → evaluation_passes
        + evaluation_pass_items (per tag / per value, verified evidence counts)
      SQL v_eva_item_consensus decides → eva_packets.state = consensus | disputed
      failed / tier-1 disputed → ONE split-plan re-run (own cycle row) → needs_review  [B12]
   projections packets.tags / scores  (UPDATE … WHERE input_hash = :target)        [B9]
   readiness → ready (+ promotion) | needs_review (+ approval-queue entry)
```

Stale-write guard: before every write the packet must still be the head with the same
`input_hash`; otherwise the evaPacket becomes `superseded` and nothing more is written.

## 2. Consensus decision table (B2, B10 rulings 1–8, D5)

All thresholds come from `settings` (`aggregation.*`, `consensus.*`). Scores are integers 0–10.
**Arithmetic is integer fixed point scaled by 10⁴ (M2.1 fix 7):**
- weights and thresholds are quantised once, half up (`0.6 → 6000`);
- `conf = (cw·cross + sw·self + 5) / 10` with integer division (half up); text fields use `cross · 1000`;
- the agreement mean is `(a + b + 1) / 2`.

All comparisons are between integers, identically in SQLite, PostgreSQL and the oracle (review finding MC5: REAL rounding had diverged at a boundary). The `confidence` columns are the `*_e4` integers divided by 10000, for display only.

| Input (per item: a scalar field, or one tag) | Status | Reported confidence | Final value |
|---|---|---|---|
| an applicable pass is missing | `pending` | NULL | – |
| both candidates NULL (tag absent from both) | `null` | 0 | – |
| agreement, mean(conf_a, conf_b) ≥ `consensus.agreement_min` (0.60), evidence ok | `accepted` | mean | the value |
| agreement below 0.60 or evidence not ok | `disputed` | max(conf_a, conf_b) | – |
| one NULL, other's conf ≥ `consensus.one_sided_min` (0.70), evidence ok | `accepted` | that conf | that value |
| one NULL otherwise | `disputed` | that conf | – |
| disagreement, winner ≥ `consensus.winner_min` (0.70) and margin ≥ `aggregation.dispute_margin` (0.15), evidence ok | `accepted` | winner's | winner's value |
| disagreement otherwise, or an exact tie | `disputed` | max(conf_a, conf_b) | – |

* `conf_a = (cross_w·P4 + self_w·P5)/10`, `conf_b = (cross_w·P3 + self_w·P6)/10` (0.6 / 0.4);
  text fields: `conf_a = P4/10`, `conf_b = P3/10`.
* Passes that would evaluate a NULL value are skipped; the cycle is complete when every
  *applicable* pass is done (ruling 2).
* **Evidence** counts verified quotes only, from the passes that evaluate the winning value
  (P4+P5 for A, P3+P6 for B, P3–P6 on agreement, none on a tie; text: P4 / P3). OK means
  supports ≥ `consensus.min_supports` (1) and contradictions ≤ supports.
* Tag sets: one row per vocabulary tag (ruling 4, D5 point 2).
* Ruling 5 (M2.1 fix 10): every disputed row, including a disputed agreement, reports
  max(conf_a, conf_b); only an *accepted* agreement reports the mean.

The same table is implemented twice: `orchestrator.evaluation.consensus.decide` (pure Python,
the reference oracle) and the view `v_eva_item_consensus`. `tests/test_truth_table.py` (D2
TC-01…52) and `tests/test_consensus_parity.py` (randomised) require them to agree row for row on
SQLite and PostgreSQL.

## 3. Readiness (B9, D1)

`orchestrator.evaluation.readiness.assess_readiness` is a pure function and the only authority.
It runs after a cycle, at admission (`ready → queued`) and at the atomic claim
(`queued → running`). Per required tier-1/2 preset field: missing/pending blocks (not waivable);
accepted and within the preset's value gate passes; otherwise a **hard** field blocks forever
and a **soft** field blocks unless a waiver for this exact version exists and is not revoked.
Then typed dependencies and pending approvals. Tier-3 fields never gate. Queue overrides are not
readiness: BLOCK/HOLD stop admission; FORCE/PIN/REORDER change order only.

Completion by the kernel (`evaluating_post → completed`) requires the post-phase evaluation set
to pass the same rules (and to target the newest artifact's hash when one exists);
`waiting_for_children → completed` requires every child to be completed (roll-up).

**M2.1:**
- **Completion gate for everyone (fix 5).** The gate applies to every actor, users included.
  Completing needs the newest artifact and a passing post-phase evaluation of exactly that
  artifact (fix 3). Hard gates are never bypassed; soft fields pass only through a waiver bound to
  this version and the post phase. A person who closes work without that evidence records
  `closed_unverified` (terminal, locked, never a success).
- **Gate revision (fix 2).** Every governance write that can change readiness bumps the packet's
  `gate_revision` in the same transaction: waivers, overrides, revocations, dependencies, and
  confirmations created or decided.
- **Atomic admission and claim (fixes 1, 2, 11).** Admission and claim read the revision first,
  then re-check context, overrides and readiness, and build the snapshot. One guarded UPDATE then
  compares status, version, gate revision, input hash and the project/objective heads (claim: also
  the lease). A concurrent writer therefore makes the admission or claim lose; it never runs on
  stale gates.
- **Context binding (fix 1).** The input fingerprint is recomputed from the current packet,
  project and objective heads at every evaluation write, at admission and at claim. A mismatch
  sends the packet back to `evaluating_pre`, with events.
- **Failure budget (fix 12).** Exhausting the lifetime failure budget moves the packet to
  `awaiting_confirmation` (audited) with a `failure_budget` confirmation. Approving grants a fresh
  budget: `v_queue` stops counting failures before the approval. Rejecting marks the packet
  `failed`.
- **Pinning fails closed (fix 8).** Every non-fake evaluator must be pinned by digest and verified
  by a provider that can report digests; otherwise evaluation is refused and evented. The
  FakeProvider is recorded as `evaluation.fake_provider`, never as verified.

## 4. View contracts (B6)

Booleans are INTEGER 0/1; confidences NUMERIC(·,4) on PostgreSQL and REAL on SQLite (compare
with `ROUND(…, 4)`); timestamps are UTC. Views carry no ORDER BY unless stated; callers order.
Every view exists with the same columns on both dialects; dialect differences are tokens in
`orchestrator/sql/views.py` (JSON array explode, hour difference, timestamp comparison,
newline literal). They are part of the master schema (§6); the baseline migration carries the generated text.

| View | One row per | Columns (NULL semantics) |
|---|---|---|
| `v_eva_progress` | evaPacket | `done1..done6`, `a_present`/`b_present` (P1/P2 produced a value), `is_text`, `complete` (every applicable pass done) |
| `v_eva_item_consensus` | evaPacket × item key | `value_a`/`value_b` (NULL = no value), `conf_a`/`conf_b` (NULL when that value is NULL), `agreement`, `status`, `confidence` (NULL only when pending), `supports`/`contradictions`/`unverified` (winning scope; 0 when pending), `winning_value` |
| `v_field_consensus` | head packet × field × phase × item | as above for the most relevant evaPacket of the same packet row (not superseded). It is *current* only when all of these hold (M2.1 fixes 3, 4): its target is `packets.input_hash` (pre phase) or the sha256 of the packet's newest artifact (post phase); its preset is the active revision; its `definition_version` is the field definition's current version. Selection order: current, then complete, then highest escalation, then newest. A non-current choice reports `pending` with NULL confidence and value; `final_value` is NULL unless accepted and current |
| `v_field_status` | head packet × field × phase | `status`: pending > disputed > null (nothing accepted) > accepted; item counts; `min_confidence` |
| `v_packet_stats` | packet row | prompt word count (space-separated, approximate), evaPackets, QE attempts, evaluation ms, lifecycle hours (to completion or now), accepted scores ≥ `stats.score_threshold`, evidence items, generation ms (0 until M3) |
| `v_branch_stats` | trunk lineage with descendants | first creation time, descendant count, score variance (population), revision count, failed runs, contradictions, distinct models |
| `v_branch_model_usage` | trunk × model | passes |
| `v_branch_triggers` | head packet × fired enabled threshold | action, precedence, matched items, first matching field |
| `v_ready_candidates` | unlocked head packet | counts of required fields by status, active waivers, unmet dependencies, pending approvals — **explanatory only** |
| `v_queue` | head packet in `ready`/`queued` | `eligible`, `ineligible_reason` (blocked > held > awaiting_approval > failure_budget > backoff > claimed), `queue_class` (1 force, 2 pin, 3 max-wait, 4 normal), `priority` (6 dp), `age_hours`, `failed_runs`, `position` (1…n over eligible rows, ORDER BY class, priority DESC, created_at, ref; NULL when ineligible) |

`priority = w_objective·objective/10 + w_age·min(age, max_age)/max_age + w_ambiguity·(10 − COALESCE(ambiguity, 5))/10 + w_boost·boost/10 − w_failures·min(failed, budget)/budget + Σ REORDER deltas`.
`orchestrator.queue.queue_rows(now=…)` runs the identical query with a bound `now` (simulated time).

## 5. Pass plans and escalation (D3, B12)

`orchestrator/evaluation/pass_plans.yaml` is data: `combined` (one call per evaluation pass,
canonical) and `split` (accuracy and evidence prompts). Presets name a plan, so a field class can
be pre-routed to `split` by adding a preset revision. A combined call gets one targeted repair
(`evaluation.repair_attempts`); a field that still fails, or ends disputed on tier 1, is re-run
once (`evaluation.max_escalations`) with the split plan and `attempts.quickfire_evaluation`
attempts per call; still unresolved → `needs_review` plus an approval-queue entry.

## 6. One master schema, one generated baseline (CFO decision Q4)

The schema is defined in one place, per kind of object:

| Object | Source | Notes |
|---|---|---|
| Tables, columns, constraints, indexes, server defaults | `db/models.py` | SQLAlchemy 2.0; `compare_metadata` checks the built database against it |
| Triggers (and PostgreSQL trigger functions) | `db/ddl.py` | All dialect-specific DDL (golden rule 7): immutability, lineage, append-only, frozen bindings, transition tables |
| Views | `sql/views.py` | Portable text; dialect differences are tokens |
| Assembly | `db/schema.py` | `schema_statements(dialect)`: tables (dependency order, indexes by name), then triggers, then views |
| Seed rows | `db/seed.py` | Not schema; inserted by `ensure_defaults` on every `init-db` |

`migrations/versions/0001_baseline.py` is **generated** (`python scripts/generate_baseline.py`)
as literal DDL and imports nothing from the application. `tests/test_schema.py` fails when the
baseline and the master schema differ, checks the installed triggers, views and functions, and
round-trips a downgrade. A database with history (any `events` row) refuses to downgrade,
because dropping tables would erase the append-only audit log in one statement.

The baseline replaced migrations 0001–0007 while there was no live data. It was compared object by
object with the old head: identical on SQLite; on PostgreSQL the only differences are
`control_state.id` without a sequence (single row, id always explicit), the status CHECK listing
values in another order, and `v_ready_candidates` now including `fencing_token` and
`active_run_id` (its `p.*` was expanded in 0006, before those columns existed). Once a database
must be kept, the baseline is frozen and changes become new revisions.

## 7. Packet and run state machines (M3 C23)

The tables below are **generated** from `orchestrator/transitions.py`, the single guard every
status change passes (`lifecycle.transition` for packets, `orchestrator.runs` for runs).
The union of allowed (from, to) pairs is carried into triggers on both dialects (`db/ddl.py`), so a
pair not listed is rejected by the database too; `tests/test_transitions.py` checks every
unlisted pair for every actor kind. Starting point: `docs/reviews/D8_state_machines.md` with its
errata E1–E11 (binding). Actor *kernel* = a `system` actor (D8's kernel and governor); *user* =
a `user` actor (an owner decision); `llm` actors never move a packet or a run.

**Notation** (as D8): `G` gate revision, `F` fencing token, `H` input hash (context
fingerprint), `M` manifest hash. A core edit, interrupt supersession or locked-match supersede
never moves a row: it creates a new version row, and the old row keeps its state.

**States.** Packets: C11's set plus `waiting_for_children`, `awaiting_confirmation` and
`closed_unverified` (terminal, locked, never a success, never satisfies a dependency, needs a
reason). `archived`, `evaluating`, `in_progress` and `processed` are legacy (see P3, P4).
Runs: `planned → dispatched → awaiting_result → validating → accepted`, plus `failed`, `stale`
and `awaiting_confirmation` (exit 3); `claimed`, `succeeded`, `abandoned` are legacy M2 values.
Terminal runs (`failed`, `stale`, legacy) and an `accepted` run whose commit is recorded never
change (DB-enforced).

<!-- transition-tables:begin (generated by scripts/transition_tables.py) -->

#### 7.1 Packet transitions

| From → To | Actor | Trigger | Guard | Dev. |
|---|---|---|---|---|
| `awaiting_confirmation` → `blocked` | user | BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `awaiting_confirmation` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `awaiting_confirmation` → `completed` | user | optional-scan approval | E5 completion guard; the approval covers only that gate ERROR, never a hard constraint; locks |  |
| `awaiting_confirmation` → `evaluating_post` | user | repo_write / optional-scan approval | E4: repo_write needs the full publication guard of running → evaluating_post (R1 for the targeted run); a mismatch stales the run |  |
| `awaiting_confirmation` → `evaluating_pre` | kernel / user | approval but H stale (C25); candidate-promotion approval (E1) | targets the current version and context; promotion and C25 gate bump atomic; readiness recomputed; a stale approval does not apply |  |
| `awaiting_confirmation` → `held` | kernel / user | rejection (any type); HOLD (user) or alignment hold (governor) | reason required; E2: an exit-3 rejection keeps the run parked |  |
| `awaiting_confirmation` → `queued` | user | failure-budget or 48 h approval | H current |  |
| `awaiting_confirmation` → `running` | user | exit-3 approval | R1; then the shared dispatch guard (E3); resumes the same conversation exactly once |  |
| `awaiting_confirmation` → `waiting_for_children` | user | split approval | E10 split guard; the confirmation is validated and C25 applied |  |
| `blocked` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `blocked` → `evaluating_post` | kernel | required scanner available again (D10 Q14) | only a post-phase scanner blocker (E6), no BLOCK/HOLD; the output's M and commit SHA are already recorded; the scan is re-requested for that SHA | P6 |
| `blocked` → `evaluating_pre` | kernel / user | last BLOCK revoked / blocker cleared; no HOLD | — |  |
| `blocked` → `held` | user | last BLOCK revoked, HOLD remains | — |  |
| `draft` → `blocked` | user | BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `draft` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `draft` → `evaluating_pre` | kernel | tick step 2 | H = current fingerprint (B9, C25) |  |
| `draft` → `held` | kernel / user | HOLD (user) or alignment hold (governor) | no active BLOCK |  |
| `evaluating` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `evaluating` → `evaluating_pre` | kernel | legacy M1 row re-enters evaluation | — | P3 |
| `evaluating_post` → `awaiting_confirmation` | kernel | optional-scan ERROR | — |  |
| `evaluating_post` → `blocked` | kernel | required scanner unavailable, or execution/report retrieval failed | E6 |  |
| `evaluating_post` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `evaluating_post` → `completed` | kernel / user | completion | E5: every required post field complete, accepted and current for M from C29-verified evaluators; scan bound to the commit SHA; quality policy (C30); all hard gates; no hold or block; locks | P2 |
| `evaluating_post` → `held` | kernel | alignment < 7,500 (governor) | — |  |
| `evaluating_post` → `needs_review` | kernel | post-eval failed / returned required-scan gate ERROR | E6: a gate ERROR bound to the commit SHA also raises a confirmation |  |
| `evaluating_pre` → `awaiting_confirmation` | kernel | promotion/branch | scope = confirm |  |
| `evaluating_pre` → `blocked` | user | BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `evaluating_pre` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `evaluating_pre` → `held` | kernel / user | HOLD (user) or alignment hold (governor) | no active BLOCK |  |
| `evaluating_pre` → `needs_review` | kernel | evaluation | tier-1 dispute, B12, ambiguity |  |
| `evaluating_pre` → `ready` | kernel | readiness | required tiers accepted or soft-waived; no hard failure; dependencies; approvals; ambiguity ≤ 4,000; H current |  |
| `evaluating_pre` → `waiting_for_children` | kernel | split | E10: depth < 2; descendant and fan-out caps; atomic capacity reservation (C36); B5 idempotency on (packet id + version, rule id, rule version); branching scope auto |  |
| `failed` → `awaiting_confirmation` | kernel | failure budget exhausted | one confirmation |  |
| `failed` → `blocked` | user | BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `failed` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `failed` → `evaluating_pre` | kernel | fingerprint or readiness changed | H ≠ current or not ready; the kernel commits (M2.1 ruling 4) |  |
| `failed` → `held` | kernel / user | HOLD (user) or alignment hold (governor) | no active BLOCK |  |
| `failed` → `queued` | kernel | retry | within budget and backoff, or config changed; H current |  |
| `held` → `blocked` | user | BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `held` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `held` → `evaluating_pre` | user | last HOLD revoked / owner clears hold | never automatic |  |
| `in_progress` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `in_progress` → `evaluating_pre` | kernel | legacy M1 row re-enters evaluation | — | P3 |
| `needs_review` → `blocked` | user | BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `needs_review` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `needs_review` → `evaluating_post` | user | soft waiver (post) | G bumped |  |
| `needs_review` → `evaluating_pre` | user | soft waiver (pre) or definition/exemplar edit | G bumped |  |
| `needs_review` → `held` | kernel / user | HOLD (user) or alignment hold (governor) | no active BLOCK |  |
| `processed` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `processed` → `evaluating_pre` | kernel | legacy M1 row re-enters evaluation | — | P3 |
| `queued` → `awaiting_confirmation` | kernel | failure budget / 48 h touch | one confirmation |  |
| `queued` → `blocked` | kernel / user | WP routing with no consumer (C5); BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `queued` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `queued` → `evaluating_pre` | kernel | fingerprint or readiness changed | H ≠ current or not ready; the kernel commits (M2.1 ruling 4) |  |
| `queued` → `held` | kernel / user | HOLD (user) or alignment hold (governor) | no active BLOCK |  |
| `queued` → `running` | kernel | claim | E9: H recomputed, then the full readiness predicate; guarded UPDATE (status, version, H, G, lease); not paused (C26); budget reserved (C36); snapshot written and F incremented in the same transaction |  |
| `ready` → `awaiting_confirmation` | kernel | branch split confirmation; failure budget | one version-bound approval | P1 |
| `ready` → `blocked` | user | BLOCK, incl. branch kill (D9-R1: a branch-scope BLOCK, reversible) | G bumped in scope (C35) |  |
| `ready` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `ready` → `evaluating_pre` | kernel | fingerprint or readiness changed | H ≠ current or not ready; the kernel commits (M2.1 ruling 4) |  |
| `ready` → `held` | kernel / user | HOLD (user) or alignment hold (governor) | no active BLOCK |  |
| `ready` → `queued` | kernel | admit_next | E9: H recomputed, then the full readiness predicate (C12); guarded UPDATE on status, version, H, G; no BLOCK/HOLD; within failure budget |  |
| `ready` → `waiting_for_children` | kernel | split | E10: depth < 2; descendant and fan-out caps; atomic capacity reservation (C36); B5 idempotency on (packet id + version, rule id, rule version); branching scope auto |  |
| `running` → `awaiting_confirmation` | kernel | exit 3 / repo_write = confirm | — |  |
| `running` → `blocked` | kernel | execution check under NoRunner (C31) | — |  |
| `running` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `running` → `evaluating_post` | kernel | write + commit | publication guard: run accepted; run F = packet F; G, H match; expected HEAD; C32 ownership and base hash; write authorised; idempotent write + commit; M and commit SHA recorded first |  |
| `running` → `evaluating_pre` | kernel | stale run | E7: only if the packet's active run ID and F match this run, it is still running, and no hold, block, pause or confirmation applies (V2) |  |
| `running` → `failed` | kernel | run failed | F matches |  |
| `waiting_for_children` → `closed_unverified` | user | explicit owner close (branch kill is a BLOCK, not a close: D9-R1) | reason required; locks; in-flight runs staled |  |
| `waiting_for_children` → `completed` | kernel / user | roll-up | ≥ 1 child and all children completed (closed_unverified does not count); locks | P2 |

#### 7.2 Run transitions (`generation_runs.state`)

| From → To | Actor | Trigger | Guard | Dev. |
|---|---|---|---|---|
| `accepted` → `stale` | kernel | F/G/H change (E8), superseded version (E8), HEAD moved, modifies base mismatch | only before the commit is recorded (R4); publication prohibited; only unused reservations released |  |
| `awaiting_confirmation` → `dispatched` | user | approval | R1 (run G = packet G, both + 1), then the E3 dispatch guard; resumes once |  |
| `awaiting_confirmation` → `failed` | user | cancellation after rejection | no budget charge |  |
| `awaiting_confirmation` → `stale` | kernel | non-approval governance change or H change | E2: the rejection of this run's own exit-3 confirmation is excluded (the run stays parked); later unrelated changes still stale it |  |
| `awaiting_result` → `awaiting_confirmation` | kernel | exit 3 | resume identity; no counters |  |
| `awaiting_result` → `awaiting_result` | kernel | exit 1/4 or deadline expiry (E11) with attempts left | E3 dispatch guard before the retry effect; new channel_runs row |  |
| `awaiting_result` → `failed` | kernel | exit 2; or exit 1/4 / deadline expiry (E11) on the last attempt | exit 2: channel paused, no charge; E11: failed_runs + 1 exactly once per operation ID |  |
| `awaiting_result` → `stale` | kernel | F superseded, G or H mismatch, packet closed/held/blocked | releases unused reservations |  |
| `awaiting_result` → `validating` | kernel | exit 0 before the deadline | F and lease valid |  |
| `claimed` → `stale` | kernel | legacy M2 claim (no fencing token) | — | R1 |
| `dispatched` → `awaiting_result` | kernel | payload written / process launched | deadline |  |
| `dispatched` → `dispatched` | kernel | deadline expiry (E11) with attempts left | timeout recorded once per operation ID; the retry goes only through the E3 guard |  |
| `dispatched` → `failed` | kernel | exit 2; or exit 1/4 / deadline expiry (E11) on the last attempt | exit 2: channel paused, no charge; E11: failed_runs + 1 exactly once per operation ID |  |
| `dispatched` → `stale` | kernel | F superseded, G or H mismatch, packet closed/held/blocked | releases unused reservations |  |
| `planned` → `dispatched` | kernel | adapter dispatch | dispatch guard (E3): version, F, G, H and lease revalidated; authorised atomically against pauses (C26); capacity reserved (C36); operation ID and first channel_runs row before any effect |  |
| `planned` → `stale` | kernel | F superseded, G or H mismatch, packet closed/held/blocked | releases unused reservations |  |
| `validating` → `accepted` | kernel | checks pass | F, G, H match; compute M |  |
| `validating` → `awaiting_result` | kernel | validation failed, attempts left | error fed back; E3 dispatch guard before the retry effect |  |
| `validating` → `failed` | kernel | last attempt failed, or NoRunner gate | — |  |
| `validating` → `stale` | kernel | F superseded, G or H mismatch, packet closed/held/blocked | releases unused reservations |  |

<!-- transition-tables:end -->

### 7.3 Deviations from D8 (each with its reason)

| # | Deviation | Reason |
|---|---|---|
| P1 | `ready → awaiting_confirmation` (kernel) is kept, for the failure budget and for a branch-split confirmation (D8 gap-fills) | **Failure budget:** M2.1 #12's sweep parks exhausted packets found in `ready` or `queued`. A packet can reach `ready` with an exhausted budget (`failed → evaluating_pre → ready`); without this row it would sit in `ready`, never admitted and never surfaced to the owner. **Split confirmation (R4):** split triggers fire on `ready` packets, and D8's split approval row (`awaiting_confirmation → waiting_for_children`) needs the packet in `awaiting_confirmation`; D8 lists only `evaluating_pre → awaiting_confirmation`. Either way it only parks; it never advances. A rejected split goes to `held` (D8 row 61), never straight back to `ready` |
| P2 | `evaluating_post → completed` and `waiting_for_children → completed` also allow a **user** actor | Keeps M2.1 fix 5's `orchestrator packet complete`: the identical completion gate applies to users, so this grants no extra authority. Completing from any other status (M2.1 allowed it from any non-terminal status with evidence) is now rejected, per D8 §4 #3 |
| P3 | Legacy M1 statuses `evaluating`, `in_progress`, `processed` keep one exit, `→ evaluating_pre` (kernel), plus `→ closed_unverified` (user) | Old rows must stay valid (C22) and must be able to re-enter the governed pipeline. They re-enter from scratch rather than resuming (`in_progress → running` would bypass the claim and fencing) |
| P4 | `archived` is terminal with **no entries** | D8 has no `archived`; `closed_unverified` (with a reason) replaces it. Existing archived rows stay valid and locked |
| P5 | New version rows start in `draft`, not `evaluating_pre` | M2 behaviour (`supersede_packet`); `draft → evaluating_pre` follows on the next tick under the same guard (H current), so nothing is evaluated earlier or skipped |
| P6 | `blocked → evaluating_post` (kernel) when the required scanner is available again | D10 ruling Q14: recovery from the E6 post-phase blocker. Only a scanner blocker qualifies; the recorded manifest and commit are unchanged, so post-evaluation resumes for the same M |
| R1 | Legacy run state `claimed` may only go `stale` | An M2 claim has no fencing token or operation ID, so it can never dispatch or publish under C26 |
| — | `dispatched → dispatched` (E11) is listed as a self-transition | A deadline expiry with attempts left is a new attempt row, not a state change; the row documents that the retry passes the E3 guard |

**D10 rulings that shape these tables.** Q4: pause/resume are controls serialised through the
scope rows (C26), not C25 governance writes, so they never bump G. Q5: rejecting an exit-3
confirmation bumps the packet's and the run's G together (as R1), so the run stays parked and
matched; the owner later cancels (`awaiting_confirmation → failed` for the run, packet stays
`held`) or reconsiders through a new confirmation that follows R1. Q14: post-phase recovery is
`blocked → evaluating_post` (P6) and `needs_review → evaluating_post` (phase-post soft
waiver); run cancellation leaves the packet `held`. Everything else stays rejected.

### 7.4 Errata E1–E11

E1 → `awaiting_confirmation → evaluating_pre` for a user (promotion approval). E2 → an exit-3
rejection holds the packet and leaves the run parked (`awaiting_confirmation → stale` excludes
it; only explicit cancellation fails the run). E3 → every first dispatch, retry and resume passes
one dispatch guard (`orchestrator.runs.authorise_dispatch`). E4, E5, E6 → the publication,
completion and scan guards on the rows named. E7 → the stale reset needs the packet's active
run ID and F. E8 → `accepted → stale` also on an H mismatch or a superseded version. E9 →
admission and claim run the full readiness predicate after recomputing H (already so in M2.1).
E10 → split guards on both split rows. E11 → deadline expiry is a timeout failure, recorded once
per operation ID.

**D9-R1 (PM ruling, 2026-11-04).** Branch kill is a BLOCK override at branch scope: the
packets move to `blocked` and the kill can be undone by revoking it. Closing
(`→ closed_unverified`) is only ever a separate, explicit owner action with a reason.

## 8. Governance atomicity, fencing and exclusivity (M3 C25, C26)

| Mechanism | Where | How it holds under concurrency |
|---|---|---|
| **Scope gate bump** (C25) | `gates.bump_scope` | One UPDATE increments `gate_revision` of every non-terminal head packet in the scope (global, project, objective, branch = trunk ref and its descendants, packet), in the writer's transaction, with one batch event. Writers: dependencies, waivers, overrides, revocations (packet scope); confirmation decisions (packet, or project for ingestion); autonomy scopes and settings (global); project/objective supersession (that scope). Pause/resume are *not* gate writes (D10 Q4) |
| **Claim fencing** (C26) | `queue.claim` | The guarded UPDATE also compares `packets.fencing_token` and sets it + 1 and `active_run_id`; the run row is inserted `planned` with that F, the observed G, the lease and the frozen snapshot, in the same transaction |
| **Bindings** (C25, E7, E8) | `runs.binding_mismatch` | Head version, owned by this run, same F, same G, same and still-current H, lease (not for a parked run). Checked before every dispatch, acceptance, publication and approval |
| **Dispatch guard** (E3) | `runs.authorise_dispatch` | Kernel lock fenced → bindings → every governing scope touched with `UPDATE … WHERE status = 'normal'` in a fixed order → capacity reserved → immutable attempt row with a unique `operation_id` → run state moved by compare-and-set. Commits before any adapter effect |
| **Pause** (C26, C33) | `control.pause` / `resume` | Flips the scope row's status (revision + 1, evented). A dispatch that touched the row first holds its lock until commit, so the pause waits and the dispatch drains; a pause that committed first makes the dispatch match no row (`DispatchRefused`). Only an owner resumes |
| **Kernel lock** (C26) | `control.KernelLock` | A `kernel_leases` row taken by conditional UPDATE (free or expired → holder, epoch + 1). `fence` re-checks holder, epoch and expiry with another conditional UPDATE inside each guarded transaction, so a taken-over owner is fenced out before it writes |
| **Parked runs** (R1, E2, Q5) | `runs.park_run` / `approve_run` / `reject_run` / `cancel_run` | Creating, approving or rejecting the run's own confirmation moves packet G and run G together (CAS on both from equal values); any other governance write leaves them unequal and the run goes stale. Approval resumes the same run once through the dispatch guard; rejection holds the packet and leaves the run parked; only an explicit cancel fails it (no budget charge) |
| **Decisions** | `confirmations.decide_once` | Every confirmation is decided by `UPDATE … WHERE status = 'pending'`: of two concurrent decisions exactly one wins |
| **Outcomes** | `runs.record_outcome` | Append-only, unique per attempt; a report for an obsolete F is recorded as `stale` evidence and revives nothing |
| **Kernel claim errors** (rulings 4–5) | `runs.try_claim` | A context mismatch at claim is committed; a lost claim (including SQLite's lock error) rolls back and is retried next tick |

The C38 two-session scenarios for these pieces are in `tests/test_concurrency.py`; they assert the
expected number of effects (committed, authorised attempt rows): one for an authorised
operation, zero for one refused by governance, pause, fencing or budget, never two.
