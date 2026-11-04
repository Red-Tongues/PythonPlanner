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
newline literal). Migration 0005 holds frozen copies; `tests/test_sql_views.py` keeps them equal.

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

## 6. Schema change technique on SQLite (migration 0005)

SQLite cannot alter constraints. Rebuilding `packets` (copy, drop, rename) breaks under
`foreign_keys = ON`: dropping a referenced table leaves deferred violations, and renames rewrite
child foreign keys. 0005 therefore uses SQLite's documented procedure for format-neutral changes:
rewrite `sqlite_master.sql` under `writable_schema` and bump `schema_version`. This is used only
to widen the status CHECK (existing rows stay valid), append defaulted columns at the end of the
column list (what `ADD COLUMN` does) and add FOREIGN KEY clauses (checked by
`foreign_key_check` immediately). No row moves and no trigger or index is touched.
