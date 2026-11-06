# R5 - Independent audit of the CFO T1-C v2 directive (pre-M3a)

Date: 2026-11-06

**Inputs:** Argon (Gemini) deep audit of the directive design; R&D (Microsoft Copilot,
Think deeper) critique of the relational queue score; GitHub Copilot CI/branch report.
Product decisions remain the CFO's; PM decisions are triage and recommendations.

## Argon findings

| # | Severity | Finding | PM decision |
|---|---|---|---|
| A1 | HIGH | In-place edits on ready/queued plus `binding_mismatch` ignoring `gate_revision` let a run claimed at T0 be sealed with a row edited at T1. | **ACCEPT.** New OQ29: the seal binds the packet state captured at claim (`content_revision` or a `field_revisions` pointer on the run), and in-place edits to packets at genQueue positions 0-2 are refused or deferred to the child. |
| A2 | HIGH | Always-spawn children plus no K=3 breaker give unbounded lineage and crash loops. | **ACCEPT.** Same gap as OQ14 (stop rule) and OQ18 (crash ceiling); PM recommends both be numeric settings. |
| A3 | HIGH | The lockout set comes from a `ROW_NUMBER` view that evaluation output itself changes, so positions shift mid-evaluation. | **ACCEPT.** New OQ30: an evaluation already running when its packet enters positions 0-2 finishes, but its write re-checks the lockout in the same compare-and-set and is discarded on conflict (PM recommendation). New OQ31: compute the lockout set from a snapshot persisted at each admission tick, not live. |
| A4 | HIGH | Claims ready/queued are trigger-locked, so raw SQL needs carve-outs. | **PARTLY REJECTED:** factually wrong; only completed/sealed rows are locked. **ACCEPT residual:** outbox-trigger vs transition-trigger order must be fixed and tested on both dialects (new OQ32 and an acceptance criterion). |
| A5 | MEDIUM | `gate_revision` loses its meaning. | **ACCEPT.** New OQ33: PM recommends `gate_revision` for governance only plus a new `content_revision` bumped by the outbox trigger so CAS callers detect raw edits (links OQ19). |
| A6 | MEDIUM | Upstream in-place edits leave verified quotes pointing at text that no longer exists. | **ACCEPT.** New OQ34: `question_context` also flips when a cited quote no longer verifies against its current source. |
| A7 | MEDIUM | SQLite lock pressure from heavy triggers and recursive CTEs. | **NOTE.** Add a SQLite WAL concurrency acceptance test. |
| A8 | LOW | Write amplification. | **NOTE.** `content_blobs` is already sha256-addressed; keep the unique constraint. |
| A9 | P1 | Outbox drain sync vs async; seal must flush pending diffs. | **ACCEPT.** New OQ35; PM recommends synchronous drain. |

## R&D: relational queue score

**Findings**

- Roots with `parent_grade` 0 inflate: any root with centrality >= 715 clears the 3500 floor.
- `question_context_count` x 1000 is unbounded, so churn can monopolise the queue.
- Centrality feeds back into churn.
- Low-centrality well-graded work can starve forever.
- Feedback can be gamed.
- Integer plateaus need explicit tie-breaks.

**Proposed fixes**

- Cap each additive term (e.g. `question_context` at 3 x 750, feedback capped and deduplicated).
- Set root `effective_parent_grade` to 5000.
- Add a bounded ageing term so every Ready packet is eventually admitted.
- Order score DESC, then `ready_since` ASC, then `packet_id` ASC.
- Use an adaptive floor tied to runnable depth plus maximum-wait admission.

**PM decision:** RECOMMEND under OQ4-OQ9 that M3a adopts bounded terms, a root
default grade, bounded ageing and explicit tie-breaks, with constants as settings.
R&D's point matches OQ5: boosting packets with questioned context pushes them into
the lockout where they cannot be refreshed.

## Other: CI and branches

- Main CI green at 71977eb and at PR #3 merge 6b89a18; earlier known issue closed.
- Open PRs #4 and #5 (draft).
- Three squash-merged source branches are cleanup candidates; review-packs kept.
