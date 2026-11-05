# J-audit: PR #3 code (R4 fixes), Gemini Argon, 5 Nov 2026 (morning sprint)

**Input:** R4 consensus, the engineer's 16-item map, and the source diff ff489f6..c6b5e6a. The pack is on the `review-packs` branch.

**CI on c6b5e6a:** green on all three jobs:
- SQLite, Python 3.11;
- SQLite, Python 3.12;
- PostgreSQL 16.

**Verdict:** CONDITIONAL PASS. Fix J1 and J2, then PR #3 is safe to merge as the M3 Stage A foundation.

| # | Sev | Finding | Decision (PM, sprint autonomy) |
|---|---|---|---|
| J1 | High | Split rejection `awaiting_confirmation → ready` (user) breaks D8 row 61. It also skips readiness and hash re-checks after the owner has waited. | **Follow D8:** rejection → `held`, reason required. The kernel gap-fill `ready → awaiting_confirmation` stays (needed for D8 row 60). The engineer and Argon agree. |
| J2 | Medium | In the `stale_run` else-branch, the ownership-release UPDATE ignores `rowcount`. The in-memory state and the `packet.run_released` event are then written regardless, so a phantom event is possible under READ COMMITTED. | Fix it: mutate and record the event only on rowcount 1, with a test. |
| J3 | Low | `reconcile_runs` handles many runs in no fixed order, which raises the deadlock surface on PostgreSQL. | Fix it (cheap): order by `(packet_id, id)`. |
| J4 | Info | `max(ChannelRun.attempt)` is safe because `guard_bindings` write-locks the packet row first. | No change. |
| J5 | Info | The GA-R1 second CAS can't be reached on its own because `_bump_pair` already guards it. | Keep it as defence in depth. |

**Argon on evidence:** 15 of 16 items are test-proven on both dialects (#13 is code-verified). The fix for the real torn-read bug (75e801e) is "textbook distributed systems engineering".

**PM note:** this confirms G8. The PostgreSQL run found a real concurrency bug that SQLite could not show.
