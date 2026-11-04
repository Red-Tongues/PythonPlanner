# D10 — Kernel tick loop and channel dispatch (design reference for M3 Stage B)

**Author:** Microsoft Copilot GPT-6.1 Sol (design), 2026-11-04. **PM rulings Q1–Q16** at the end are binding. Verification by Gemini Argon is pending.
**Builds on:** Stage A (transition guard, 0007, gate revisions, fencing, kernel lock), D8 with errata E1–E11, and D9.

## 1. Execution model
- **One kernel process**; GUI and CLI run owner commands concurrently through the **same domain services**.
- **Three kinds of work:**
  - `DB_TX`: short transactions.
  - `IO`: no transaction open; bounded and non-blocking (start/poll only; never wait for a model, process, inbox file or scanner).
  - `PUBLICATION_TX`: serialises git publication against governance and kernel takeover, and contains only bounded local git work.
- **Invariants:**
  - every mutating transaction checks kernel ownership;
  - every generation step checks version, active run, F, H, G and the lease;
  - every transition goes through the Stage A guard;
  - **no effect before intent**: authorisation, operation ID, reservation and attempt evidence are committed before any adapter call;
  - no hidden model calls (promotion, split planning and interrupt extraction use the same dispatch path);
  - acceptance, publication and completion stay distinct.
- **Durable operation journal** (in 0007, or a later migration; never edit a frozen one):
  - fields: `operation_id` (allocated once), kind (evaluate/generate/resume/publish/scan), owner, attempt number, resume sequence, version/H/G/F, configuration revision, request hash, authorised_at, deadline_at, reservation, receipt hash, outcome;
  - a retry is a new operation; recovery reuses the ID; an exit-3 resume is a new operation on the same run and conversation.

**Error classes:**
| Code | Error | Handling |
|---|---|---|
| L | lost claim, zero-row update, obsolete fence, kernel lost | roll back; no effect; continue other work only while still the owner |
| S | SQLite lock at admission/claim | treat as a lost claim; retry next tick (M2.1 ruling 5) |
| H | context mismatch at admission/claim | **commit** the return to `evaluating_pre` (ruling 4) |
| B | reservation refused | no effect; state unchanged; exact readiness reason shown |
| T | deadline expired | exit-4 equivalent, once per operation; late results rejected; retry only through E3 |
| X | invalid settings, unverifiable evaluator, invariant broken | fail closed for that action, event it, other channels continue |

## 2. Tick (C10 order)
```
tick(now):
 1 renew lock + read controls; reconcile operations; collect receipts; expire due operations once; apply owner commands
 2 schedule current evaPackets (content-address unique; tier-1 failure stops later tiers)
 3 batch = up to eval_batch_size evaPackets, ordered (recovery, post, pre, priority, id)
     for slot in (A, B): for eva in batch: advance one READY pass (authorise TX → start/poll → record TX)
     new work never joins this batch
 4 refresh projections; promotions and branch triggers (no provider calls; split planning goes through dispatch)
 5 for LG, WP: select → admit (TX) → claim_and_plan (TX: recompute H + pure readiness; selection order again;
     reserve; guarded claim + snapshot + new F; fairness counter in the same TX; on mismatch COMMIT the reset)
 6 for QE, LG, WP: poll / record receipt / validate; authorise_dispatch before every first attempt, retry or resume
 7 publish accepted outputs (PUBLICATION_TX); advance scans; schedule post evaPackets after the recorded commit
 8 completion and roll-up (TX per packet); heartbeat; digest reads are read-only
 return next wake-up = min(tick interval, nearest deadline, lock renewal)
```
**`authorise_dispatch` (one TX):**
- assert kernel owner; lock the owner and dispatch-control rows;
- check version/F/G/H/lease, dispatch gates and pause;
- evaluative calls: verify the effective model configuration (C29);
- reserve capacity, or return `Refused(reason)` with no state change;
- insert the operation and attempt evidence once; transition to `dispatched`;
- **after commit**: `adapter.start()` / `resume()`.

The initial generation attempt reuses the claim's reservation; retries and resumes reserve again (E3).

## 3. Channel adapter protocol
```python
class ChannelAdapter(Protocol):
    def available(self) -> tuple[bool, str | None]: ...  # local check, no model call
    def start(self, req: AttemptRequest) -> Observation: ...  # returns promptly
    def resume(self, req: AttemptRequest, ident: ResumeIdentity) -> Observation: ...
    def poll(self, operation_id: str) -> Observation: ...  # never blocks
    def recover(
        self, req: AttemptRequest
    ) -> Observation: ...  # inspect evidence; never blindly re-run
    def cancel(self, operation_id: str) -> Observation: ...  # best-effort cleanup only


# AttemptRequest: operation_id, channel, owner_id, attempt_number, request_hash, payload, deadline_at,
#                 configuration_revision, resume_identity
# Observation = Receipt(exit_code, output, reported_model, resume_identity, tokens…) | Pending | Unknown(reason)
```
- **Contract:** every call returns within a bounded local time. Deadlines are persisted before dispatch, and polling never extends them. Receipts echo the operation ID and request hash; duplicates are harmless. Adapters hold no DB connection, credentials or production-repo write access.
- **QE:** wraps the M2 provider through an injected async handle. Verifies the digest before every dispatch, and persists each pass (never a blocking `evaluate_pending()` inside the tick).
- **LG:** same mechanics; model identity is recorded, and "unverified" is a label, not a block.
- **FileDropWorkbench:** atomic temp-file + rename, with the JSON written last as the ready marker; the envelope carries operation ID, request hash, deadline and resume identity.
- **SubprocessWorkbench:** stdin envelope, JSON on stdout, bounded output and runtime, polled handles. No Juggler worktrees in M3.

**Exit mapping:**
| Outcome | Action | Accounting |
|---|---|---|
| 0, valid | quarantine → validating | usage reconciled once |
| 0, malformed / contract fail | failed attempt; correction fed back on retry | counts toward max attempts (QE 4 / LG 3 / WP 2); exhaustion → failed_runs + 1 |
| 1 | failed attempt; retry via E3 | as above |
| 2 | run failed; channel paused (`configuration_error`) | no packet budget charge |
| 3 | run parked; resume identity kept | no count; a resume reserves again |
| 4, or deadline reached | timeout failure, once per operation | as a failed attempt |
| NoRunner on a required execution check | packet `blocked` | no packet budget charge |
| unknown exit / signal / launch failure / bad exit-3 metadata | see Q10 | — |

## 4. Crash recovery
| Boundary at crash | Reconciliation |
|---|---|
| intent written | If authorisation never committed, don't dispatch. If committed, `recover()` first; an unknown outcome is never a licence to re-dispatch (Q1). |
| dispatched | Poll/reconnect; if the deadline has passed, record E11 once. A restart never allocates a new operation. |
| result received | Verify the receipt hash and binding; restore quarantine from the durable receipt. Acceptance is unique per operation. |
| validated | Re-run deterministic static checks if evidence is incomplete; no generation replay. |
| accepted | If no publication receipt: re-check version/F/G/H, HEAD, ownership and authorisation, then publish or stale (E8). |
| committed, DB not acknowledged | Find the publication in git by the operation marker; **record the existing commit**, never publish again. |
| recorded | Schedule any missing exact-M post evaPackets and exact-SHA scan idempotently; run the completion guard. |

**Git publication protocol (outline):**
1. Prepare outside a TX: quarantined files, confined paths, static checks, M, objects in a **private index**.
2. Persist the publication intent (operation ID, expected HEAD, M, branch, deterministic commit metadata).
3. In `PUBLICATION_TX`: lock owner and governance rows; re-check everything; create or recognise the commit (its message carries the operation ID); **compare-and-swap the branch ref** from expected HEAD to the commit SHA; insert artifacts and the publication receipt; packet → `evaluating_post`.
4. After a crash between the ref update and the DB commit, reconcile the marker before any new publication.

## 5. Pause, drain, interrupt
- **Pause** locks the same dispatch-control row used by authorisation.
  - Authorised before the pause → in flight, and drains. Pause first → no generation, generation retry or pre-evaluation.
  - Reconciliation, collection, validation and already-authorised publication continue.
  - Post-evaluation continues only for outputs committed before the pause, and stops if the QE channel itself is paused.
- **Display:** `PAUSED (draining)` while permitted work remains, then `PAUSED`.
- **Resume:** explicit owner action only. Off-hours never clears a pause.
- **SIGINT/SIGTERM:** stop new authorisations, collect within a bounded grace period, exit.
- **Interrupt:**
  1. Persist the request and the pause; drain.
  2. Extract (Q12).
  3. Match per C37 (unique only; ambiguous items wait for an owner choice).
  4. Supersede changed unlocked matches (C25 bumps); new drafts for unmatched items; locked matches need a supersede confirmation.
  5. Old rows keep their state; stale runs are handled under E7.
- **Parked-run decisions:**
  - approve-resume is one TX (checks, gates, pause, reservation, then both G + 1; the same conversation resumes after commit);
  - reject → packet `held`, run stays parked;
  - cancel → run `failed`, no budget charge.

## 6. Offline test plan (Stage B)
**Fixtures:** an injected clock (no sleeps), a process runner with crash hooks, a fake executable (exits 0–4, malformed, delayed, duplicate/late, deduplicating ledger), injected QE/LG/FileDrop/Subprocess adapters, an injected isolated runner and NoRunner, `httpx.MockTransport`, temporary real git repos (private index + ref CAS), a populated 0006 → head database, SQLite, and local PostgreSQL two-session tests.

**Tests by group:**
- **Tick and evaluation.** `tick_order_matches_c10`, `eval_batch_is_finite_and_frozen`, `eval_recovery_and_post_precede_pre`, `eval_pass_dependencies_survive_restart`, `tier1_failure_stops_later_tiers`, `b12_repair_split_then_review_is_bounded`.
- **Dispatch and budgets.** `slow_wp_does_not_block_qe_or_lg`, `every_external_attempt_has_intent_and_reservation`, `sqlite_lock_at_admit_or_claim_is_lost_claim`, `context_mismatch_reset_commits`, `readiness_rechecked_at_both_boundaries`, `retry_and_resume_use_e3`, `budget_refusal_preserves_owner_state`, `unknown_usage_keeps_reservation`, `usage_reconciled_once_per_operation`.
- **Exit codes and parking.** `exits_0_to_4_and_malformed_output`, `timeout_recorded_once_late_result_rejected`, `exit2_requires_configuration_change`, `exit3_resume_preserves_conversation`, `exit3_rejection_parks_cancel_does_not_charge`.
- **Governor, fairness and pause.** `validation_streak_counts_failed_runs`, `fairness_and_overdue_precedence`, `off_hours_pin_exempts_only_wp_pause`, `pause_drains_without_retry_dispatch`, `paused_post_eval_requires_pre_pause_publication`.
- **Interrupts and stale runs.** `interrupt_matching_and_supersession`, `stale_run_cannot_move_restricted_or_newer_packet`.
- **Publication and recovery.** `crash_at_each_durable_boundary` (all 7), `publication_replay_reuses_operation_commit`, `moved_head_or_modifies_base_stales_before_publish`, `obsolete_fence_cannot_publish`, `path_and_symlink_escape_rejected`, `completed_artifact_provenance_immutable`.
- **Execution gates and completion.** `norunner_never_passes_execution_gate`, `isolated_runner_sentinel`, `completion_requires_exact_manifest_and_commit`, `quality_policy_matrix`, `hard_gate_not_waivable_by_scan_approval`, `closed_unverified_never_satisfies_dependency`.
- **Digest.** `digest_get_is_read_only`, `digest_repeat_action_returns_same_receipt`, `digest_stale_action_has_zero_effect`, `cli_and_digest_share_domain_services`, `mark_seen_is_not_human_touch`.
- **PostgreSQL two-session** (C38 + C26): `pg_concurrent_claim_one_packet`, `pg_governance_between_readiness_and_claim` (parameterised), `pg_two_claimers_last_budget_unit`, `pg_parked_approval_rejection_race`, `pg_late_duplicate_inbox_after_takeover`, `pg_crash_after_git_commit_before_ack`, `pg_pause_races_authorisation`, `pg_kernel_lease_expiry_takeover`.

## 7. PM rulings on GPT-6.1's open questions (binding)
- **Q1 What C38 "exactly one effect" means (blocking → resolved).**
  - **Covered:** DB records, git branch publications, FileDrop request files and acceptance of results. These are exactly-once.
  - **Not covered:** model and subprocess *execution*, which is **at most once accepted**. An unknown outcome after a crash is never re-dispatched automatically. It waits for its deadline, is recorded once as a timeout (E11), and any retry is a new operation through E3.
  - **Requirement:** generation processes may have no side effects outside their quarantine/output directory. (Workbench worktree side effects are an M4 concern.)
- **Q2 A-then-B (accepted as proposed).** Ready A passes, then ready B passes, within each tick; dependencies carry across ticks. `eval_batch_size` counts **evaPackets**.
- **Q3 Append-only attempts (affects Stage A).** One immutable `channel_runs` row is written per attempt at authorisation. Later outcomes are separate append-only rows (e.g. `channel_run_outcomes`) or journal entries. A "latest attempt" view serves the console. *Stage A chooses the exact shape and records it.*
- **Q4 Pause/resume do not bump G.** They are controls serialised through the dispatch-control row (C26), not C25 governance writes; bumping G would stale the work that should drain.
- **Q5 Exit-3 rejection bumps packet G and run G together** (as R1 does), so the rejection doesn't stale its own run. To continue later, the owner either cancels (run → failed) or uses **"reconsider"**, which creates a new confirmation for the same run that follows R1 when approved. Un-holding the packet alone never resumes the run.
- **Q6 An approval is consumed only when authorisation succeeds** (gates + reservation). Otherwise it stays pending, with the refusal reason shown.
- **Q7/Q15 Publication.** The linearisation point is the **branch ref compare-and-swap**, with the operation ID in the commit message. The kernel **never writes the production working tree**: it publishes commits to a configured branch (default `orchestrator`) using a private index, and the owner merges or pulls. Recovery records an existing commit and never publishes again.
- **Q8 Completion evaluates the packet's own immutable snapshot** (its M and commit SHA). Later commits by other packets don't invalidate it; only a mismatch with *this* publication does.
- **Q9 Reservations.** Run capacity counts in the window of the **authorisation** time. Unused token reservation is released at outcome reconciliation, and unknown usage stays reserved. Windows roll over on the injected clock. Recovery keeps existing reservations.
- **Q10 Unmapped outcomes fail closed.**
  - A missing or unlaunchable executable is configuration, treated as exit 2.
  - An unknown exit code or signal death is a failed attempt (counted).
  - Malformed exit-3 metadata is a failed attempt; it is never parked without a resume identity.
- **Q11 QE attempts.** Each pass's provider call has up to 4 attempts. B12 repair and split are *new* evaluation work with their own attempts, bounded by B12 (one repair, one split, then `needs_review`).
- **Q12 Interrupts while paused** use **deterministic extraction only**. LLM extraction runs only when the owner explicitly requests it (`orchestrator interrupt --extract-now`): an owner-authorised operation through the normal dispatch guard, the single documented exception to pause.
- **Q13 FileDrop paths.** `workbench/outbox/<run_id>/<operation_id>.{md,json}` and `workbench/inbox/<run_id>/<operation_id>.json`. Consumer presence is a heartbeat file (`workbench/consumer.heartbeat`) refreshed within `consumer_heartbeat_minutes` (default 10); a stale heartbeat → `blocked: no workbench consumer`.
- **Q14 Missing D8 edges (affects Stage A).** Stage A enumerates explicit rows for recovery from post-phase blockers:
  - `blocked`(scanner) → `evaluating_post` when the scanner is available again;
  - `needs_review`(post) → `evaluating_post` through a phase-post soft waiver, or a new version;
  - outcomes of run cancellation on the packet (→ `held` stays held; the run → `failed`).
  
  Every other edge is rejected and tested; deviations are listed in the PR.
- **Q16 Typed action bindings.** Packet actions bind (version, G). Run actions bind (run ID, run G). Objective top-up binds an **objective budget revision**, the one objective-wide GUI action (an explicit exception to D9-R6). Pause binds a **control revision**.
