# M4 — Juggler bridge extension (workbench autonomy)

**Goal:** let the orchestrator drive complex work in juggler.studio without a human relaying prompts, by shipping a Juggler extension that reads the orchestrator's Workbench Production queue and returns validated results. This turns the M3 file-drop workbench into a real, unattended consumer. CFO decision 2026-11-03: *"an extension for Juggler is exactly what is needed."*

Serves requirement **D** (complex tasks on a readable workbench) and **E** (agent-independence scopes) · Key Idea K1, K2, K4. Builds on M3's `WorkbenchAdapter`, durable runs (C1) and non-blocking channels (C2).

## What Juggler offers — verified against the source (github.com/juggler-ai/juggler @ 0074240, read 2026-11-04)

**1. A documented headless CLI — `juggler run` (`docs/headless-linux.md`).** This is the primary integration path.
- Usage: `juggler run [--project <dir>] [--timeout <dur>] [--strategy <id>] [--json] "<prompt>"`; a prompt of `-` is read from stdin.
- The answer goes to stdout; `--json` returns the whole outcome (conversation id and folder, turns, answer).
- Exit codes: `0` completed · `1` failed or interrupted · `2` bad command line · `3` **parked — a tool wanted a human** · `4` timeout.
- Each run starts and stops its own session. The run's conversation stays in the project's `.juggler/` folder, so it can be opened and inspected in the Juggler GUI afterwards (a readable workbench, K4).
- The default strategy is `yolo` (self-approving). The orchestrator must always pass its own strategy.
- The model used is the configured default; there is no per-run model flag in the docs (to confirm in the spike).
- A project already open in Juggler can't be used by a run (exit 1), so the orchestrator gives every run its own git worktree.

**2. Juggler is an MCP client (`docs/mcp.md`).** Servers are declared in `~/.juggler/mcp.json` or `<project>/.juggler/mcp.json`, and their tools become ordinary Juggler tools with approvals. The orchestrator can expose packet context and result submission as an MCP server.

**3. Extensions (`docs/extension_guide.md`; SDK source in `web/sdk/`)** — eight capability types: Context Item (tool), **Strategy** (`GUIDANCE`, `filterTools()`), **Sub-agent** (a tool that runs a delegated child thread from a self-contained `prompt` plus a `resultSpec` return contract — the same shape as a Prompt Packet), Command, Info Card, Pinboard Item, File Viewer, Workspace Provider, and **Hook** (`beforeTool` deny/ask/note, `afterTool` note/fail — around every tool call, whatever the strategy). Also system-prompt contributions. Conversation state goes only through `MessageThread` `@plugin-api` methods (e.g. `addEvent`, `addContextItem`, `enqueuePendingItem`, `mutate`). CLI: `juggler ext init | validate | link | add`.

**4. No-code options.** Hook files in `~/.juggler/hooks/*.md` and custom slash commands as markdown files.

**Licence:** core AGPL-3.0; SDK and built-in extensions Apache-2.0. Personal use — no action needed (D16).

### Mapping to the draft's Tool Design list
| Draft capability | How M4 delivers it |
|---|---|
| Conversation selection | Each `juggler run` creates its own conversation; `--json` returns its id and folder, stored on the `channel_runs` row and linked from the GUI |
| Model selection | Default model per project via Juggler config in the run's worktree (**spike: confirm the mechanism**); a strategy can steer behaviour |
| Prompt insertion | The packet's self-contained prompt is piped to `juggler run -` |
| Model outputs | stdout / `--json` answer, validated against the packet's response contract |
| Error messages | Exit code + `status` (`failed`, `parked`, `timeout`) + the conversation folder named on every non-zero exit |

## Deliverables
1. **Spike (first, time-boxed).** Confirm per-project default-model configuration and the `--json` schema. Confirm that a custom strategy can be selected by id from an extension, and how `parked` runs can be resumed. Record findings in `docs/integrations/juggler.md`.
2. **`JugglerWorkbench` adapter (Python, replaces the M3 stub).**
   - For each Workbench Production run, create a git worktree of the production repo (`runs/<run_id>`) — the run's sandbox and quarantine (C3).
   - Execute `juggler run --json --project <worktree> --strategy orchestrator-packet --timeout <deadline> -` with the packet prompt on stdin.
   - Map exit codes to run states: `0` → validating; `3` parked → `awaiting_confirmation` (approval queue); `4` → failed attempt (timeout); `1` → failed attempt.
   - Store conversation id/folder on the run. Validate the answer and the worktree diff against the packet's response contract. Merge accepted changes into the production repo with provenance.
   - Respect the pace budget (D14). One run per worktree, never against a project open in the GUI.
3. **Juggler extension `orchestrator-bridge`** (`integrations/juggler/orchestrator-bridge/`):
   - **Strategy `orchestrator-packet`.** `GUIDANCE` injects the response contract and Key Idea guardrails; `filterTools()` limits tools to what the contract needs. Never `yolo`.
   - **Hooks (governance, K2).** `beforeTool` denies writes outside the packet's `repo_target_path` and asks before destructive commands, so a headless run parks (exit 3) rather than acting. `afterTool` notes on sandbox refusals.
   - **Info Card**: orchestrator console (current run IDs, queue depth, paused/running). **Command `/orch-status`**.
   - Optional **Sub-agent** `work_packet` for interactive use from the Juggler GUI (same prompt + `resultSpec` contract).
4. **Optional MCP server** (`orchestrator mcp serve`): read-only packet context (`orch_get_packet`, `orch_status`). Result submission stays on the CLI path.
5. **Docs and tests.** Install steps (`juggler ext validate` / `link`); adapter tests with a fake `juggler` executable covering every exit code, timeouts, parked runs and stale results; one manual end-to-end script against a real Juggler install.

## Acceptance criteria
1. Spike findings documented, with a clear yes/no for each Tool Design capability.
2. With Juggler running and the extension linked, a Workbench Production packet goes from queue → Juggler thread → validated result → artifact in the production repo with no copy-paste.
3. Results are bound to `run_id`, input hash and policy revision; stale or duplicate submissions are rejected.
4. When Juggler or the extension is absent, packets show as `blocked: no workbench consumer` while other work continues (C5).
5. Tests green; `docs/PROGRESS.md` updated.
