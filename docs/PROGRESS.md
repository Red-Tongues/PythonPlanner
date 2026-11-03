# Progress log

Newest entries first. Every agent session appends an entry: what changed, decisions, deviations from spec (with reasons), review triage, known issues, next steps.

---

## 2026-11-03 — M0: plan and repository seeded (PM)

- Converted the CFO's draft `Python Planner.docx` into `docs/PROJECT_PLAN.md` (plan v0.2): Key Idea decision filter, A–F traceability, glossary, architecture, data model, six-pass design, routing, channels, autonomy scopes, GUI, integrations, decisions, scope, milestones, risks, budget, open questions.
- Archived the original draft and flowchart in `docs/archive/` (read-only provenance).
- Wrote the agent guide `CLAUDE.md` and milestone specs M1–M3 in `docs/milestones/`.
- Findings that shaped the plan:
  - juggler.studio has no documented REST API (JS extension SDK only) → file-drop workbench adapter in v1, Juggler extension in v1.1. Core licence AGPL-3.0, SDK Apache-2.0.
  - SonarQube Community Build analyses a single branch, with no PR decoration or secrets detection → per-branch project keys; separate secrets scanner later.
  - The draft's six-pass numbering is internally inconsistent → resolved as 2 extractions + 4 combined evaluations (decision D3, pending CFO).
- **Next:** M1 kernel foundation with Claude Code.
