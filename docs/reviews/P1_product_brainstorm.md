# P1 — Product brainstorm (Gemini Argon, 2026-11-04) with PM triage

**Prompt:** step back from the specs and design for the CFO's real usage: a few minutes between meetings. Four questions: the owner's day, the v1.0.0.0 acceptance scenario, product-level failure modes, and the roadmap after M4.
**Status:** proposals. Items marked **CFO** need the CFO's decision; items marked **PM→M3** fit the approved scope and go into M3 Stage B.

## 1. The owner's day
- **Rhythm.**
  - 5 min before work: phone digest, approve 1–2 parked items, maybe one `interrupt`.
  - Unattended daytime: bounded ticks; anything uncertain parks and the kernel moves on.
  - 10 min at lunch: triage on the phone.
  - Evening: desktop session for deeper work.
- **Mobile-first digest is the product.** The four-column Miller view is unusable on a phone. Make `/digest` a single-column card stack, in this order:
  1. a heartbeat banner (kernel and channel state, a red Pause/Drain button);
  2. **action required**, capped at the 5 most urgent, each card showing why it stopped plus inline context (diff, tool request, both model quotes);
  3. governor interventions since the last visit;
  4. completed and committed work (SHA, files, ±lines);
  5. a budget meter (runs/tokens today) and the queue size.
  
  **New since last visit** comes first.
- **One-tap:** approve/reject, waive a soft field (preset reasons), budget top-up, kill branch / hold, pause/drain.
- **Never interrupts:** individual retries, evaPacket passes, routine transitions, normal budget exhaustion. No push notifications in v1.
- **Trust comes from:**
  - a diff preview before repo_write (repo_write defaults to confirm);
  - hard ceilings (depth 2, fan-out 3, Workbench 4/h);
  - the immutable audit trail.
- **Remote access:** the kernel stays bound to `127.0.0.1`. Use **Tailscale** with `tailscale serve --bg 8765`: private HTTPS to the owner's own devices only, with no auth code in the app.

## 2. The v1.0.0.0 acceptance scenario
- **Challenge to the plan:** with M3 alone the kernel has only FileDrop and `NoRunner` (every execution check blocked). Argon argues this is "a state-machine demo, not a working prototype".
  - Proposal: **M3 = v0.9 (kernel complete); v1.0.0.0 = M4 (Juggler bridge) + a lightweight isolated test runner.**
- **Scenario: `csv-profiler`.** A pure-stdlib Python CLI that profiles messy CSVs: types, nulls, min/max/mean, and flags likely PII (emails, UK phone numbers, NHS numbers). It suits the CFO's data-analyst training and IT work.
  - The messy brief deliberately includes an ambiguous "maybe a GUI dashboard later?".
- **Over 3 days (accelerated in acceptance):**
  - ingest → 3 objectives;
  - the core parser routes to Local Generation;
  - the PII packet splits (with confirmation);
  - the dashboard objective trips ambiguity → `needs_review` → closed_unverified by the owner;
  - a Workbench run parks (exit 3) and resumes after approval;
  - an interrupt creates a new version using `modifies:` with a base hash;
  - post-evaluation and scan, then lock.
- **Pass criteria:**
  - ≥ 4 completed and locked packets; exactly 1 closed_unverified;
  - 0 unaudited writes (every commit maps to an artifact and manifest); 0 duplicate effects across a simulated crash;
  - ≥ 1 parked run resumed;
  - ≤ 6 owner decisions and < 15 minutes total owner time;
  - pytest 100% in the production repo, with clean provenance commits.
- **Dogfooding on PythonPlanner itself:** not before M5. Then only on leaf modules, and in a clone, never on the repo running the kernel.

## 3. Product failure modes (each with a v1 metric and a mitigation)
| # | Failure | Early-warning metric | Mitigation |
|---|---|---|---|
| 1 | **Approval fatigue**: one packet needs 4 sequential confirmations, which means days of latency | confirmations per completed packet (> 2.0); p95 confirmation wait | **Bundled approval**: "approve dispatch + auto-commit if post-eval ≥ 8,000" |
| 2 | **Bureaucratically aligned drift**: hollow code that quotes the brief scores high on alignment | ratio of hollow artifacts (stubs, `pass`, TODO); closed_unverified after post-eval | Static check that rejects stub-only modules unless the packet allows them; require paired tests |
| 3 | **Safe-middle scoring**: small models answer 6–7 for everything, so everything routes to Workbench | distribution of pass scores (> 60% in {6,7}); WP routing ratio (> 75%) | Anchor exemplars at 2 and 9; rubric that discourages the middle |
| 4 | **Quote starvation on terse briefs**: verbatim-quote rule floods `needs_review` | dispute rate caused by zero verified quotes while scores agree | Keep verbatim quotes for extractive fields; relax for *estimative* routing scores |
| 5 | **Stale churn**: an objective-wide governance write stales a healthy sibling run 90% done | stale-run waste ratio (> 10%) | Governance actions default to **packet scope**; wider scope needs an explicit flag |
| 6 | **Digest blindness**: the same blocked cards sit at the top for days | age of the oldest un-actioned card; hours between owner touches | "New since last visit" first; one-tap "hold for 7 days" |

## 4. Roadmap after M4
- **M5, trust and reality:**
  - a real isolated runner (bubblewrap/rootless Podman, no network, read-only repo, timeout);
  - `orchestrator calibrate`, which benchmarks candidate local models against gold exemplars before the CFO picks models A and B;
  - a lightweight secrets scanner before any repo write;
  - governor hysteresis only if the deterministic holds prove too twitchy.
- **M6, "5-minute owner" UX:**
  - a polished mobile digest with inline diffs and bundled approvals;
  - `sync-repo`, a deterministic file and AST map of existing (brownfield) repos so packets know what already exists;
  - an optional twice-daily one-way summary (ntfy or a webhook).
- **M7, hardware and efficiency:**
  - short-circuit self-check passes when A and B agree exactly on non-key fields;
  - Ollama residency/VRAM management;
  - embedding-based drift.
- **Never build:**
  - a chat UI or IDE inside the app (Juggler is the workbench);
  - multi-user auth or cloud hosting;
  - autonomous dependency installation (new dependencies always park for the owner);
  - free-form multi-agent debates for evaluation;
  - a React/SPA frontend.

## Argon's top five
1. A mobile-first `/digest` with one-tap actions, served over Tailscale.
2. v1.0.0.0 acceptance includes the M4 Juggler bridge and a light isolated runner (M3 = v0.9).
3. Bundled approval, plus governance writes defaulting to packet scope.
4. Relax the verbatim-quote rule for estimative routing scores.
5. `calibrate` and `sync-repo` before any governor sophistication.

## PM triage
| Proposal | Triage |
|---|---|
| Mobile-first `/digest` card stack, "new since last visit", capped action list, one-tap actions | **PM→M3 Stage B.** Within the approved GUI scope (C37 keeps the digest, approvals and pause). It replaces the Miller view as the *default* page; the Miller view stays for desktop. |
| Governance actions default to packet scope in the GUI/CLI | **PM→M3 Stage B.** UX default only; C25 semantics are unchanged. |
| Six early-warning metrics as digest footer and `orchestrator stats` | **PM→M3 Stage B** as read-only SQL views (K4, readable). Cheap; no behaviour change. |
| Tailscale for remote access | **CFO** (his machine and network; RUNBOOK documentation only, no code). |
| **v1.0.0.0 bar: M3 = v0.9; v1.0.0.0 = M4 + light isolated runner** | **CFO.** This changes decision D13. PM view: agree. It is a better test of the Key Idea, and the cost is that v1 comes later. |
| `csv-profiler` as the acceptance scenario | **CFO** (is it a project he actually wants?). PM view: good fit. |
| Bundled approval ("approve + auto-commit if post-eval ≥ 8,000") | **CFO** (it loosens a `confirm` default). PM view: sensible, *as an opt-in per approval*, recorded as a version-bound conditional approval. |
| Relax verbatim quotes for estimative fields | **CFO + panel.** It changes the evaluation rule (M2 §7). Needs evidence from real models first. Revisit at M5 `calibrate`. |
| Stub-rejection static check | **PM→M3 Stage B** as a contract option (default on for `python_module`). |
| Exemplars at the extremes (2 and 9) | **PM→M5** with `calibrate` (needs real models). |
| M5–M7 roadmap and the never-build list | **CFO** to endorse. The never-build list is low-risk to adopt now. |

## Second opinion (Microsoft Copilot GPT-6.1 Sol, independent) and revised triage
**GPT-6.1 on the five decisions:**
- **Q-P1: reject the relabelling, adopt an operational acceptance gate.** D13 deliberately approved a *kernel* prototype. M3 already proves crash recovery, governance and immutable history; M4 already commits to real Juggler integration. Keep v1.0.0.0 = M3, then require a separate real-world acceptance gate after M4 before relying on the kernel unattended. *10-second version: "Finish M3 as approved; prove real unattended delivery after M4 before relying on it."*
- **Q-P2: adopt with changes.** Judge working output, not a prescribed packet count, because counts reward artificial decomposition. Keep PII flags explicitly heuristic, with synthetic test data. Keep functional acceptance separate from the injected crash, parking and ambiguity tests.
- **Q-P3: defer.** Approving a dispatch can't approve an unseen diff. "Post-eval ≥ 8,000" is not a defined predicate. This changes authority (K2/K5), not just click count, so it needs a precise, tested contract for bounded conditional permissions first.
- **Q-P4: adopt with changes.** Owner-only access depends on the network access policy. Document an access policy, revocation for a lost phone, and a test that unauthorised access fails. The kernel stays on localhost.
- **Q-P5: adopt with changes.**
  - Order: pull a small real-model calibration spike forward; put isolation, secrets checks and multi-hour soaks before operational acceptance; put minimal repository mapping before brownfield generation; mobile polish after that; residency and embeddings last.
  - Skipping self-check passes changes the six-pass method, so it needs the CFO's approval.
  - Treat the never-build list as current boundaries, not permanent prohibitions.

**On the metrics:** not all are cheap SQL views yet.
- Confirmation wait times need request/resolution timestamps.
- "Hollow output" needs deterministic analysis results, not hashes.
- Quote starvation needs structured rejection reasons.
- Stale waste needs elapsed time/tokens per run.
- "Last visit" needs an owner-interaction event.

**Missing failure mode that matters more: "busy but useless".** Lots of evaluation, retries and branching with no independently verified useful output. Track **accepted deliverables that pass external checks, per owner-minute and per elapsed day**.

**Items the PM wrongly triaged as Stage B:** default-on stub rejection (changes accepted-output behaviour); the packet-scope default (equivalence with C25 coverage not shown); new one-tap authorities (budget top-up, waive, kill); metrics that need new instrumentation.

### Revised PM triage (supersedes the table above where different)
- **Q-P1:** PM now recommends **GPT-6.1's synthesis**: keep D13 (v1.0.0.0 = M3 kernel prototype), and add an **operational acceptance gate** after M4 (the csv-profiler scenario plus a light isolated runner) before unattended reliance. Same substance as Argon's proposal, but no decision is reopened. CFO to choose.
- **Q-P3:** PM now recommends **defer** (an authority change that needs its own contract).
- **Stage B now gets only:**
  - the mobile-first `/digest` layout and "new since last visit";
  - one-tap for **already-specified** actions only: approve/reject confirmation, HOLD/BLOCK and revoke (C35), pause/resume/drain, close-unverified with a reason (C23);
  - budget top-up and soft-field waiver as one-tap, *because C35/C36 already define them as owner actions*, using the same guards as the CLI;
  - metrics only where computable from the Stage A schema, plus an `owner_visit` event that is explicitly **not** a "human touch" under C33;
  - the "busy but useless" metric (accepted deliverables per day and per owner decision).
- **Moved to the CFO:** default-on stub rejection (proposed default: *warn*, not reject, until approved); a packet-scope default for governance actions (proposed: the CLI/GUI asks for scope explicitly, no default).
