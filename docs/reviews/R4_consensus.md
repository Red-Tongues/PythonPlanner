# Review R4 — consensus on M3 Stage A code (PR #3) and the fix list

**Panel:** MR = Microsoft Copilot GPT-6.1 Sol (findings MR1–MR13) · GR = GitHub Copilot (cross-check; supported all 13, added GR1) · GA = Gemini Argon (cross-check; supported all 14, added GA-R1–R5). PM-verified in code: MR1, MR2, MR3, GR1, GA-R3. **Every finding was supported by all three reviewers. None declined.**

## Fix list (on PR #3, after the build tidy; data-model-independent)
1. **MR1 Atomic binding guard.** Every binding-dependent run write (dispatch, accept, publish, park, approve) does a **packet-row CAS** in the same transaction:
   - `UPDATE packets … WHERE id, version, status, locked=false, active_run_id, fencing_token, gate_revision, input_hash match, and the row is head`;
   - rowcount 1, otherwise StaleRun.
   
   This works on both dialects (Argon's design). Add a PG barrier test at the `before_scopes` seam.
2. **MR2 + MR3 Mandatory fence; approval-bound user dispatch.**
   - System actors must pass a valid `KernelLock` (FencedOut otherwise).
   - A user dispatch is allowed only via `approve_run` with a just-approved, run-bound confirmation, and only while the packet is `running` with no hold or block.
   - No other path can dispatch a parked run.
3. **MR4** Approved resume renews the run and packet leases atomically.
4. **MR5** Confirmed split keeps the approving user as actor (or adds an explicit kernel continuation row bound to the consumed confirmation).
5. **MR6** A retry requires the previous attempt's retry-eligible outcome, or recorded deadline expiry.
6. **MR7** Separate dispatch sequence from chargeable attempts: exit-3 park/resume never consumes the attempt budget.
7. **MR8** Staling a restricted packet's run records a deferred-recovery condition, processed once the restriction clears (E7 still applies). No ownerless `running` packets.
8. **MR9** A stale-binding approval commits an audited stale disposition and retires the unusable confirmation. "Stays pending" applies only to recoverable refusals (pause, budget).
9. **MR10** Reconciliation includes legacy `claimed` runs.
10. **MR11** Budgets: non-negative integer validation plus DB CHECK constraints.
11. **MR12 + MR13** Race tests:
    - propagate unexpected exceptions; assert both workers' allowed outcomes; fail on timeout;
    - integrated dispatch/acceptance tests with an effect-counting fake adapter.
12. **GR1** `cancel_run` clears `active_run_id` only with a guarded UPDATE (`WHERE active_run_id = run.id AND fencing_token = run.fencing_token`).
13. **GA-R1** `reject_run`'s packet CAS also guards `active_run_id` and `fencing_token`.
14. **GA-R2** Failure-budget approval checks that `confirmation.entity_version == packet.version`, and applies the status transition and gate bump atomically.
    *Note: failure-budget behaviour will be replaced by the T1 failure indicator later; fix only the atomicity.*
15. **GA-R3** `packet_scopes`: a trunk packet (`root_id` NULL) includes its own branch scope, so a branch pause blocks the trunk too.
16. **GA-R5** Verify the `budgets.reserve` audit/flush ordering. Record the event before `session.add`, or use `no_autoflush`. Add a test.

**Resolved without code change:** GA-R4 (the run gate trigger allows exactly +1). "Reconsider" is valid only while `run.G == packet.G` and H is current (D10 V-D2). The trigger stays as it is.

**Constraints:**
- Immutability triggers stay on, including in tests.
- Tests run on both dialects.
- One focused commit per fix group.
- No Stage B / M3a feature work: the genPacket field-revision model (`docs/notes/2026-11-04_T1_*`) is still under CFO review.
