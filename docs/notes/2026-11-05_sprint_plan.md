# Sprint plan — 5 Nov, 02:10–03:00 (50 min)

**Labels:** BUILD PROCESS (getting R4 merged) and PRODUCT (T1-C v2). Merging still needs the CFO's click.

## State at 02:12
- **R4 groups 1–2:** done by Claude Code on PR #3 (6dae604, c83d407, ff489f6).
- **R4 groups 3–11:** done by the GitHub Copilot cloud agent. 14 files, +875 −159. SQLite gave 361 passed and 234 skipped. PostgreSQL was **not run**. The work is **unpushed**: it sits in the agent session behind "Create pull request".
  - One deviation to check: split rejection adds an `awaiting_confirmation → ready` transition that is not in D8.
- **T1-C:** audited by Argon (`docs/reviews/G1_audit_T1C.md`). Not ready. G1 was verified by the PM.
- **Q1 step 3 (GPT-6.1):** the reply was lost because the Copilot tab reset to a new chat. Re-ask during the sprint.

## Ordered plan
| Min | Who | Step |
|---|---|---|
| 0–5 | **CFO** (one click) | In the Copilot agent task "R4 safety fixes", click **Create pull request**, or tell the PM to. Ideally it targets PR #3's branch `claude/elegant-davinci-hcny5t`; otherwise a PR to `main` that the engineer rebases. |
| 0–5 | PM | Start a **fresh** Claude Code session (don't reuse the 76%-full one) with the handover below. |
| 5–30 | Engineer (fresh) | 1. Pull the agent's work onto PR #3's branch.<br>2. Run `ruff` and the full SQLite suite.<br>3. Run the **PostgreSQL suite** (`ORCH_TEST_POSTGRES_URL`); CI provides PG, so push and read CI if there is no local PG.<br>4. Fix any PG failures (G8).<br>5. Map all 16 R4 items to code and tests with three statuses: panel-supported / code-verified / test-proven.<br>6. Flag the D8 deviation; don't silently keep it. |
| 5–30 | PM + GPT-6.1 | Draft **T1-C v2** (one page): G12 Mutable Head + Immutable Seal, G4 intra-packet manifest, G1 exact bigint formula, G10 per-attempt refs, G9 breaker, G6 hash-at-write, [CFO] tags. GPT-6.1 critiques it; re-ask Q1 step 3 in the same chat. |
| every ~1 min / push | GitHub Copilot | (a) List the engineer's changes. |
| 30–45 | Deep auditor | (c) Audit the R4 diff and the 16-item map. **Argon** if she has quota; otherwise the **GitHub Copilot auto agent** (CFO instruction). |
| 45–50 | PM | Record outcomes and update PROGRESS/PLAN. Send the CFO: PR status, CI result, and T1-C v2 questions. **Do not merge.** |

## Handover for the fresh Claude Code session (paste as-is)
> You're the engineer on PythonPlanner. Read CLAUDE.md, then `docs/reviews/R4_consensus.md` (the 16 fixes) and `docs/notes/2026-11-05_sprint_plan.md`. Branch: `claude/elegant-davinci-hcny5t` (PR #3). Groups 1–2 are yours, already committed. Groups 3–11 were written by a GitHub Copilot agent and will arrive as a PR/branch; merge it into PR #3's branch. Tasks, in order:
> 1. ruff and the full SQLite suite;
> 2. the PostgreSQL suite (via CI if there's no local PG) — this is the blocker;
> 3. fix failures;
> 4. a table in the PR description mapping each R4 item → code → test, with ✅/❌;
> 5. flag that `awaiting_confirmation → ready` on split rejection deviates from D8, and propose keeping or removing it.
>
> No new features, no T1-C work. Stop when CI is green and report.

## Decisions needed from the CFO (from T1-C + G-audit), in plain English
1. **Editable packets (G12).** The live packet row stays editable, by you in SQL too. Every change is auto-copied to a history table, and each completion is frozen as a sealed snapshot that can never change. Is that your "completed work is immutable"?
2. **Upstream changes (G4).** If a project brief or another packet changes, should this packet's scores become *more likely to need a refresh* (soft), rather than *instantly invalid* (hard)? T1 said soft.
3. **Safety brake (G9).** While the failure maths is only advisory, should a simple hard brake apply after N failures in a row on the same prompt?
4. **Failures with an unknown cause (G5).** Should they only affect queue maths, or may they block the packet?
5. **Inheritance after a split (G7).** Use an automatic small capped carry-over, or ask you to confirm each cause?
