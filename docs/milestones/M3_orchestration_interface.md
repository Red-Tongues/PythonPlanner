# M3 — Orchestration loop, channels, interface and integrations

> **Reading order (R3, 2026-11-04):** the **R3 amendments C22–C38** at the end of this file take precedence over all earlier text, including R0 (C1–C8), D6 (C9–C20) and C21. Lines marked *[superseded → Cxx]* are kept only for history.

**Goal:** the kernel runs continuously and visibly — evaluating, routing, generating, writing to the production repo, scanning quality and feeding results back — with the user in control through autonomy scopes, an approval queue, loop interruption and a local web GUI.

Serves requirements **C**, **D**, **E**, **F** · Key Idea K1–K5. Builds on M1–M2.

## Deliverables

### 1. Kernel loop (`orchestrator.kernel`)
- `orchestrator run [--once] [--tick-seconds N] [--max-ticks N]`. Each tick: (1) read `control_state` (paused / pending interrupt); (2) quickfire evaluation batch, batched by model with weighted priority; (3) SQL refresh (views are live; materialise only if needed); (4) promotion and branch triggers; (5) route ready packets; (6) process channel runs; (7) write accepted artifacts to the production repo; (8) trigger quality scans for changed branches and create content-evaluation packets; (9) sleep. *[superseded → C10, C30]*
- Crash-safe and idempotent: all state in the DB; a restarted kernel resumes without duplicating work. Graceful shutdown on SIGINT/SIGTERM.

### 2. Channels (`orchestrator.channels`)
- Common base: creates a `channel_runs` row per attempt with channel-prefixed run IDs (`QE…`, `LG…`, `WP…`), validates output against the packet's `response_structure` contract, retries with the validation error up to the channel's max attempts (QE 4, LG 3, WP 2), then marks the run `failed`.
- Console lines, in exactly this form: `Quickfire Evaluation is verifying #QE16709 [attempt 1/4]`. Exposed via `orchestrator status` and the GUI.
- `LocalGenerationChannel`: simple generations through the local provider.
- `WorkbenchProductionChannel` uses a `WorkbenchAdapter`:
  - `FileDropWorkbench` (implemented): writes `workbench/outbox/<run_id>.md` (human-readable prompt packet) + `<run_id>.json`; polls `workbench/inbox/<run_id>.json` for the result; timeout → failed attempt. A human, another agent or a future Juggler extension can serve it.
  - `JugglerWorkbench` (stub) *[superseded → C34: generic subprocess adapter + fake executable in M3; real Juggler in M4]*: documents the extension contract — conversation selection, model selection, prompt insertion, model outputs, error messages — in `docs/integrations/juggler.md`.
- Routing per PROJECT_PLAN §8 using consensus values and `thresholds`.

### 3. Response Structure contracts
- Complete the registry: `markdown_document`, `python_module` (must parse with `ast.parse`; execution checks → C31), `json_object`, `yaml_document`, `csv_table`, `subtask_list`, `project_tree`, `test_module`. Each output also declares the target path (defaults to the packet's `repo_target_path`).

### 4. Repository writer (`orchestrator.repo`)
- Production repo at `production_repo_path` (outside this repo); `git init` if missing.
- Writes only artifacts that are accepted (directly, or via the `repo_write` confirmation). Commit message: `<packet ref> <run id>: <title>`. Records `artifacts` rows with sha256.
- Never overwrites a file owned by a completed packet unless the writing packet supersedes it; history stays in git (reversibility). *[extended → C32]*
- ~~Every accepted artifact creates a content-evaluation packet~~ *[superseded → C30: post-phase evaPackets bound to the output manifest hash; new `content_eval` packets are forbidden (C9)]*

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
  - Skip/failure policy is documented: a skipped scan is evented; a failed quality gate flags the content-evaluation packet and raises a confirmation. *[superseded → C30]*
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
- **C9** *[migration number superseded → C22: M3 starts at 0007]* Migration (**0005**; 0004 is M1.1) keeps IDs, lineage and legacy `content_eval` rows but forbids new ones. evaPackets are keyed by version × phase × preset revision × target hash and grouped by `evaluation_cycles`.
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
- **C15** *[moved to M4 → C34]* The Juggler stub is replaced by a non-blocking `juggler run --json --project <worktree> --strategy orchestrator-packet --timeout <deadline> -`: prompt on stdin, a unique worktree, conversation metadata persisted. Never `yolo`. Keep FileDrop as a fallback. Spike the model/JSON/resume contracts.
- **C16 Exit codes:** `0` → validating; `1` → failed attempt; `3` → `awaiting_confirmation` (parking is not failure; approvals are revalidated before continuing); `4` → timeout failure; `2` → configuration failure, never retried unchanged.
- **C17** Validation, authorised merge/write and post-evaluation acceptance are separate steps. Keep quarantine, path safety, idempotency and provenance. Evaluations bind to the artifact hash and scans to the commit SHA; changed output needs re-evaluation before locking.
- **C18** Quality and drift evidence attaches to output evaPackets and to the originating versions. Keep confirmations and branch pauses. Define the scan skip/failure acceptance policy without bypassing hard constraints. *[policy now defined → C30]*
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
- **Response ladder** *[v1 simplified → C33; full ladder deferred to M5]*: log → slow down → require confirmation → pause branch → pause objective → global pause. Escalate after 2 consecutive breaching cycles; step down after 3 clean cycles (hysteresis). Every action goes in append-only `governor_events`.
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

## R3 amendments C22–C38 (three-model pre-implementation review, 2026-11-04 — see `docs/reviews/R3_consensus.md`)
*Binding. They take precedence over everything above. Finding IDs are given in brackets. Verified by an independent GPT-6.1 pass (V1–V11, all adopted in the text below).*

- **C22 Migrations** [GA1, MA1, GC1]. M3 builds on the merged M2.1 schema (frozen migrations 0001–0006, never edited). All M3 schema goes in new frozen migration **0007** (more only if unavoidable), with dialect DDL in the dialect module. Do not recreate `eva_packets`, `evaluation_cycles` or the `content_eval` ban. Test: upgrade a *populated* 0006 database to head on SQLite and PostgreSQL; IDs, hashes, lineage and immutable rows are unchanged, and new `content_eval` rows are still rejected.
- **C23 Packet states** [GA2, MA3, GC3]. The genPacket state set is C11's plus `waiting_for_children`, `awaiting_confirmation` and `closed_unverified`.
  - `closed_unverified` is terminal and non-success. It is excluded from the queue, never counts as `completed`, and never satisfies a dependency.
  - The CLI and the GUI's "Close unverified" action require a reason.
  - Publish separate **packet** and **run** transition tables in `docs/ARCHITECTURE.md`; tests assert that every transition not in the tables is rejected.
  - GUI and digest give `closed_unverified`, `awaiting_confirmation` and `blocked:<reason>` distinct badges.
- **C24 Run tables** [GA3].
  - Each generation dispatch creates one `generation_runs` row. It owns the C1 run state machine, the lease, the fencing token (C26) and the frozen snapshot, including `gate_revision`.
  - Each attempt appends one row to `channel_runs` (append-only, linked by `generation_run_id`); Quickfire Evaluation attempts link `eva_packet_id`.
  - Console IDs are `#<QE|LG|WP><channel_runs id>`.
- **C25 Governance atomicity** [GA4, MA2, GC2].
  - **Writers.** Every governance write increments the `gate_revision` of every non-terminal packet in its scope, in the same transaction. Governance writes are: dependency additions and revocations, override and waiver additions and revocations, holds, branch kill, confirmation decisions, autonomy-scope or threshold changes, budget top-ups and interrupt supersession, whether made from the GUI, the CLI or the digest. Scope means the packet itself or all packets under the branch, objective or project.
  - **Admission and claim.** Both recompute the context fingerprint from the current packet, project and objective heads (M2.1 #1). The claim's guarded UPDATE checks status, version, input hash, `gate_revision` and lease. The snapshot is written in the same transaction.
  - **Run completion.** It re-checks fingerprint and gate revision. On mismatch only the completing run becomes `stale`, with an event. The packet returns to `evaluating_pre` only if that run still owns the current packet version, the packet is non-terminal, and no hold, block, pause or `awaiting_confirmation` applies; otherwise the packet's current state and restrictions are kept. A stale run never changes a terminal packet or a superseding version.
- **C26 Fencing and exclusivity** [MA8, GA16 partial].
  - **Fencing.** Every claim carries a monotonically increasing fencing token. Result acceptance and repository publication reject obsolete tokens.
  - **Pause.** Dispatch authorisation is serialised atomically with pause: work authorised before the pause commits is in flight and drains. While a scope is paused, no generation, generation retry or pre-phase evaluation is dispatched in it. Reconciliation and collection of already-dispatched results continue. Post-phase evaluation may still be dispatched for outputs whose authorised write committed before the pause, subject to evaluation budgets and evaluator verification (C29). Pausing the evaluation channel itself stops these too.
  - **Publication and recovery.** Publication checks the expected branch HEAD. Every external effect has a durable operation ID, so recovery recognises an effect that already happened.
  - **Kernel lock.** The single-kernel lock must be portable to both dialects (e.g. a lease row taken by conditional UPDATE). Its mechanism is the engineer's choice, but tests must show exclusion, lease expiry and takeover, and that a fenced-out old owner cannot publish.
- **C27 Attempts, exit codes and failure budget** [GA5, MA3, GC3, MA15].
  - **Attempt failures.** A validation failure, exit `1` or exit `4` is a failed attempt and counts toward the channel's max attempts. Exhausting them marks the `generation_runs` row `failed` and adds 1 to the packet's `failed_runs`.
  - **Exit 3.** The run moves to the run state `awaiting_confirmation` (added to the `generation_runs` state set), keeping a stable conversation/resume identity and counting against nothing. Approval revalidates current bindings (C25) and the dispatch gates, then resumes the same run and conversation through `dispatched` exactly once; it never silently starts a new conversation. Rejection leaves the run parked until it is explicitly cancelled. Cancellation → `failed`, without using the packet's failure budget. A binding mismatch → `stale`. These transitions go in the run transition table (C23).
  - **Exit 2.** The run fails and the channel pauses with `configuration_error`. This does not count toward the packet's failure budget. The channel retries only after the configuration revision changes.
  - **Lifetime failure budget exhausted** (M2.1 #12). The packet makes an audited transition to `awaiting_confirmation`, with exactly one confirmation. Approval grants a bounded allowance (`failure_budget_topup`, default 1) and history is never erased. Rejection moves the packet to `held` with the reason.
- **C28 Numbers** [GA6, MA4, GC5, GC10; closes GE7].
  - **Fixed point.** All decision arithmetic, including governor thresholds, uses M2.1 fixed point at scale 10,000. Scores 0–10 map to score × 1,000. For example, alignment ≥ 7,500 needs a score ≥ 8, and ambiguity ≤ 4,000 needs a score ≤ 4.
  - **Settings.** Settings and weights are typed and range-checked on write. Strings, non-finite and out-of-range values are rejected. An invalid value already stored fails closed, with an event.
  - **Ruling 5 and acceptance.** Snapshots, GUI and digest apply ruling 5 (disputed rows report the max). Acceptance always checks status, not confidence alone.
- **C29 Model identity** [MA5, GA9 partial].
  - **Evaluative calls** (models A and B, and model C whenever its output feeds a decision) are fail-closed. Before every dispatch they need a verified digest for the *effective* provider/model configuration. A configuration change invalidates the verification, and the verified identity is part of the content address.
  - **Generative calls** (LG) record the model identity per `generation_runs` row. They are marked `unverified` when it can't be verified and are never labelled verified, but they are not blocked for that alone.
  - **Workbench runs** record the model reported in the conversation metadata.
- **C30 Output identity, completion and quality policy** [GA7, MA6, GC4, GC13, GA10]. Replaces "content-evaluation packet" and C18's open policy.
  1. **Quarantine and static validation.** Each output set gets a canonical manifest (sorted relative paths + sha256) with its own manifest hash.
  2. **Write and commit.** An authorised write and commit records the manifest hash and commit SHA before anything else. The packet moves to `evaluating_post`, still unlocked.
  3. **Post-evaluation and scan.** Post-phase evaPackets bind to the manifest hash, and the scan binds to the commit SHA.
  4. **Completion.** It requires a complete, accepted evaluation for every required post field for *that* manifest hash, plus the quality policy below. Evidence for a different manifest or commit (branch advanced, merge changed bytes, late scan of an old SHA) is stale and is rescheduled. A failed post-evaluation → `needs_review`.
  - **Quality policy:** the per-project setting `quality_scan` = `off` | `optional` (default) | `required`.
    - `off`: no scan is requested; it is recorded as `skipped` with an event and the packet shows as "unscanned". Scanning never blocks completion.
    - `optional`: a missing scanner or a scanner error is recorded as `skipped` and shown as "unscanned", and does not block completion. A returned gate `ERROR` raises a confirmation and blocks completion until it is decided.
    - `required`: a missing scanner or a scanner error blocks completion; gate `ERROR` → `needs_review` plus a confirmation.
    - Hard constraints are never waivable.
- **C31 Executing generated code** [MA7, GA12 amended; defer 5].
  - **Static checks in M3.** M3 validation is static: `ast.parse`/`py_compile`, contract checks and path confinement.
  - **Execution checks.** Checks that need execution (import, pytest) run only through an injectable `IsolatedRunner` with no secrets, no DB, no network, no write access to the production repository, and bounded time and output.
  - **Default runner.** M3 ships the interface plus a `NoRunner` default, under which those checks report `blocked: no isolated runner` — **never passed**. A required execution check is a hard completion gate, unless its contract explicitly names it as a soft requirement. The owner may waive only such a soft requirement, through a waiver bound to version, phase and manifest. Otherwise the packet stays blocked or is closed `closed_unverified` with a reason; owner confirmation alone can never mark it `completed`.
  - **Test.** A sentinel test covers the runner contract using a fake runner.
- **C32 Editing files owned by completed packets** [GA14 partial]. Besides supersession, a packet may modify a file last written by a completed packet only when:
  - its manifest declares `modifies: <path>` with the expected base sha256, and
  - its repo write is authorised (by scope or confirmation).
  
  A base mismatch makes the run stale. Completed packets' artifact rows never change; history stays in git.
- **C33 Governor v1 — deterministic** [defer 3; MA12, MA13, GC8, GA13, GC15]. C21's signals stay; its six-rung ladder and hysteresis move to M5.
  - **Responses.**
    - Hard caps (pace budgets, packets per objective per day, fan-out) refuse the *first* excess action.
    - Post-generation alignment < 7,500 holds the branch immediately and raises a confirmation.
    - Three distinct failed validation attempts on a channel within the preceding 2 h pause that channel and raise a confirmation.
    - Pre-generation ambiguity > 4,000 sends the packet to `needs_review`.
    - 48 h with no human touch makes new dispatches in that objective require confirmation. A human touch is an owner decision or edit only, never automated events or polling.
  - **Scope states.** A scope's persisted status is `normal` or `paused`; a pause clears only on explicit owner resume. "Off-hours throttling" is a derived display condition (the off-hours multiplier is active). It is not a status and never clears a pause. Manual holds and failure-budget parking never clear automatically.
  - **What continues while paused:** exactly what C26 allows.
  - **Time.**
    - The clock is injected. `governor_timezone` is an IANA name (default `UTC`) and windows are half-open.
    - Nights are 22:00–07:00 and weekends are Saturday–Sunday, both in `governor_timezone`. During them, run limits are × 0.25, floored, and Workbench Production is 0 unless pinned.
    - Each observation counts once: re-reading the same run on every tick never counts again.
    - Missing evidence is `unknown`, never clean, and is shown.
  - **Embedding divergence** is reported as `not_configured` and is never inferred as passing.
  - **Every governor action** writes `governor_events`.
- **C34 Integration boundary and offline acceptance** [defers 1, 6; GA8, MA15, GC11, GC14].
  - **Workbench.** M3 implements a generic `SubprocessWorkbench` adapter (configurable executable, prompt on stdin, JSON on stdout, exit codes per C27) next to the default `FileDropWorkbench`. It is tested with a fake executable fixture covering exits 0–4, timeout, malformed output, and late or duplicate results. The real Juggler CLI, worktrees and the extension are **M4** (C15 moves there).
  - **SonarQube.** Adapter plus `httpx.MockTransport` tests only; a live service is optional (RUNBOOK).
  - **Offline acceptance.** All M3 acceptance runs offline, with injected process runner, adapter, clock and HTTP transport, and temporary real git repositories.
- **C35 Overrides and fairness** [MA9, GC6, MA10, GC7].
  - **Override resolution.**
    - Active overrides are resolved across the packet, branch, objective and project scopes. Any BLOCK beats HOLD, and HOLD beats every priority override. A narrower override can never cancel an ancestor's restriction.
    - Revocations name the exact override ID. Every override is bound to a version, evented, and lapses on a version change.
    - Within a priority class, order by explicit rank, then `created_at`, then `ref`.
  - **PIN and WAIVE limits.** PIN exempts a packet only from the Workbench off-hours pause; never from budgets, approvals, hard gates or holds. WAIVE applies only to named soft fields, bound to version and phase. A budget top-up is a separate owner action.
  - **Fairness.**
    - A durable per-channel counter tracks successful FORCE/PIN admissions since the last normal admission. After `fair_admission_n` (default 3), the next admission is reserved for an eligible non-forced packet: longest wait, then `created_at`, then `ref`.
    - If there is no such packet, forced work may proceed and the reservation stays due.
    - Counters move only on a successful guarded claim, atomically with it.
    - **Max-wait first (B4).** Each channel first selects among eligible packets waiting longer than `max_wait_hours` (longest wait, then `created_at`, then `ref`). This takes precedence over priority classes and the normal-admission reservation. An overdue forced/pinned claim does not clear a due reservation; a normal claim does.
- **C36 Budgets and bounded ticks** [MA11, GC9, MA14].
  - **Atomic reservation.** Before every external attempt (evaluation, generation, retry), reserve channel run capacity and objective token capacity atomically: prompt plus max output, or `workbench_token_estimate` for workbench runs.
  - **Reconciliation.** Reconcile actual usage exactly once per operation ID. Unknown usage keeps its reservation.
  - **Splits.** Reserve packet capacity before committing a split. Top-ups are owner-authorised and evented.
  - **Token exhaustion.** Objective token capacity is a hard admission limit. If an attempt's reservation exceeds what remains, it is refused before any external effect: the packet/run state is unchanged and the readiness reason is `blocked: objective_token_budget`. It is reconsidered after the budget window resets or an authorised top-up commits. Exhaustion alone never creates a scope pause.
  - **Bounded ticks.** Each tick evaluates a finite batch (`eval_batch_size`, default 8): model A, then model B, for that batch only. Post-phase and recovery evaPackets are scheduled ahead of pre-phase ones. B12's `needs_review` outcome is kept.
- **C37 Interrupts and GUI scope** [GA11; defer 4].
  - **Interrupt matching.** Extracted projects and objectives match by case-normalised name within their parent; packets match by `ref`, then by exact title within the same objective.
    - A supplied `ref` must resolve uniquely within the target objective, or that item is rejected. A name or title match counts only when it is unique; multiple matches need explicit owner selection by stable ID/ref before any change. Never pick the first match, and never create a new draft to resolve the ambiguity.
    - Matched unlocked items whose core fields changed get a superseding version (with C25's gate bump).
    - Unmatched items are new drafts; unmentioned items are untouched.
    - A locked match is changed only through an explicit supersede confirmation.
    - Late runs for superseded versions are recorded as stale.
  - **GUI scope.** The GUI is read-only for thresholds, weights, scopes and settings; those are edited through the CLI (validated per C28, evented, versioned). The GUI keeps the Miller tree, packet detail, approvals, pause/resume/interrupt, digest and console.
- **C38 Required PostgreSQL two-session tests** [GC12; extends C6/C20]. Each test asserts row counts, final states, unchanged provenance and the scenario's expected number of external effects. A successfully authorised operation produces exactly one effect, including after replay or recovery. One rejected by governance, pause, fencing or budget produces zero. No scenario may ever produce a duplicate effect. Scenarios:
  - concurrent claims of one packet;
  - a governance write between readiness and claim (waiver revoke, BLOCK, approval change, context-head edit);
  - two claimers racing for the last budget unit;
  - approval/rejection racing a parked run;
  - a late or duplicate inbox result after lease takeover;
  - a crash after git commit but before the DB acknowledgement;
  - pause racing dispatch authorisation (one effect if the authorisation commits first, zero if the pause does).

**Deferred beyond M3:**
- real Juggler integration (M4);
- the isolated runner's real implementation (M4/M5);
- the six-rung governor ladder with hysteresis (M5);
- embedding divergence;
- GUI CRUD forms;
- live SonarQube provisioning;
- console wording for idle/paused/blocked states (GA15, backlog).
