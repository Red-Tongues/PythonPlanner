# Review R2 — consensus on M2 code (PR #2) and the M2.1 fix list

**Subject:** PR #2 @ fbd8856 (helper script reverted at 0e83b02) · **Date:** 2026-11-04 · CI green; 350 tests.
**Panel:** GE = Gemini Argon · MC = Microsoft Copilot GPT-6.1 · GH = GitHub Copilot. **Method:** as R0/R1 — `confidence = (0.6 × mean cross + 0.4 × self)/10`.

| Verdict | Findings |
|---|---|
| **ACCEPT (21)** | GE1 0.88 · GE2 1.00 · GE3 0.94 · GE4 0.77 · GE5 0.78 · GE6 0.87 · GE9 0.80 · MC1 1.00 · MC2 0.94 · MC3 1.00 · MC4 0.97 · MC5 0.84 · MC6 0.90 · MC7 0.90 · MC8 0.91 · GH1 1.00 · GH2 1.00 · GH3 0.93 · GH4 0.96 · GH5 0.86 · GH6 0.83 · GH7 0.77 |
| **DEFER (1)** | GE7 0.67: numeric settings stored as JSON strings break the casts in the views → M3 backlog (validate numeric settings on write) |
| **DECLINE (2)** | GE8 0.49: the `writable_schema` edit is documented and tested, and both other reviewers contradicted the finding · GE10 0.40: refusing unverifiable pinned specs is intentional under B11; the real defect is the *opposite* (MC8, fail-open) |

PM verified in the code before relaying: GE1, GE2, GE3, GE9. The contested pair GE10/MC8 is resolved in MC8's favour: the provider must **fail closed**.

## M2.1 fix list (on PR #2, before merge) — new frozen migration **0006**; never edit 0001–0005
1. **Context binding** (MC1, GH1). Recompute the input fingerprint from the *current* packet, project and objective heads at evaluation writes, at admission and at claim. A mismatch sends the packet back to `evaluating_pre`, with an event.
2. **Claim atomic with respect to governance** (MC2, GH3). Waiver, override, revocation, dependency and approval writers increment a per-packet **gate revision**. The claim's guarded UPDATE includes it, and the snapshot is built in the same transaction from the same gate state. Add two-session barrier tests on both dialects (revoke a waiver / add a BLOCK between check and claim).
3. **Post-phase artifact binding** (GE2, MC3, GH2). Post-phase consensus is selected only for the exact artifact hash being completed. Every required post field needs a *complete, accepted* evaluation for that hash. Test: accepted artifact A, then an incomplete evaluation of artifact B, must not complete.
4. **Definition, registry and preset binding** (GE3, MC4, GH4). The content address includes the effective field-definition fingerprint (version, prompt hint, label, kind, limits) and the selected exemplars. Current consensus requires the active preset revision and registry revision; anything else is `pending`. Add tests that change only the definition, only the exemplars, or only the preset revision.
5. **Manual completion obeys gates** (GE1, MC6; reverses judgement call #9).
   - The completion gate applies to user actors too.
   - Hard gates are never bypassed; soft gates only through a version- and phase-bound waiver.
   - Closing a packet without evaluation gets a distinct non-success disposition (e.g. `closed_unverified`), never `completed`.
6. **Immutability of the new tables** (GE6, GH5, MC7).
   - `evaluation_cycles` binding columns are frozen after insert.
   - `packet_dependencies` is append-only (changes go through revocations).
   - eva_packet identity columns (`target_hash`, `model_config`, `content_address`, preset/cycle refs) are immutable from insert.
   - BEFORE TRUNCATE guards on `generation_runs` and every M2 append-only table on PostgreSQL.
   - Raw-SQL tests on both dialects.
7. **Fixed-point arithmetic** (MC5). Weights, thresholds and confidences are computed in integer fixed point (e.g. scaled by 10⁴) identically in the oracle and both dialects. Add MC5's exact case and configurable-weight boundary cases to the parity tests.
8. **Model pinning fails closed** (MC8). Every non-fake evaluator must be pinned and verified before evaluations run. Unverified models are never recorded as verified. A provider that cannot verify refuses outright.
9. **B12 escalation outcome** (GE5). A field whose split escalation is still unresolved sends the packet to `needs_review`, as B12 states, whatever its tier.
10. **Ruling 5** (GE4, GH7; reverses judgement call #4). Disputed rows, including disputed agreement, report `max(conf_a, conf_b)` in the oracle and the views; accepted agreement keeps the mean. Update the expectations.
11. **Atomic admission** (GE9). `admit_next` uses a guarded `UPDATE … WHERE status='ready' AND version=…` with a rowcount check, as `claim()` does.
12. **Failure budget** (GH6, B4). Exhausting the lifetime failure budget makes an audited transition to `awaiting_confirmation` and creates a confirmation. Tests cover retry exhaustion and approve/reject on both dialects.

**Deferred to M3:** GE7 (validate numeric settings on write).

## M2.1 outcome and PM rulings (2026-11-04)
All 12 fixes are implemented on PR #2 @ 2fcfda1. CI is green on all three jobs (PM-verified through the GitHub API). Tests: 410 passed, 4 dialect-specific skips (up from 350). Migrations 0001–0005 are unchanged since the reviewed commit; all new schema is in 0006. PM spot-checked guarded `admit_next` and ruling 5 in the oracle.

Rulings on the engineer's open notes (PROGRESS.md):
1. **Registry binding is per field**, not on the global registry revision. *Accepted:* binding globally would invalidate every packet whenever any one field is edited, which works against slow, steady progress.
2. **Exemplar changes stop reuse but don't make existing consensus pending.** *Accepted for M2.* From M3, editing a field's exemplars must bump that field's definition version (CLI edits, C37), so consensus becomes pending through rule 1.
3. **`closed_unverified` is terminal and locked, and only a user can set it**, with a reason. *Accepted:* locking keeps history safe (K5); M3 C23 is updated to match.
4. **A context mismatch at claim leaves the return to `evaluating_pre` pending until the caller commits.** *Accepted;* the M3 kernel must commit on that error, never roll back (added to C25).
5. **On SQLite a lost race can surface as a database-lock error rather than `ClaimError`.** *Accepted;* the M3 kernel treats a lock error as a lost claim and retries next tick (added to C25).
6. **The failure budget is checked only for packets waiting for admission or claim.** *Accepted;* the running-packet path arrives with M3 channels (C27).
7. **New transition `queued → awaiting_confirmation`.** *Accepted;* it is included in the C23 tables.
