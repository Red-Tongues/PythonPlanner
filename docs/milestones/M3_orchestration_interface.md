# M3 — Orchestration loop, channels, interface and integrations

**Goal:** the kernel runs continuously and visibly — evaluating, routing, generating, writing to the production repo, scanning quality and feeding results back — with the user in control through autonomy scopes, an approval queue, loop interruption and a local web GUI.

Serves requirements **C**, **D**, **E**, **F** · Key Idea K1–K5. Builds on M1–M2.

## Deliverables

### 1. Kernel loop (`orchestrator.kernel`)
- `orchestrator run [--once] [--tick-seconds N] [--max-ticks N]`. Each tick: (1) read `control_state` (paused / pending interrupt); (2) quickfire evaluation batch, batched by model with weighted priority; (3) SQL refresh (views are live; materialise only if needed); (4) promotion and branch triggers; (5) route ready packets; (6) process channel runs; (7) write accepted artifacts to the production repo; (8) trigger quality scans for changed branches and create content-evaluation packets; (9) sleep.
- Crash-safe and idempotent: all state in the DB; a restarted kernel resumes without duplicating work. Graceful shutdown on SIGINT/SIGTERM.

### 2. Channels (`orchestrator.channels`)
- Common base: creates a `channel_runs` row per attempt with channel-prefixed run IDs (`QE…`, `LG…`, `WP…`), validates output against the packet's `response_structure` contract, retries with the validation error up to the channel's max attempts (QE 4, LG 3, WP 2), then marks the run `failed`.
- Console lines, in exactly this form: `Quickfire Evaluation is verifying #QE16709 [attempt 1/4]`. Exposed via `orchestrator status` and the GUI.
- `LocalGenerationChannel`: simple generations through the local provider.
- `WorkbenchProductionChannel` uses a `WorkbenchAdapter`:
  - `FileDropWorkbench` (implemented): writes `workbench/outbox/<run_id>.md` (human-readable prompt packet) + `<run_id>.json`; polls `workbench/inbox/<run_id>.json` for the result; timeout → failed attempt. A human, another agent or a future Juggler extension can serve it.
  - `JugglerWorkbench` (stub): documents the extension contract — conversation selection, model selection, prompt insertion, model outputs, error messages — in `docs/integrations/juggler.md`.
- Routing per PROJECT_PLAN §8 using consensus values and `thresholds`.

### 3. Response Structure contracts
- Complete the registry: `markdown_document`, `python_module` (must parse with `ast.parse`), `json_object`, `yaml_document`, `csv_table`, `subtask_list`, `project_tree`, `test_module`. Each output also declares the target path (defaults to the packet's `repo_target_path`).

### 4. Repository writer (`orchestrator.repo`)
- Production repo at `production_repo_path` (outside this repo); `git init` if missing.
- Writes only artifacts that are accepted (directly, or via the `repo_write` confirmation). Commit message: `<packet ref> <run id>: <title>`. Records `artifacts` rows with sha256.
- Never overwrites a file owned by a completed packet unless the writing packet supersedes it; history stays in git (reversibility).
- Every accepted artifact creates a **content-evaluation packet** that is evaluated with the content-level candidate fields.

### 5. Quality integration (`orchestrator.quality.sonarqube`)
- Settings: host URL, token (env only), project key prefix, `sonar-scanner` path.
- Community Build analyses one branch only → analyse each branch as its own project key `<prefix>__<branch>`.
- `scan(branch)` runs `sonar-scanner` if present (skips with a clear event otherwise); `fetch_report(key)` reads the quality gate, measures and issue counts from the Web API into `quality_reports`. Tests mock HTTP with `httpx.MockTransport`.

### 6. Loop interruption and autonomy enforcement
- `orchestrator interrupt "prompt"` (and a GUI form): sets paused; in-flight runs finish; the prompt goes through the Part 1 path (deterministic or LLM extraction) producing new core-field **versions**; affected packets are marked stale for re-evaluation; resume automatically or with `orchestrator resume`, per scope.
- `orchestrator pause` / `resume`.
- Enforce every autonomy scope in PROJECT_PLAN §11 at the point of action; `manual` stages never act; `confirm` stages create confirmation rows; objective-level LLM values are always `confirm`.

### 7. GUI (`orchestrator.gui`) — FastAPI + Jinja2 + HTMX
- `orchestrator gui [--host 127.0.0.1] [--port 8765]`.
- Miller columns: Projects │ Objectives │ prioritised Tasks │ Packet detail (core fields editable unless locked; locked → read-only with "create new version"; candidate consensus with per-pass evidence; lineage; runs; artifacts).
- Approval queue (approve / reject with reason); autonomy settings; thresholds and weights; console panel (three channel lines + last 50 events, refreshed with HTMX polling); pause / resume / interrupt controls; stats page.
- No external CDN dependencies at runtime (vendor HTMX into `static/`). Tests with FastAPI `TestClient`.

### 8. Demo and docs
- `orchestrator demo`: end-to-end in a temp directory on SQLite with FakeProvider and FileDropWorkbench auto-responder: ingest sample → evaluate → consensus → branch → route → generate → write to production repo → mocked quality report → content evaluation; prints the console and a summary table.
- `docker-compose.yml` (optional services): PostgreSQL 16, SonarQube Community Build, Ollama.
- `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md` (running against Postgres + Ollama + SonarQube), `docs/integrations/juggler.md`; update `docs/PROGRESS.md`.

## Acceptance criteria
1. `orchestrator demo` completes and prints a console trace showing all three channels and a summary.
2. `orchestrator run --max-ticks 3` on the sample project advances packets with no human input when scopes are `auto`, and stops at the approval queue when they are `confirm`.
3. `orchestrator gui` serves the Miller-column tree, approval queue, console and controls; GUI tests pass.
4. Locked packets cannot be edited from GUI, CLI or code paths (tests).
5. Interrupt → pause → re-evaluate → resume works (test).
6. All tests pass (SQLite + PostgreSQL CI); ruff clean; `docs/PROGRESS.md` updated; PR lists each criterion ✅/❌.

## R0 review amendments (adopted 2026-11-03 — see `docs/reviews/R0_consensus.md`)

These take precedence over the text above where they conflict.

- **C1 Durable runs.**
  - Persisted run state machine (`planned → dispatched → awaiting_result → validating → accepted | failed | stale`).
  - Intent is written before any external side effect; idempotency keys make every effect repeatable.
  - Atomic claims with expiring leases and heartbeats; a single-kernel lock.
  - Startup reconciliation of DB, workbench files and git (resume, re-dispatch or mark stale).
- **C2 Non-blocking channels.**
  - Workbench runs wait in `awaiting_result` and are checked each tick until `deadline_at`.
  - Every external operation has a deadline; one blocked channel never halts the others.
  - Pause means *drain*: no new dispatches, in-flight runs complete.
  - Results and approvals are bound to the input hash and policy revision; stale completions are rejected.
- **C3 Artifact safety.**
  - Generated artifacts are quarantined (`workbench/quarantine/`) until structural + semantic checks pass (e.g. `python_module` must import in a subprocess and its tests must pass when present).
  - Target paths are resolved under the repo root, rejecting traversal and symlink escapes.
  - Accepted artifacts are bound to git commit SHA + content hash; writes and commits are repeatable.
- **C4 Code outputs** use fenced code blocks with a small front-matter header (path, contract, version), not JSON-escaped strings.
- **C5 Honest autonomy.**
  - With no workbench consumer attached, complex-production packets show as `blocked: no workbench consumer` (GUI + CLI) while all other work continues.
  - SonarQube scans are bound to the exact commit SHA.
  - Skip/failure policy is documented: a skipped scan is evented; a failed quality gate flags the content-evaluation packet and raises a confirmation.
- **C6 Adversarial tests.**
  - Fault injection at every durable boundary (e.g. crash after commit, before DB acknowledgement).
  - Concurrent claims; late or duplicate inbox results; approval races; branch replay; starvation; retry exhaustion.
  - A short randomised-fault soak (`orchestrator soak --ticks N --seed S`) that asserts progress and no duplicate effects.
- **C7 Trust boundary** (documented in `docs/RUNBOOK.md`). On PostgreSQL the runtime role has no DDL rights (migrations use a separate role). Generated work and workbench consumers never receive DB credentials.
- **C8 Pace and drift governor (CFO decision D14).** Implement PROJECT_PLAN §11.1:
  - per-channel pace budgets (runs/hour, runs/day) with conservative defaults;
  - key routing fields evaluated first; other fields lazily, re-evaluated only on input-hash change;
  - a daily new-packet cap per objective; a confirmation for packets not traceable to an approved objective;
  - low requirement-alignment on content evaluation pauses that branch;
  - `orchestrator digest` and a GUI digest page (what advanced, what waits for the user, what is blocked and why).

## D6 reconciliation amendments C9–C20 (GPT-6.1, 2026-11-04) — align M3 with D1 (genPacket/evaPacket) and M4
*These supersede earlier M3 text where they conflict.*
- **C9** Migration (**0005**; 0004 is M1.1) keeps IDs, lineage and legacy `content_eval` rows but forbids new ones. evaPackets are keyed by version × phase × preset revision × target hash and grouped by `evaluation_cycles`.
- **C10 Tick order:**
  1. controls and reconciliation;
  2. schedule evaPackets;
  3. batched evaluation (model A, then model B);
  4. projections and triggers;
  5. readiness admission and claim;
  6. advance channels;
  7. authorised writes, scans and post-evaluation scheduling;
  8. completion and digest.
  
  Pause drains in-flight work; no new dispatches.
- **C11** Run states are separate from genPacket states (`draft → evaluating_pre → ready → queued → running → evaluating_post → completed`, plus `needs_review`, `held`, `blocked`, `failed`). Run acceptance never locks a packet on its own.
- **C12** One pure readiness predicate governs admission and atomic claim; SQL views only explain and filter. Tier 1 is evaluated before routing fields, then optional fields; a tier-1 failure or dispute → `needs_review`, no further tiers. Typed dependencies, not hierarchy.
- **C13** FORCE, PIN and REORDER change priority only. WAIVE covers named soft fields, with actor, reason and version; hard constraints are never waived. These lapse on revision. HOLD and BLOCK declare their scope; revocations are append-only. One normal admission is reserved per N forced/pinned admissions; ageing is capped.
- **C14** Freeze tags, scores, consensus, waivers and override IDs in `generation_runs`. Reuse an evaluation only on a full content-address match. Hash-guard projections. Superseded evaPackets never block current work.
- **C15** The Juggler stub is replaced by a non-blocking `juggler run --json --project <worktree> --strategy orchestrator-packet --timeout <deadline> -`: prompt on stdin, a unique worktree, conversation metadata persisted. Never `yolo`. Keep FileDrop as a fallback. Spike the model/JSON/resume contracts.
- **C16 Exit codes:** `0` → validating; `1` → failed attempt; `3` → `awaiting_confirmation` (parking is not failure; approvals are revalidated before continuing); `4` → timeout failure; `2` → configuration failure, never retried unchanged.
- **C17** Validation, authorised merge/write and post-evaluation acceptance are separate steps. Keep quarantine, path safety, idempotency and provenance. Evaluations bind to the artifact hash and scans to the commit SHA; changed output needs re-evaluation before locking.
- **C18** Quality and drift evidence attaches to output evaPackets and to the originating versions. Keep confirmations and branch pauses. Define the scan skip/failure acceptance policy without bypassing hard constraints.
- **C19** GUI and digest show packet and run states, pre/post cycles, readiness reasons, waivers and overrides, snapshots, parked approvals and conversation links.
- **C20** The demo ends with exact-hash post-evaluation acceptance and locking. Tests cover expiry, claim races, stale results, snapshots, CLI exit codes, parking and crash recovery.
- **Integration risks:** M2–M3 evaluation schema/migration compatibility · M2–M4 atomic readiness-to-launch binding · M2–M4 artifact/worktree/merge hash identity · M3–M4 parked approval/resume/restart idempotency · M2–M4 deadlocks between completion, quality and branch pauses.

## C21 — Pace and drift governor, detailed (Gemini Argon design D7, 2026-11-04; PM-reconciled). Implements C8.
- **Signals and default thresholds:**
  - fan-out ≤ 3 per packet (`v_lineage_metrics`);
  - ≤ 24 new genPackets per objective per day;
  - post-generation requirement alignment ≥ 0.75;
  - ≤ 3 consecutive validation failures in 2 h (`channel_runs`);
  - pre-generation ambiguity ≤ 0.40;
  - ≤ 150k tokens per objective per day (from `generation_runs` snapshots);
  - ≤ 48 h since the last human touch on an active objective (events);
  - lineage divergence (cosine distance from the root brief, via a small local embedding model) ≤ 0.35 — *optional in v1*.
  - **PM reconciliation:** Gemini proposed a depth signal of ≤ 6. The **hard cap stays `max_branch_depth = 2` (B5)**, so depth is not a governor signal in v1.
- **Response ladder:** log → slow down → require confirmation → pause branch → pause objective → global pause. Escalate after 2 consecutive breaching cycles; step down after 3 clean cycles (hysteresis). Every action goes in append-only `governor_events`.
- **Pace budgets (defaults):** Quickfire Evaluation ≤ 120 runs/h; Local Generation ≤ 20/h (≤ 200/day); Workbench Production ≤ 4/h (≤ 24/day). Per objective: ≤ 24 packets/day and ≤ 150k tokens/day. FORCE/PIN change queue position only and never bypass budgets. Nights and weekends throttle to 25%; Workbench Production pauses unless pinned.
- **Daily digest**, ranked:
  1. actions and holds needing the owner;
  2. advanced work and commits;
  3. blocked or paused items, with reasons;
  4. resource and token summary.
  
  One-click actions: waive, kill branch, top up an objective's budget, hold — each evented.
- **Data model:** `governor_states` (scope_type, scope_id, status, tokens_today, packets_today, last_human_touch_at); `governor_events` (append-only: trigger_signal, current_value, threshold, action, scope); `channel_budget_trackers` (channel, runs_this_hour, runs_today, limits).
- **Must-have tests:**
  - a split loop hits the packet cap and pauses the objective;
  - a slow-burn objective trips the 48 h touch limit;
  - an override storm still respects budgets;
  - a flapping metric is held by hysteresis;
  - budget exhaustion halts new dispatches;
  - runaway fan-out slows pacing;
  - a waiver lapses on revision;
  - a Workbench timeout never blocks other channels.
- **Not in v1:** automated refactoring of drifting branches; predictive cost-to-completion models; dynamic budget markets between objectives.
