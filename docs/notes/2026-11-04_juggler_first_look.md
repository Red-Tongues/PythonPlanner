# Juggler — first look at the source (PM, 4 November 2026)

**Source:** public repo `juggler-ai/juggler` @ 0602747 (v0.7.9; Go server + JavaScript engine). Read-only inspection.

## What already exists
- **`juggler run --json` (headless, one prompt).** Exits 0 completed / 1 failed / 2 bad command line / 3 parked (a tool wanted a human) / 4 timeout. The JSON includes conversation id and folder, turns and the answer. This matches the M4 assumptions.
- **Conversations live in `<project>/.juggler/`.** Sessions are stored and synchronised as **Yjs documents** (README), *not plain files*. Within a session the SDK exposes a flat list of typed messages that **includes `thinking` blocks**, alongside tool calls and results *(corrected after audit A1, finding A24)*. So the recorded thinking the CFO needs is stored by Juggler.
- **An internal HTTP API** on the server, including `/api/conversations`, `/api/session/conversations/…`, `/api/session/workspaces`, `/api/git/diff`, `/api/ops/call`, `/api/health`. It's the API the Juggler UI itself uses. Its auth and stability for outside callers still need checking.
- **Extensions (8 capability types)**, unsandboxed JavaScript with full app privileges:
  - **Strategy:** controls the agentic loop and can read the whole transcript (`messageThread`), including thinking, and do external work at turn end.
  - **Hook:** policy and notes around every tool call.
  - **Workspace Provider:** builds the place a conversation works in, headlessly (e.g. a git worktree).
  - **Info Card, Command, Context Item, Pinboard Item, File Viewer;** plus system-prompt contributions.

## What this means for the add-on ("expose an API")
We probably don't need to rebuild Juggler's thinking capture. The add-on can most likely be a **Juggler extension** — a Strategy and/or Hook, plus possibly a Workspace Provider — that:
1. receives a genPacket's prompt;
2. runs it;
3. writes the outcome, the answer **and the recorded thinking** into a place the kernel reads.

Alternatively the kernel could read `.juggler/` conversation folders after `juggler run --json`, or call the internal API.

## Open questions (next step)
1. Storage format of the conversation folder (readable JSON, or a binary sync format?).
2. Whether the internal API is safe and stable to call from the kernel (auth token, versioning).
3. Which route the panel recommends: extension, folder reading, or API.
4. Whether a supervised look at the CFO's running Juggler is still needed after the code review.


**Audit A1 cautions (A25, A26).** Treat the internal API as unstable until it's contract-tested, and prefer `juggler run` as the first boundary, pinned to an exact release. Treat recorded thinking as *optional, sensitive telemetry* (possible secrets, source, personal data; not guaranteed faithful). Decisions rely on observable outputs, tool traces, diffs and tests.
