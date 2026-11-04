# Review R3 — consensus on the M3 spec (pre-implementation)

**Subject:** `docs/milestones/M3_orchestration_interface.md` @ 7795b16 · **Date:** 2026-11-04 · findings in `R3_findings.md`.
**Panel:** GA = Gemini Argon · MA = Microsoft Copilot GPT-6.1 Sol · GC = GitHub Copilot. Fresh chats; independent findings; every reviewer then scored all the *other* reviewers' findings. **Method:** as R0–R2 — `confidence = (0.6 × mean cross + 0.4 × self)/10`; ACCEPT ≥ 0.70 with no contradiction; ACCEPT-AMENDED ≥ 0.70 with a contradiction; DEFER/PARTIAL 0.50–0.70; DECLINE < 0.50.

**Calibration note:** Gemini supported all 30 findings it scored (scores 8–10). Copilot GPT-6.1 was the discriminating reviewer (6 contradictions), and GitHub Copilot raised 1. So the contradictions carry the signal this round, and the PM checked each one.

## Scores
| Verdict | Findings (confidence) |
|---|---|
| **ACCEPT (40)** | GA1 0.97 · GA2 0.97 · GA3 0.91 · GA4 0.97 · GA5 0.90 · GA6 0.90 · GA8 0.84 · GA10 0.94 · GA11 0.81 · GA13 0.84 · MA1 1.00 · MA2 1.00 · MA3 1.00 · MA4 0.94 · MA5 0.87 · MA6 0.97 · MA7 0.91 · MA8 0.87 · MA9 0.90 · MA10 0.87 · MA11 0.97 · MA12 0.97 · MA13 0.97 · MA14 0.84 · MA15 0.97 · GC1 0.97 · GC2 1.00 · GC3 1.00 · GC4 0.97 · GC5 0.87 · GC6 0.87 · GC7 0.87 · GC8 0.90 · GC9 0.93 · GC10 0.93 · GC11 0.94 · GC12 0.93 · GC14 0.90 · GC15 0.77 |
| **ACCEPT-AMENDED (3)** | GA7 0.82: MA says C17 already supersedes the content-eval line. *Amend:* delete the stale wording; keep GA7's step order. · GA12 0.75: MA says a stripped env plus a timeout is not isolation. *Amend:* adopt MA7's isolated-runner rule. · GC13 0.75: MA says provenance is already required. *Amend:* fold into MA6's multi-file manifest. |
| **PARTIAL (3)** | GA9 0.69: pin and verify every *evaluative* call. Generative calls record their model identity and are flagged unverified when needed, but are not blocked. · GA14 0.50: no dependency-edge overwrite authority. Use a narrow, base-hash-bound `modifies` declaration under repo_write approval instead (MA's suggested fix). · GA16 0.69: specify the observable exclusion and recovery tests; the lock storage mechanism is the engineer's choice (must be portable). |
| **DEFER (1)** | GA15 0.59: console wording for idle/paused/blocked → backlog (cosmetic). |
| **DECLINE** | none |

PM verified the cited contradictions in the spec text before adopting: C9 names migration 0005 (GA1); Deliverable 4 still says "content-evaluation packet" (GA7); Deliverable 2 stubs Juggler while C15 calls the live CLI (GA8); C18 asks the engineer to "define" the scan policy (GA10); Deliverable 3 `ast.parse` vs C3 "import in a subprocess" (GA12).

## Defer proposals
| # | Proposal | GA | MA | GC | Outcome |
|---|---|---|---|---|---|
| 1 | Real Juggler CLI + worktrees → M4; M3 ships adapter contract + fake executable + FileDrop | ✓ | ✓ | ✓ | **Adopted** |
| 2 | Embedding divergence → later; report `not_configured` | ✓ | ✓ | ✓ | **Adopted** |
| 3 | Six-rung ladder with hysteresis → deterministic caps, immediate holds, explicit resume | ✓ | ✓ | ✓ | **Adopted** (full ladder → M5) |
| 4 | GUI CRUD → read-only GUI + CLI edits | ✓ | ✓ | ✓ | **Adopted** |
| 5 | Executing generated code → static checks only in M3 | ✓ | ✓ (blocked, never passed) | ✗ (isolate or block) | **Adopted as amended:** the runner interface ships in M3 with a no-runner default whose execution checks report *blocked*, never passed |
| 6 | Live SonarQube provisioning → adapter + fake server | ✓ | ✓ | ✓ | **Adopted** |

## Outcome
All accepted findings are written into the M3 spec as **R3 amendments C22–C38**. They take precedence over all earlier M3 text, and stale lines are marked in place. Expected effect: far fewer engineer judgement calls in M3 (M2 had 14) and a smaller, safer v1.0.0.0 scope.
