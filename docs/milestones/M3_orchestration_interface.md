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
