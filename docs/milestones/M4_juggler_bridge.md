# M4 — Juggler bridge extension (workbench autonomy)

**Goal:** let the orchestrator drive complex work in juggler.studio without a human relaying prompts, by shipping a Juggler extension that reads the orchestrator's Workbench Production queue and returns validated results. This turns the M3 file-drop workbench into a real, unattended consumer. CFO decision 2026-11-03: *"an extension for Juggler is exactly what is needed."*

Serves requirement **D** (complex tasks on a readable workbench) and **E** (agent-independence scopes) · Key Idea K1, K2, K4. Builds on M3's `WorkbenchAdapter`, durable runs (C1) and non-blocking channels (C2).

## What the Juggler SDK offers (researched 2026-11-03, juggler.studio/extensions.html)
- Extensions are JavaScript modules importing public `juggler/*` modules. Each class declares `static MANIFEST` (`id`, `name`, `version`, `description`) and is the default export. The SDK and built-in extensions are Apache-2.0; the core app is AGPL-3.0 (personal use — no licensing action needed; revisit if products are sold).
- Seven capability types:
  - **Context item**: a model-callable tool (`getToolDefinitions()` → `name`, `category`, `description`, `input_schema`; `execute(params)`; `getSummary(outcome)`), with an optional approval gate.
  - **Strategy**: agent-loop policy covering available tools, auto-approval and model guidance.
  - **Command**: a slash command.
  - **Workspace provider**, **File viewer**, **Info card** (sidebar tile) and **Pinboard tab** (display-only).
- Extensions are unsandboxed and declare the host access they use. Helpers cover filesystem, shell, search, web and UI.
- CLI: `juggler ext init <name>`, `juggler ext validate <dir>`, `juggler ext link <dir>` (symlink into `~/.juggler/extensions`, hot reload on save).
- **Not documented on the public page:** programmatic conversation creation/selection, model selection, prompt insertion and output events. The SDK source (`juggler-ai/juggler`, `examples/extensions`) is the canonical reference, so a spike must confirm these before the design is final.

## Deliverables
1. **Spike (first, time-boxed):** read the SDK source and examples; record in `docs/integrations/juggler.md` exactly which of the draft's Tool Design capabilities are possible — conversation selection, model selection, prompt insertion, model outputs, error messages — and how. Decide between *pull* (extension fetches work) and *push* (orchestrator triggers Juggler).
2. **Orchestrator side:** a small local bridge API on the kernel (`127.0.0.1` only, token from env), or the existing file-drop folders if the spike shows HTTP is unnecessary:
   - `GET /workbench/next` returns the next eligible Workbench Production run: prompt packet, response contract schema, context refs, `run_id`, `deadline_at`.
   - `POST /workbench/{run_id}/result` returns the output for Pydantic validation.
   - `POST /workbench/{run_id}/error` reports a failure.
   - All calls are idempotent (C1) and evented.
3. **Juggler extension `orchestrator-bridge`** (in `integrations/juggler/orchestrator-bridge/`):
   - **Command** `/orch-next`: claims the next run and inserts the packet as the working prompt in a new thread named `<run_id> <packet ref>`.
   - **Context items**: `orchestrator_get_packet` (read-only context fetch) and `orchestrator_submit_result` (submits the structured result; approval gate on when the autonomy scope is `confirm`).
   - **Strategy** `orchestrator-packet`: restricts tools to what the packet's contract needs, keeps destructive actions behind approval, and injects the response contract and Key Idea guardrails as model guidance.
   - **Info card**: live console (current run IDs per channel, queue depth, paused or running).
   - **Pinboard tab**: packet detail, evidence and acceptance criteria.
   - If the spike confirms programmatic thread/model control: an *autopilot* mode that pulls the next run when the previous one finishes, within the pace budget (see the pace & drift governor).
4. **Docs:** install steps (`juggler ext validate` / `link`), trust note (unsandboxed; host access declared), and how the autonomy scopes map onto Juggler approvals.
5. **Tests:** orchestrator-side API tests (claims, idempotency, stale-result rejection, contract validation); extension unit tests with mocked SDK base classes where feasible; one manual end-to-end check script.

## Acceptance criteria
1. Spike findings documented, with a clear yes/no for each Tool Design capability.
2. With Juggler running and the extension linked, a Workbench Production packet goes from queue → Juggler thread → validated result → artifact in the production repo with no copy-paste.
3. Results are bound to `run_id`, input hash and policy revision; stale or duplicate submissions are rejected.
4. When Juggler or the extension is absent, packets show as `blocked: no workbench consumer` while other work continues (C5).
5. Tests green; `docs/PROGRESS.md` updated.
