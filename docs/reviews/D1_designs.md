# D1 — three independent designs for genPackets / evaPackets (condensed)

Brief: `D1_brief_gen_eva_packets.md`. Designers: **MC** Microsoft Copilot · **GE** Gemini (Argon) · **GH** GitHub Copilot (grounded in the M1 code).

## MC — Microsoft Copilot
- **Data model.** Separate `gen_packets` (versioned, `content_hash`, `state`) and `eva_packets` (immutable evaluation job for one genPacket *version* × one `eva_template`, with `input_hash`, `cycle_id`, `state`, `attempt_count`).
  - `eva_templates`: field key, prompt version, schema version, required, threshold, active.
  - `evaluation_passes`: append-only, with `eva_id`, raw and validated output.
  - `eval_consensus`: deterministic adjudication result.
  - `gen_packet_fields`: a replaceable projection of accepted consensus.
  - `dependencies`; `queue_overrides` (append-only, with `expires_at` / `revoked_at`).
  - `generation_runs.evaluation_snapshot` freezes the tags and scores used for each execution.
- **genPacket lifecycle:** draft → evaluating → ready → queued → running → completed → locked, plus held / blocked / failed. Content revision sends it back to evaluating; editing a running version creates a successor.
- **evaPacket lifecycle:** planned → queued → running → done, plus retry_wait / failed / cancelled / superseded. `done` means adjudicated, not necessarily agreed; disputes are valid outcomes.
- **Readiness** (Python predicate, re-checked at queue admission *and* worker claim). The genPacket needs:
  - valid core schema, completed dependencies, no block, approvals present;
  - for each required template: a current-hash consensus that is not disputed and is at or above threshold, and passes the field constraints.
  - Missing evaluation ≠ low confidence. **Human force cannot bypass hard safety, schema or dependency constraints.**
- **Queue:**
  - Precedence: BLOCK > HOLD > FORCE (highest eligible class; cannot bypass readiness) > PIN > REORDER > NORMAL.
  - Score = 0.35 objective priority + 0.25 dependency-unlock value + 0.20 capped age + 0.10 feasibility + 0.10 project balance; sorted by class, manual position, score, `ready_at`, id.
  - One normal admission is reserved per N forced/pinned admissions. Overrides are append-only events with actor, reason and expiry.
- **Scheduling.** Evaluate only what readiness needs: deterministic checks → blocking fields (ambiguity, dependencies, approvals) → routing fields → optional fields.
  - Unique (gen, template, input_hash); one active cycle per genPacket version; two concurrent model calls by default.
  - Batch by model; stop downstream evaluation when a blocking field fails.
  - Budget exhaustion pauses scheduling and never fakes readiness.
- **Risks:** stale evidence, false confidence from model disagreement, overrides undermining governance. **Invariant: no genPacket executes unless its exact version has a complete, current, validated, auditable evaluation set.**

## GE — Gemini (Argon)
- **Data model.**
  - `gen_packets`: `input_hash` and `output_hash`, plus a materialised `eval_fields` JSON (value, confidence, cycle).
  - `eva_presets`: `pre_gen` / `post_gen` phase, Pydantic schema, **tier 1 = gating, tier 2 = routing**, prompt template.
  - `eva_packets`: one per (genPacket, `target_hash`, preset) — this replaces `eval_cycle_id`; holds the consensus value, confidence and disputed flag; `UNIQUE(gen_packet_id, target_hash, preset_key)`.
  - `evaluation_passes`: append-only, pass types `extract_A/B`, `self_A/B`, `cross_AB/BA`.
  - `queue_overrides`: FORCE / PIN / HOLD / BLOCK, `manual_rank`, `expires_at`, **`target_version`**.
- **genPacket lifecycle:** DRAFT → EVALUATING_PRE → READY → EXECUTING → EVALUATING_POST → LOCKED, plus NEEDS_REVIEW on a dispute or failed gate. An edit increments the version, recomputes the hash and returns it to EVALUATING_PRE.
- **evaPacket lifecycle:** PLANNED → EXTRACTING (passes 1–2) → CROSS_SCORING (passes 3–6) → CONSENSUS | DISPUTED | FAILED (3 validation failures) | SUPERSEDED (hash changed mid-flight).
- **Readiness** is the SQL view `v_ready_gen_packets`: every pre_gen preset has CONSENSUS for the current `input_hash` with confidence ≥ 0.75, not disputed, parent locked, no BLOCK/HOLD, and the hard gate ambiguity < 0.40. **FORCE bypasses the eva gates.**
- **Queue:** FORCE tier > PIN tier (respects readiness; ordered by `manual_rank`) > algorithmic score (priority − ambiguity − coding intensity + capped ageing). FORCE overrides bound to an older `target_version` deactivate automatically on edit.
- **Scheduling:**
  - **Tiered lazy gating:** tier 1 (ambiguity, safety) first; failure sends the packet to NEEDS_REVIEW and saves the remaining passes.
  - **Hash memoisation:** reverting a prompt reuses existing consensus with zero LLM calls.
  - **VRAM-aware batching:** all model-A passes, then all model-B passes. Pacing ceiling, e.g. 24 passes per 5 minutes.
- **Risks:** dispute deadlocks (cap retries, route to NEEDS_REVIEW); mid-flight edit races (guarded update `WHERE input_hash = :target_hash`); JSON breakage in cross passes (grammar-constrained decoding, fuzz tests).

## GH — GitHub Copilot (grounded in M1 code)
- **Data model.** Keep M1's `packets` as the genPacket table, adding `tags` and `scores` JSON projections with per-field provenance. Add:
  - `evaluation_cycles` (packet version, `input_hash`, state);
  - `eva_packets` (cycle, packet, field, prompt contract, model, `input_hash`, state, attempt, `idempotency_key`);
  - `queue_overrides` (pin / reorder / hold / force / block, rank, reason, expiry, `cleared_at`).
  
  `evaluation_passes` gains `eva_packet_id` and `input_hash`; `eval_cycle_id` becomes a foreign key.
  - **Migration 0004**, frozen DDL. Old revisions are not edited. Legacy `content_eval` rows are kept, but no new evaluation work is created through `packet_kind`.
- **Lifecycles:**
  - genPacket: draft → evaluating → ready → queued → running → completed (locks), with blocked / failed and re-evaluation when inputs change.
  - evaPacket: planned → batched → running → done | failed; a superseded hash cancels pending work.
- **Readiness:** the field registry's `is_key_field` and required fields must be current with confidence ≥ the field threshold. There must be no dispute, no stale hash and no in-flight evaPackets on an old hash, and dependencies must be ready. Python predicates are used where the SQL dialects diverge.
- **Queue:** a stable algorithmic rank, then the latest unexpired override. block/hold remove the packet; force dispatches it if its dependencies are ready (**does not bypass safety/readiness**); pin goes first; reorder sets a rank. Overrides are audited in one transaction with an event.
- **Scheduling:** key fields first; reuse a result whose hash matches; batch by model and contract; per-cycle, concurrency and daily token caps; plan persisted before dispatch.
- **Risks:** stale results overwriting newer versions; unsafe force or starvation; migration and cross-dialect drift (test upgrades from populated M1 databases).

## Points of disagreement for cross-evaluation
1. **FORCE and readiness:** GE lets FORCE bypass the eva gates; MC and GH forbid it.
2. **genPacket table:** MC and GE create a new `gen_packets` table; GH evolves M1's `packets` through migration 0004.
3. **evaPacket granularity:** one per (genPacket version × field/template) inside a cycle (MC, GH), versus one per (genPacket × `target_hash` × preset) with no separate cycle (GE).
4. **Readiness location:** SQL view (GE) versus Python predicate re-checked at admission and claim (MC) versus mixed (GH).
5. **Post-generation evaluation:** an explicit EVALUATING_POST state before LOCKED (GE) versus content evaluation as evaPackets on the output after completion (MC, GH implicit).
