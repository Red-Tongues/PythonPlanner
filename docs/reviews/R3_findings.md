# Review R3 — pre-implementation panel review of the M3 spec

Subject: `docs/milestones/M3_orchestration_interface.md` (C1–C21) on main @ 7795b16, read against the R2 consensus (M2.1 fixes 1–12).
Panel: **GA** = Gemini Argon · **MA** = Microsoft Copilot GPT-6.1 Sol · **GC** = GitHub Copilot (reads the repo directly). Each reviewer worked independently in a fresh chat. The last column is the reviewer's own confidence (0–10).

## Gemini Argon (GA)
| ID | Sev | Location | Problem | Proposed fix | Conf |
|---|---|---|---|---|---|
| GA1 | critical | C9 | Spec says migration 0005; 0005 already shipped in M2 and 0006 is M2.1. eva_packets, evaluation_cycles and the content_eval ban already exist. | M3 schema goes in frozen 0007; don't recreate M2 tables; upgrade test 0001→0007 on both dialects. | 10 |
| GA2 | critical | C11, C13, C19, D7 | closed_unverified and awaiting_confirmation are missing from the state machine, the GUI and the digest. | Add both. closed_unverified is terminal, unlocked and excluded from the queue, with a distinct badge and a "Close unverified" action that needs a reason. Test. | 10 |
| GA3 | critical | C1, C11, D2 | channel_runs vs generation_runs: unclear which owns the C1 state machine. | One generation_runs row per dispatch (state machine, lease, snapshot incl. gate_revision); append-only channel_runs per attempt. 2-attempt test. | 10 |
| GA4 | critical | C10, C12–C14, C21 | M3 governance actions (GUI/CLI overrides, scope/threshold edits, digest waive/kill/hold, interrupt) don't bump gate_revision; run completion doesn't re-check fingerprint or gate revision. | Normative rule plus barrier test; a stale run sends the packet back to evaluating_pre. | 10 |
| GA5 | high | C16, C21 | Failure-budget counting unclear: attempts vs runs, exit codes 2/3/4, what approval resets. | Attempts count toward max_attempts; exhausted → run failed → failed_runs+1. Exit 3 parks, no count. Exit 2 fails and pauses the channel, no budget count. Approval adds allowance (history kept). | 9 |
| GA6 | high | C8, C21, D7 | C21's float thresholds and GUI-editable weights ignore fixed point and GE7. | Strict numeric validation on write (closes GE7); fixed point ×10⁴ everywhere; boundary parity tests. | 9 |
| GA7 | high | C17, C20, D4 | "Every accepted artifact creates a content-evaluation packet" contradicts the content_eval ban and post-phase evaPackets; order of commit vs post-eval unclear. | Quarantine + validate → authorised write + commit (evaluating_post, unlocked) → post evaPackets on sha256 + Sonar on commit_sha → completion gate; failure → needs_review. | 10 |
| GA8 | high | D2, C15, C16, C20 | Juggler stub (D2; plan says M4) vs live `juggler run` in C15/C16; untestable without the binary. | Configurable juggler_bin tested with a fake CLI fixture (exits 0–4, timeout); FileDrop stays the default. | 9 |
| GA9 | high | D1–D2, C11 | M2.1 #8 fail-closed pinning not extended to the LG channel or model C. | All non-fake calls verified before dispatch; refuse with an event; digest frozen in generation_runs. | 9 |
| GA10 | high | C5, C17, C18, D5 | C18 tells the engineer to "define" the scan skip/failure policy, which forces a judgement call; repo-write branch undefined. | Explicit policy: skipped is recorded and doesn't block unless requires_quality_gate; gate ERROR → needs_review + confirmation. 3-case test. | 10 |
| GA11 | medium | D6, C2, C10 | Interrupt extraction: how extracted nodes match existing records, and what happens to a late in-flight run after supersede, are undefined. | Match by normalised name or ref; supersede changed unlocked items; late runs recorded stale. | 9 |
| GA12 | medium | D3, C3, C4 | ast.parse (D3) contradicts "import in subprocess and tests pass" (C3); front-matter format unspecified; hang risk. | Define the C4 format; staged temp copy, stripped env, 30 s timeout, py_compile + pytest. | 9 |
| GA13 | medium | C21 | "Cycle", timezone, nights/weekends and "today" undefined → nondeterministic tests. | Injected Clock; governor_timezone (default UTC); a cycle is one tick's governor evaluation. | 9 |
| GA14 | medium | D4 | File ownership only allows supersede, so iterative edits by dependent packets are impossible. | Allow supersede, a satisfied dependency edge, or an explicit repo_write confirmation. | 8 |
| GA15 | medium | D2, D7, C19 | Console format for idle/paused/blocked channels and the source of the sequence number undefined. | Integer seq per channel run; defined formats; regex test. | 8 |
| GA16 | low | C1, C6 | Single-kernel lock storage and soak fault injection unspecified. | Lease row via conditional UPDATE; seeded fault injection; determinism assertion. | 9 |

## Microsoft Copilot GPT-6.1 Sol (MA)
| ID | Sev | Location | Problem | Proposed fix | Conf |
|---|---|---|---|---|---|
| MA1 | high | C9 | Migration 0005 conflicts with M2/M2.1. | Start from the 0006 schema; next unused id; preserve history; upgrade test of a populated DB. | 10 |
| MA2 | critical | C12, C14 | Gate revision, context recompute and guarded admission not carried into M3; snapshot can be consistent but obsolete. | Recompute at admission and claim; claim guards gate revision; snapshot and claim commit together; every effective governance change (incl. inherited scope) invalidates. PG barrier test. | 10 |
| MA3 | high | C1, C11, C16, C19 | awaiting_confirmation, closed_unverified, waiting_for_children missing; run parking vs packet parking blurred. | Separate packet and run transition tables; manual close never completes or satisfies a dependency; budget exhaustion makes one confirmation; approval grants a bounded retry allowance. | 10 |
| MA4 | high | C12, C14, C19, C21 | Fractional thresholds vs integer scores; GE7 validation; ruling 5. | Scale 10,000; score×1,000 (alignment ≥7,500 ⇒ score ≥8); validate settings; disputed → max; acceptance checks status too. | 10 |
| MA5 | high | C1, C10, C14, C15 | Pin verification at startup only; config changes and workbench model substitution unaddressed. | Verify the effective config before each non-fake dispatch; a config change invalidates verification; persist it in the content address. | 9 |
| MA6 | critical | C3, C5, C17, C18, C20 | Multi-file outputs have no single identity; merges change bytes; C18 leaves scan policy open. | Canonical manifest of sorted paths + hashes; completion bound to the manifest; scan bound to the exact commit; optional skip evented; required-scan error blocks; failed gate → confirmation. | 10 |
| MA7 | high | C3, C7, C17 | Quarantine is storage, not execution, isolation: importing and testing generated code runs arbitrary code as the kernel user. | Isolated runner with no secrets, DB, network or repo write, and bounded resources; if unavailable, validation is blocked (never passed). Sentinel test. | 10 |
| MA8 | high | C1, C2, C6, C10, C17 | Lease expiry doesn't fence an old worker; pause has a check-to-dispatch race; publish after ownership change. | Monotonic fencing tokens; dispatch serialised with pause; publication checks expected HEAD; durable operation IDs. PG tests. | 9 |
| MA9 | high | C13, C14, C19, C21 | Override scope composition and ties undefined; digest "waive" could read as a governor bypass. | Resolve across packet/branch/objective/project; BLOCK > HOLD > priority; revocation names the override; ties by rank, created_at, ref; PIN exempts only the off-hours pause; WAIVE only for soft fields. | 9 |
| MA10 | high | C13 | Fairness accounting undefined (N, persistence, empty normal queue, races). | Durable per-channel FORCE/PIN counter; reservation after N; atomic selection + counter + admission. PG test. | 9 |
| MA11 | high | C8, C10, C21 | Budgets are counters, not atomic constraints; eval tokens and unknown workbench usage missing. | Atomic reservation before every external attempt; reconcile once by operation id; an unbounded adapter can't run under a hard cap; reserve before committing a split. | 10 |
| MA12 | high | C21 | Signals not mechanically defined; the same observation re-read each tick could count twice; automated events look like "human touch". | Injected clock + IANA tz; half-open windows; count each observation once; human touch = owner decision only; missing evidence = unknown, not clean. | 10 |
| MA13 | high | C8, C18, C21 | The ladder conflicts with hard caps (2-cycle lag) and can deadlock recovery. | Hard caps act immediately; low alignment holds immediately; the ladder applies only to advisory signals; manual holds never auto-clear; safety work continues while paused. | 10 |
| MA14 | high | C2, C10, C12, C18, C20 | A/B batching unbounded; a generation pause may starve post-evaluations. | Finite per-tick batch; reserve capacity for post phases; keep B12's needs_review. | 9 |
| MA15 | high | C5, C6, C15, C16, C20 | FakeProvider can't test exits, parking/resume, partial drops, git recovery or scan identity; C15 pulls M4 work into M3. | Inject runner, adapter, clock and transport; temp git repos; real CLI spike is M4 evidence, not an M3 CI prerequisite. | 10 |

## GitHub Copilot (GC)
| ID | Sev | Location | Problem | Proposed fix | Conf |
|---|---|---|---|---|---|
| GC1 | high | C9 | Migration numbering inconsistent. | M3 starts at 0007; never edit 0001–0006; upgrade tests. | 10 |
| GC2 | blocker | C10, C12 | Guarded claim lacks the gate revision. | Governance writers bump it; claim checks revision + version + hash + readiness + lease; PG barrier tests. | 10 |
| GC3 | blocker | C11, C16 | closed_unverified missing; exit codes vs failure budget. | closed_unverified never completed; exit 3 parks without counting; exhaustion → audited awaiting_confirmation. | 10 |
| GC4 | blocker | C17, C20 | Post binding lacks "complete accepted evaluation per required post field for the exact hash". | Normative wording; a one-byte change invalidates. | 10 |
| GC5 | high | C14, C20 | Snapshots and GUI don't state fixed point or ruling 5. | Same scale and rounding as the oracle; disputed → max. | 9 |
| GC6 | high | C13 | Override semantics for simultaneous, scope, ties, expiry, revocation. | Version-scoped, append-only; highest wins, latest within type; lapse on version change. | 9 |
| GC7 | high | C12 | Fairness reservation not operational. | Overdue packet gets the next slot; consumed only by a successful claim; PG test. | 9 |
| GC8 | high | C21 | Signal windows, stale data, equality, hysteresis undefined. | Versioned signal table; deterministic transitions; flapping tests. | 9 |
| GC9 | high | C21 | Budget checks racy. | Atomic reservation with dispatch intent; last-unit PG race test. | 9 |
| GC10 | medium | C21 (GE7) | Numeric settings unvalidated. | Typed, range-validated on write; existing invalid values fail closed. | 9 |
| GC11 | medium | C5, C15, C16, C21 | FakeProvider alone can't test Sonar, pins or Juggler. | Hermetic fake HTTP server, fake executable, temp inbox/outbox. | 10 |
| GC12 | high | C6, C20 | No explicit PG two-session test matrix. | Required list: claims, governance race, budget race, approval vs parked, late inbox, crash after commit. | 9 |
| GC13 | high | C3, C5, C17 | Artifact hash, commit SHA and scan SHA not tied together. | Persist hash + SHA before post-eval/scan; mismatch → stale. | 9 |
| GC14 | medium | D2, C15 | M3/M4 Juggler boundary unclear. | M3: adapter + fake; real Juggler in M4; FileDrop demo. | 9 |
| GC15 | medium | C21 | Disabled embedding signal could fail open. | Record not_configured; never infer a pass. | 8 |

## Corroboration (independent)
- Migration numbering: GA1, MA1, GC1.
- Gate revision / governance atomicity in M3: GA4, MA2, GC2.
- Missing states (closed_unverified, awaiting_confirmation): GA2, MA3, GC3.
- Exit codes vs failure budget: GA5, MA3, GC3.
- Post-phase exact binding / commit-scan identity: GA7, MA6, GC4, GC13.
- Fixed point, GE7, ruling 5: GA6, MA4, GC5, GC10.
- Pinning at dispatch: GA9, MA5.
- Scan policy: GA10, MA6.
- Override and fairness semantics: MA9, MA10, GC6, GC7.
- Governor definitions: GA13, MA12, GC8.
- Atomic budgets: MA11, GC9.
- Juggler M3/M4 boundary and hermetic fakes: GA8, MA15, GC11, GC14.
- Code-execution isolation: GA12, MA7.

**Unique:** GA3 (run tables), GA11 (interrupt matching), GA14 (file ownership), GA15 (console), GA16 (lock/soak); MA8 (fencing), MA13 (hard caps vs ladder), MA14 (bounded batches); GC12 (PG test matrix), GC15 (embedding fail-open).

## Defer proposals (all three agree on the first two)
1. Real Juggler CLI and worktrees → M4; M3 ships the adapter contract, a fake executable and the FileDrop demo (GA, MA, GC).
2. Embedding-based divergence → later; record not_configured (GA, MA, GC).
3. C21 six-rung ladder → deterministic caps, immediate holds and explicit resume (GA, MA).
4. GUI CRUD and conveniences → read-only views + CLI; keep approvals, pause/resume and readable state (GA, MA, GC).
5. Subprocess execution of generated code → static checks in M3; isolated execution later (GA; MA requires isolation or blocked).
6. Live SonarQube service and per-branch provisioning → adapter + fake server (MA, GC).
