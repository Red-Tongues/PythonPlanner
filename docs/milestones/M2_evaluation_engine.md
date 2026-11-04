# M2 — Evaluation engine and SQL layer

**Goal:** packets get their candidate fields filled by the six-pass dual-model quickfire evaluation, and SQL turns the raw passes into consensus values, statistics, branch triggers and a unique priority queue.

Serves requirements **A** (LLM extraction contract), **B**, **C** · Key Idea K1–K4. Builds on M1; do not change M1 behaviour except to fix bugs.

## Deliverables

### 1. LLM providers (`orchestrator.llm`)
- `LLMProvider` protocol: `complete(prompt: str, *, model: str, max_tokens: int, temperature: float, json_mode: bool) -> LLMResponse` (`text`, `model`, `latency_ms`, `prompt_tokens`, `completion_tokens`).
- `FakeProvider`: deterministic. Scripted responses by (model, prompt-pattern) **or** a rule-based default that derives plausible values from the prompt text, so a full evaluation runs with no network. Can be told to return invalid JSON N times to exercise retries.
- `OpenAICompatibleProvider` (httpx): `base_url`, `api_key` (optional), timeouts, JSON mode when supported. Works with Ollama (`http://localhost:11434/v1`), llama.cpp server and LM Studio. Tests use `httpx.MockTransport` — never the network.
- Settings: `model_a`, `model_b`, `model_c` (aggregator), provider URLs.

### 2. Prompt templates (`orchestrator.evaluation.prompts`)
- Templates for extraction, combined evaluation (score + evidence + reason) and, for the split plan, separate accuracy and evidence prompts — wording based on PROJECT_PLAN §7.
- Each prompt is self-contained: field definition (label, kind, vocabulary or scale, `prompt_hint`), packet core fields, project core fields, and up to N exemplars from `exemplars` for that field if present. Output format instructions require JSON.

### 3. Pass plan and evaluator (`orchestrator.evaluation`)
- Default plan per PROJECT_PLAN §7: P1 A-extract, P2 B-extract, P3 A→value_b, P4 B→value_a, P5 A→value_a, P6 B→value_b. Plan is data (YAML or a settings row), so a `split` plan can be selected.
- Contracts: `CandidateValue` (value or NULL, typed by field kind; tag sets restricted to the vocabulary; scores 0–10; text ≤ `max_chars`) and `PassEvaluation` (`accuracy_score` 0–10, `evidence: list[{quote, tag: SUPPORT|CONTRADICTION}]`, `reason` ≤ 280 chars).
- Validation with retries: invalid output → retry with the validation error appended, up to the Quickfire Evaluation max attempts (default 4). Every attempt is recorded in `channel_runs` (`QE` run IDs) and every valid pass in `evaluation_passes` (append-only) with latency.
- **Evidence verification:** each quote must be an exact (whitespace-normalised) substring of the source text; otherwise it is stored with `verified = false` and excluded from aggregation.
- Text fields stop after 3 passes. `evaluations_count` on the packet is incremented through the governed update path.
- `evaluate_packet(ref, fields=None)` and `evaluate_pending(limit)` (packets with missing or stale candidate values), batching calls by model.

### 4. SQL layer (`orchestrator.sql`) — views on both SQLite and PostgreSQL
- `v_field_consensus`: per packet × field → final value, `agreement` flag, `confidence` (weights from `settings`), supports, contradictions, unverified-evidence count, status `accepted` / `disputed` / `null` — rules exactly as PROJECT_PLAN §7. Tag sets are exploded so each tag is a toggle.
- `v_packet_stats`: prompt word count, evaluation run count, cumulative evaluation time, lifecycle duration, scores above threshold, evidence item count, generation time (0 until M3).
- `v_branch_stats` (federated, root packets with descendants): original creation timestamp, descendant count, score variance, revision count, failed/rejected run count, contradiction count, model usage distribution (as counts).
- `v_branch_triggers`: packets × enabled `thresholds` rules that fire, with the action.
- `v_queue`: unique deterministic priority per PROJECT_PLAN §8 with anti-starvation ageing; tie-break `created_at`, `ref`.
- If a view is impractical in portable SQL, implement it as a parameterised query in Python **and** document why in `docs/PROGRESS.md`.

### 5. Promotion and branching
- `promote_consensus(packet)`: accepted consensus values feed master governance fields (e.g. `channel` routing suggestion, `priority_level`) according to the `candidate_promotion` autonomy scope (`auto` → apply + event; `confirm` → confirmation row).
- `apply_branch_triggers()`: for `split` actions, create child packets (`parent_id`, `root_id`, `branch_reason`) from a `subtask_list` produced by the Local Generation model (FakeProvider in tests); the original packet is never removed. Respects the `branching` autonomy scope.
- Disputed key fields go to the approval queue.

### 6. LLM tree extraction contract (Part 1)
- `extract_tree_with_llm(text, provider)` → validated `ProjectTree` (retries as above). Ingestion chooses deterministic vs LLM via a flag; both produce the same contract and go through the same confirmation flow.

### 7. CLI
`orchestrator evaluate [--packet REF] [--limit N]` · `consensus REF` · `stats [REF]` · `queue [--channel ...]` · `branch apply` · `confirmations list|approve ID|reject ID`.

### 8. Tests (FakeProvider only)
Full six-pass run on one packet; agreement, disagreement, dispute-margin and NULL consensus cases; tag-set toggles; evidence verification; retry on invalid JSON then success, and failure after max attempts; append-only enforcement on `evaluation_passes`; branch trigger creates linked children while the original remains; priority ordering is unique and ages correctly; autonomy scope `confirm` produces confirmation rows instead of changes; the same SQL tests run on PostgreSQL in CI.

## Acceptance criteria
1. `orchestrator ingest examples/sample_project_plan.md && orchestrator evaluate` (FakeProvider by default when no model endpoint is configured) fills candidate fields for every packet.
2. `orchestrator consensus <ref>` shows the final value, confidence, status and evidence for each field.
3. A packet with `task_parsability ≥ 7` produces child packets linked to the original.
4. `orchestrator queue` lists packets in a unique, deterministic order.
5. All tests pass on SQLite locally and on PostgreSQL in CI; ruff clean; `docs/PROGRESS.md` updated; PR lists each criterion ✅/❌.

## R0 review amendments (adopted 2026-11-03 — see `docs/reviews/R0_consensus.md`)

These take precedence over the text above where they conflict.

- **B1 Evaluation cycles.** Every evaluation run gets an `eval_cycle_id` bound to the packet version, project/objective versions, field-definition version, model identities and pass-plan version. `v_field_consensus` aggregates only the latest *complete* cycle per (packet, field).
- **B2 Consensus decision table** (document it in `docs/ARCHITECTURE.md`, truth-table tests on both dialects):
  - agreement → accepted only if confidence ≥ 0.60;
  - disagreement → the winner needs confidence ≥ 0.70 and a margin ≥ 0.15, otherwise `disputed`;
  - one model NULL → accept the non-null value if its confidence ≥ 0.70, else `disputed`;
  - both NULL → `null`; incomplete cycle → `pending`; exact ties → `disputed`;
  - acceptance requires ≥ 1 verified SUPPORT quote (except `null`); contradictions > supports → `disputed`;
  - tag sets are scored per tag (each tag a toggle with its own confidence).
  - All thresholds live in `settings`.
- **B3 Text fields:** 4 passes (P1 A-extract, P2 B-extract, P3 A→b, P4 B→a); confidence = cross score / 10.
- **B4 Priority.** Inputs normalised to 0–1 with `COALESCE` defaults (ambiguity 5/10 when unknown). The ageing term saturates at `max_age_hours`.
  - *Eligibility* (status `ready`, unclaimed or lease expired, approvals satisfied, within failure budget, not `waiting_for_children`) is computed separately from *rank*.
  - Retry backoff; a lifetime failure budget (default 3) moves the packet to `awaiting_confirmation`.
  - Max-wait rule: any eligible packet waiting longer than `max_wait_hours` is served next.
  - Tests with simulated time on both dialects.
- **B5 Branching.** Idempotent on (packet id + version, rule id, rule version), enforced by a unique key. `max_branch_depth` = 2 plus a descendant cap. A split trunk moves to `waiting_for_children` and completes by deterministic roll-up when its children do. `content_eval` packets never branch and never route to generation channels.
- **B6 View portability.** One documented semantic contract per view (columns, types, NULL semantics, ordering), dialect-specific DDL in one module, and cross-dialect tests asserting identical rows for identical seed data. Decision-making views must not fall back to Python.
- **B7 Small-model robustness.**
  - Tolerant output extraction: strip code fences and preambles before JSON parsing.
  - Conservative quote normalisation before substring matching (whitespace, typographic quotes/dashes, markdown emphasis only). Unverified quotes are stored and excluded, and never fail the parse.
  - Retry corrections are one concise line, not raw tracebacks.
  - FakeProvider gains a `noisy` mode (fences, preambles, smart quotes, trailing commas).
- **B8 Schemas in prompts.** Pass a compact JSON schema of the expected contract in the prompt. Providers declare capabilities (JSON mode, grammar) with graceful fallback.

## B9 — genPackets and evaPackets (CFO clarification + D1 panel consensus, 2026-11-04)

**Takes precedence over the earlier sections where they conflict.** Full rationale: `docs/reviews/D1_consensus.md`.

- **Terminology.** `packets` rows are **genPackets**: the generative work items. **evaPackets** are stateless, preset evaluative prompts that fill a genPacket's evaluation fields. Evaluating generated output is done by evaPackets aimed at the output, never by new genPackets.
- **Migration 0004** (additive and frozen; never edit 0001–0003). It creates:
  - `evaluation_cycles`, `eva_presets` (field, phase pre/post, tier 1–3, prompt template, schema version, policy version) and `eva_packets` (packet + version, cycle, preset revision, phase, target hash, model config, state, attempt, idempotency key, unique content address);
  - `queue_overrides` and `waivers` (append-only, version-bound, with separate revocation events), `packet_dependencies` (typed edges), and `generation_runs` with a frozen `evaluation_snapshot`;
  - on `packets`: `tags` and `scores` JSON projections plus `input_hash`;
  - on `evaluation_passes`: `eva_packet_id`, with `eval_cycle_id` becoming a foreign key;
  - a CHECK that bans new `content_eval` packets.
- **genPacket lifecycle:** `draft → evaluating_pre → ready → queued → running → evaluating_post → completed (locked)`, plus `needs_review`, `held`, `blocked` and `failed`. An edit creates a new version and returns it to `evaluating_pre`.
- **evaPacket lifecycle:** `planned → extracting (P1–P2) → cross_scoring (P3–P6) → consensus | disputed | failed | superseded`.
- **Readiness.** A pure Python predicate, re-checked at admission and at atomic claim, plus an explanatory `v_ready_candidates` view. Requires:
  - every required tier-1/2 field has a current consensus at or above its threshold, or a valid version-bound waiver for a soft field;
  - no hard-constraint failure; typed dependencies satisfied; approvals present.
- **Queue.** Algorithmic rank (normalised, capped ageing, dependency-unlock value), then human overrides with precedence BLOCK > HOLD > FORCE > PIN > REORDER > NORMAL. FORCE is priority only. One normal admission is reserved per N forced/pinned admissions.
- **Scheduling.** Tiered lazy gating; content-addressed reuse; all model-A passes, then all model-B passes; single-model concurrency by default; pace budgets (D14).
- **Guards.** Stale writes are rejected by input hash. Superseded in-flight work never blocks the current version. Every override, waiver and state change is evented.
- **Tests** (both dialects): edit during evaluation and just before claim; dispute deadlock → `needs_review` with no loop; waiver lapses on edit; FORCE cannot bypass hard gates; reserved fair admission under constant pinning; reuse only when the full content address matches; post-evaluation must pass before locking.

## B10 — Consensus rulings and truth-table fixtures (D2, 2026-11-04)
The eight PM rulings in `docs/reviews/D2_consensus_truth_table.md` are binding:
- candidate confidence formulas;
- NULL pass-skipping and completeness;
- evidence scoped to the winning value;
- per-tag pass contract and per-tag rows;
- reported confidence on disputed/null/pending rows;
- integer scores with `ROUND(conf, 4)` comparison;
- strict version binding;
- tier-3 fields and post-phase gating.

Implement TC-01 to TC-52 as parametrised tests on both dialects. Derive the expected values from the rulings in code, rewrite the fractional-score cases as integer boundary cases, and flag any case where the code and the table disagree in `docs/PROGRESS.md`.

## B11 — Evaluator model pinning (D3, 2026-11-04)
- Each provider config names a model **and its SHA-256 digest**. At startup the OpenAI-compatible/Ollama provider checks `/api/tags` (or an equivalent) against the allowlist and refuses to run evaluations on a mismatch (evented, visible in `orchestrator status`).
- Record model digest, quantisation, `num_ctx` and decoding options in each `eva_packets.model_config`, so they form part of the content address (D1).
- Always send an explicit `num_ctx` and `think: false` where supported. Use JSON-Schema structured outputs (`response_format` json_schema / native `format`) generated from the Pydantic contracts, then validate with Pydantic.

- **Hardware envelope (D17):** evaluator configuration must fit in ≤ 20 GB VRAM in total, on CUDA (RTX 3090) or Apple Metal (32 GB M2 Max). Provider code must not assume CUDA. Model choice is configuration, not code.
- **D5 clarifications:** see `docs/reviews/D5_contracts_notes.md` (integer boundaries, tag-map nulls, tie evidence, the pure consensus oracle plus SQL parity tests).

## B12 — D3 confirmed (CFO, 2026-11-04)
The combined six-call pass plan is canonical. **Hybrid escalation:** a field whose combined evaluation fails validation after one targeted repair, or ends `disputed` on a tier-1 field, is re-run once using the split plan (separate accuracy and evidence prompts). If it is still unresolved, it goes to `needs_review`. The pass plan stays data-driven, so specific field classes can be pre-routed to split by configuration.
