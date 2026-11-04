# Audit A1 — independent audit of T1, the sprint pack and R4 (22:25)

**Auditor:** Microsoft Copilot **GPT-5.6 Sol Think deeper**, standing in for Gemini Argon (quota exhausted until 23:58). Fresh chat; audit prompt per the CFO's QA structure. PM-verified: A21 (the plan had two conflicting status tables; fixed) and A24 (Juggler sessions are Yjs documents; note corrected). Arithmetic verified: the failure-indicator example is correct (A6).

**Verdict:** "Conditional hold on M3a's field-history, staleness and failure implementation. R4's independent concurrency fixes may continue, but T1 should first receive a short corrective specification addressing A1, A2, A3, A4, A10, A12 and A26." T1 "is genuinely advancing the project… reliability is currently conceptual rather than implementation-grade."

| # | Sev | Finding (condensed) | Proposed correction |
|---|---|---|---|
| A1 | critical | Current values cached on `packets` conflict with triggers that forbid updating locked rows | current state as a separate projection, view or current-pointer table; don't weaken packet immutability |
| A2 | critical | Reopening a completed packet conflicts with Requirement F (locks permanent) | reopening = a new lifecycle episode/incarnation linked to the sealed bundle |
| A3 | high | A single "based-on hash" can't capture all dependencies | a canonical dependency manifest (prompt rev, contract, field-definition version, project/objective rev, dependencies, repo snapshot, preset, policy), hashed |
| A4 | high | Staleness mixes invalidation and drift | **validity** (valid/invalid from exact manifest hashes) separate from **drift_risk** (0–10,000, schedules refreshes only); invalid is never usable |
| A5 | high | Soft-drift inputs aren't comparable units (raw commit counts) | impact features: changed paths, dependency reachability, API/contract changes, affected artifact hashes; the LLM grade is one uncertain feature |
| A6 | high | Arithmetic correct; constants uncalibrated | treat constants as experimental configuration; calibrate on labelled failure histories |
| A7 | high | Possible order dependence with truncation and caps | specify; property-test permutation invariance |
| A8 | high | The pre-attempt time estimate can be gamed and is poorly bootstrapped | store estimate + estimator version + uncertainty + evidence; robust historical quantiles per route/family; conservative fallback |
| A9 | high | Unknown = 0 rewards failing to classify | unknowns raise an *investigation/uncertainty* signal; attribution confidence; append-only reclassification events with recomputation |
| A10 | high | 6-pass attribution isn't proven calibrated | labelled failure corpus, confusion matrices, inter-model correlation, abstention rules, human adjudication thresholds |
| A11 | high | Partial output fields proliferate | an immutable **generation_attempts** table (output/artifact refs, status, provenance, cost, evaluation links); packet has a current-accepted-output pointer |
| A12 | critical | Committing failed output to production is too permissive (code, CI, manifests, migrations, hooks) | default to a quarantine worktree or non-production branch; promotion needs an artifact-class policy and objective checks |
| A13 | high | Chartedness conflates maturity, blast radius and risk | separate blast radius, criticality, maturity, reversibility, validation coverage, runtime exposure; the floor is driven by risk and verifiability |
| A14 | high | SQL dependency blocking misses dynamic or undeclared coupling | repo-level containment (isolated branch/worktree, protected branches, restricted CI, secret scan, explicit promotion) |
| A15 | high | Federation impact gating is circular (children unevaluated) | two-phase: stage → deterministic validation + a bounded evaluation set → compute impact → activate or hold atomically |
| A16 | medium | "Archived" is overloaded | separate immutable parent-definition revisions from derived roll-up; states federation_completed / inactive / superseded |
| A17 | high | 10% prior and "relevance" are arbitrary | explicit causal links; categorical inheritance first (resolved/unresolved/partial/unrelated) |
| A18 | high | Objective escalation lacks an aggregation rule | minimum independent packets, windows, shared-cause confidence, outage suppression, evaluator-defect safeguards |
| A19 | medium | Versioning isn't enough for explainability | store formula id, parameter hash, feature vector, rounding, result; prefer recomputable projections |
| A20 | high | T1 generalises the CFO's eva-field approval to all fields; the superseded federation statement is still in the operative text | mark the generalisation as pending CFO approval; separate the operative theory from history |
| A21 ✓ | medium | The plan has two conflicting status snapshots and stale vocabulary | **fixed** (status table); full consolidation pending |
| A22 | high | Panel agreement ≠ correctness for R4 | map each R4 item to a test + invariant + diff; statuses panel-supported / code-verified / test-proven |
| A23 | medium | R4 CAS needs an explicit isolation model | PG tests at the intended isolation level, deadlock retry, duplicate delivery |
| A24 ✓ | high | Juggler sessions are Yjs documents, not flat transcripts | **note corrected** |
| A25 | high | Juggler's internal API is unstable and untrusted | prefer `juggler run`; versioned adapter + contract tests; pin the release |
| A26 | critical | Recorded thinking is over-weighted and sensitive | optional sensitive telemetry (retention, redaction, access control); decisions from outputs, traces, diffs, tests |
| A27 | medium | No clear event-sourced boundary | immutable events/revisions/attempts → deterministic, rebuildable projections → sealed bundles by hash |
