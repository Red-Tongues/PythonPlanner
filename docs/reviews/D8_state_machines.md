# D8 — Packet and run state machines (design reference for M3 C23)

**Author:** Gemini Argon (design), 2026-11-04. **Reconciled by the PM**, with rulings R1–R5 at the end. It was drafted from the M3 spec before the V1–V11 corrections, and gaps already fixed by those corrections are noted.
**Status:** reference for the engineer. C23 requires the engineer to publish the final tables in `docs/ARCHITECTURE.md`, and tests reject every transition not listed. Deviations from this reference must be listed in the PR with a reason.

**Notation.** `G` = gate_revision; `F` = fencing token; `H` = input hash (context fingerprint); `M` = manifest hash.
- *overridable* = {draft, evaluating_pre, ready, queued, needs_review, failed, awaiting_confirmation}.
- *non-terminal* = every state except completed and closed_unverified.

## 1. Packet states
| State | Meaning | Terminal | Locked | Satisfies dependency |
|---|---|---|---|---|
| draft | ingested; pre-evaluation not started | no | no | no |
| evaluating_pre | tiered pre-phase evaluation | no | no | no |
| ready | readiness predicate true | no | no | no |
| queued | admitted, awaiting claim | no | no | no |
| running | claimed; a run is in flight, or accepted and awaiting write | no | no | no |
| evaluating_post | committed (M + commit SHA recorded); post-evaluation and scan | no | no | no |
| completed | post-evaluation accepted for M, quality policy met (or roll-up) | **yes** | **yes** | **yes** |
| needs_review | dispute, B12 split unresolved, ambiguity > 4,000, post-eval failure, required-scan ERROR | no | no | no |
| held | HOLD override, alignment hold, rejected confirmation | no | no | no |
| blocked | BLOCK override, no workbench consumer, NoRunner execution gate, required scanner missing | no | no | no |
| failed | latest run exhausted its attempts or hit exit 2; awaiting backoff, budget or config change | no | no | no |
| waiting_for_children | split trunk awaiting roll-up | no | no | no |
| awaiting_confirmation | owner decision pending (exit 3, failure budget, 48 h touch, `confirm` scope, optional-scan ERROR) | no | no | no |
| closed_unverified | user closed without verification (with a reason) | **yes** | **yes** | **no** |

**Versions (PM correction).** A core-field edit, interrupt supersession or locked-match supersede never moves an existing row's status. It creates a **new version row** in `evaluating_pre` (`supersedes_id` = old). The old row keeps its state: a completed row stays completed and locked, and an unlocked old version stops being current. Rows written "X → evaluating_pre (new version)" below mean this.

## 2. Packet transitions (condensed)
| From → To | Trigger · actor | Guard (clauses) |
|---|---|---|
| draft → evaluating_pre | tick step 2 · kernel | H = current fingerprint (B9, C25) |
| evaluating_pre → ready | readiness · kernel | required tiers accepted or soft-waived; no hard failure; dependencies completed; approvals done; ambiguity ≤ 4,000; H current (C12, C28, C33) |
| evaluating_pre → needs_review | evaluation · kernel | tier-1 disputed/failed, unresolved B12 escalation, or ambiguity > 4,000 |
| evaluating_pre → awaiting_confirmation | promotion/branch · kernel | scope = confirm; one confirmation created |
| evaluating_pre / ready → waiting_for_children | split · kernel | depth < 2; descendant cap; packet capacity reserved (C36); branching scope auto |
| ready → queued | admit_next · kernel | guarded UPDATE on status, version, H, G; rowcount 1; no BLOCK/HOLD; within failure budget (M2.1 #11) |
| ready / queued / failed → evaluating_pre | fingerprint or readiness changed · kernel | H ≠ current, or not ready (C25; kernel *commits*, M2.1 ruling 4) |
| queued → awaiting_confirmation | failure budget exhausted, or 48 h touch · kernel/governor | one confirmation (C27, C33) |
| queued → blocked | WP routing with no consumer · kernel | C5 |
| queued → running | claim · kernel | guarded UPDATE (status, version, H, G, lease); not paused (C26, C33); max-wait/fairness (C35); budget reserved (C36); off-hours/PIN rule; snapshot + F incremented in the same transaction |
| running → evaluating_post | write + commit · kernel | run accepted; run F = packet F; G and H match; HEAD as expected; ownership rule (C32); write authorised; record M + commit SHA |
| running → awaiting_confirmation | exit 3, or repo_write scope = confirm · kernel | resume identity persisted (C27) |
| running → blocked | execution check under NoRunner · kernel | C31 |
| running → failed | run failed · kernel | F matches; exit 1/4/validation → failed_runs + 1; exit 2 → channel paused, no budget count |
| running → evaluating_pre | stale run · kernel | **only if** packet still `running` on the run's version, with no hold/block/pause/confirmation (C25 as amended by V2) |
| failed → queued | retry · kernel | within budget and backoff elapsed, or (exit 2 and config revision changed); H current |
| failed → awaiting_confirmation | budget exhausted · kernel | one confirmation (C27) |
| evaluating_post → completed | completion · kernel | complete accepted post-evaluation for every required field **for M**; quality policy met (C30); locks |
| evaluating_post → needs_review | post-eval failed/disputed, or required-scan ERROR · kernel | evidence for M / commit SHA |
| evaluating_post → blocked | required scanner missing or errored · kernel | C30 |
| evaluating_post → awaiting_confirmation | optional-scan ERROR · kernel | C30 |
| evaluating_post → held | alignment < 7,500 · governor | branch held + confirmation (C33) |
| waiting_for_children → completed | roll-up · kernel | ≥ 1 child and **all** children completed (closed_unverified children don't count); locks |
| awaiting_confirmation → running | exit-3 approval · user | R1 below; H current; resumes the same conversation once |
| awaiting_confirmation → queued | failure-budget or 48 h approval · user | H current; allowance + `failure_budget_topup` (history kept); touch updated |
| awaiting_confirmation → evaluating_post | repo_write or optional-scan approval · user | (commits first if repo_write) |
| awaiting_confirmation → completed | optional-scan approval when post-evaluation already accepted for M · user | locks |
| awaiting_confirmation → waiting_for_children | split approval · user | capacity reserved |
| awaiting_confirmation → held | rejection (any type) · user | reason required (C27, R3) |
| awaiting_confirmation → evaluating_pre | approval but H stale · kernel | C25 |
| *overridable* / held → blocked | BLOCK · user | G bumped in scope (C35) |
| *overridable* → held | HOLD or alignment hold · user/governor | no active BLOCK in scope |
| blocked → held | last BLOCK revoked, HOLD remains · user | — |
| blocked → evaluating_pre | last BLOCK revoked, or blocker cleared; no HOLD · user/kernel | re-verify under the new G |
| held → evaluating_pre | last HOLD revoked, or owner clears governor hold · user | never automatic (C33) |
| needs_review → evaluating_pre / evaluating_post | soft waiver for the phase, or definition/exemplar edit · user | G bumped |
| *non-terminal* → evaluating_pre (new version) | core edit or interrupt · user/kernel | unlocked; versioning rule above (C37) |
| completed → evaluating_pre (new version) | supersede confirmation on a locked interrupt match · user | old row untouched (C37) |
| *non-terminal* → closed_unverified | close (branch kill → `blocked` via BLOCK, per D9-R1) · user | reason required; locks; in-flight runs staled |

## 3. Run transitions (`generation_runs`)
| From → To | Trigger · actor | Guard |
|---|---|---|
| — → planned | claim | same transaction as the packet claim: F, lease, frozen snapshot (incl. G, H, model identity), budget reservation |
| planned → dispatched | adapter dispatch | F current; lease valid; not paused (serialised with pause); operation ID + first `channel_runs` row before any effect |
| planned / dispatched / awaiting_result / validating → stale | F superseded, G or H mismatch, or packet closed/held/blocked | releases any unused reservation |
| dispatched → awaiting_result | payload written / process launched | deadline set; idempotency key |
| dispatched / awaiting_result → failed | exit 2 | channel paused; no packet budget charge |
| awaiting_result → validating | exit 0 before the deadline | F and lease valid; output quarantined; usage reconciled once |
| awaiting_result → awaiting_result | exit 1/4 with attempts left | new `channel_runs` row; retry capacity reserved |
| awaiting_result → failed | exit 1/4 on the last attempt | failed_runs + 1 |
| awaiting_result → awaiting_confirmation | exit 3 | resume identity; confirmation; no counters |
| awaiting_confirmation → dispatched | approval | R1; resumes once |
| awaiting_confirmation → failed | cancellation after rejection | no budget charge (C27/V6) |
| awaiting_confirmation → stale | non-approval governance change or H change | — |
| validating → accepted | static, contract and path checks pass; execution checks passed by an isolated runner where required | F, G, H match; compute M |
| validating → awaiting_result | validation failed, attempts left | error fed back |
| validating → failed | last attempt failed (failed_runs + 1), or NoRunner gate (no charge; packet → blocked) | — |
| accepted → stale | before the commit is recorded: F/G change, HEAD moved, or `modifies` base mismatch | R4 |

## 4. Forbidden transitions to test explicitly
1. completed → anything (a supersede only creates a new row).
2. closed_unverified → anything; it never satisfies a dependency or a roll-up.
3. running → completed (skipping evaluating_post).
4. evaluating_post → completed on evidence for a different M or commit SHA.
5. A stale run moving a held, blocked, closed or newer-version packet.
6. blocked → held when a HOLD is *added* (BLOCK beats HOLD; record the override, keep blocked).
7. held → ready/queued on a tick with no owner action.
8. queued → running while paused, or with PIN used to bypass a budget.
9. An exit-3 approval resuming after a concurrent revocation or context edit.
10. Roll-up completing when any child is closed_unverified.

## 5. PM rulings on Argon's gap list
- **R1 Approval self-invalidation (new; adopted).** An approval aimed at a specific run checks `run.G == packet.G`, then increments **both** to G + 1 in the same transaction. Any *other* governance write still makes the run stale. → C25.
- **R2 Validation streak counts runs, not attempts (new; adopted).** The C33 channel pause counts *generation runs* that ended `failed` on validation (3 within 2 h). One packet using up its own retries must not pause the whole channel. → C33.
- **R3 Optional-scan rejection (new; adopted).** Rejecting an optional-scan ERROR confirmation → `held` with the reason, consistent with C27. → C30.
- **R4 `accepted → stale` (new; adopted).** `accepted` is final only once the write and commit are recorded. Before that, a fencing/gate change, a moved HEAD or a `modifies` base mismatch makes the run stale. → C24.
- **R5 Already fixed:** the unconditional stale reset (fixed by V2) and the NoRunner deadlock (fixed by V5: a hard gate unless the contract names it soft; otherwise blocked or closed_unverified).

## 6. Errata E1–E11 (independent GPT-6.1 evaluation, 2026-11-04; all adopted, binding)
These override the rows above.
- **E1 Promotion approval exit.** Add `awaiting_confirmation → evaluating_pre` (candidate-promotion approval). The confirmation targets the current version and context; apply the promotion and the C25 gate bump atomically; recompute readiness. A stale approval doesn't apply.
- **E2 Exit-3 rejection keeps the run parked.** On rejection the packet goes to `held` (with a reason) and the run *stays* `awaiting_confirmation`. That rejection is excluded from the generic "non-approval governance change → stale" rule; later unrelated changes still stale it. `awaiting_confirmation → failed` happens only on explicit cancellation.
- **E3 One dispatch guard for retries and resumes.** Before *any* retry or resume effect (exit 1/4 retry, validation retry, exit-3 resume), the run:
  - revalidates version, F, G, H and the lease;
  - authorises dispatch atomically against pauses (C26);
  - reserves channel and token capacity (C36);
  - persists the operation ID and the attempt row.
  A refused reservation leaves packet and run state unchanged. Exit-3 resume additionally applies R1, and resumes the same conversation exactly once.
- **E4 Repo-write approval uses the full publication guard.** `awaiting_confirmation → evaluating_post` (repo_write) requires everything `running → evaluating_post` requires:
  - accepted run; F current; H and G match (R1 for the targeted run);
  - expected HEAD; C32 ownership and base hash; write authorised;
  - an idempotent write and commit; M and the commit SHA recorded before entering `evaluating_post`.
  A mismatch stales the run.
- **E5 Completion guards (both completion rows).**
  - Every required field has a complete, accepted, current post-evaluation for M, from C29-verified evaluators.
  - The scan is bound to the recorded commit SHA. All hard gates are satisfied, and no hold or block applies.
  - An optional-scan approval covers only that gate ERROR, never a hard constraint.
- **E6 Required-scan outcomes split.**
  - Scanner unavailable, or execution/report retrieval failed → `blocked`.
  - A returned gate `ERROR` (bound to the commit SHA) → `needs_review` + a confirmation.
- **E7 Stale reset requires run ownership.** `running → evaluating_pre` only if the packet's active run ID **and** F match this run, the packet is still `running`, and no hold, block, pause or confirmation applies.
- **E8 `accepted → stale` also on an H mismatch or a superseded version**, before the commit is recorded. Publication is prohibited, and only unused reservations are released.
- **E9 Admission and claim run the single pure readiness predicate (C12)**, after recomputing H. Matching H and G alone is not enough.
- **E10 Split rows (auto and confirmed)** check depth < 2, the descendant and fan-out caps, atomic capacity reservation, and B5 idempotency on (packet id + version, rule id, rule version). The confirmed split also validates the confirmation and applies C25.
- **E11 Deadline expiry counts as a timeout failure** (equivalent to exit 4), recorded once per operation ID, in `dispatched`/`awaiting_result`. A retry goes only through E3; on exhaustion the run → `failed`, and the owning packet → `failed` with `failed_runs` + 1 exactly once. Late results never revive an expired attempt.
