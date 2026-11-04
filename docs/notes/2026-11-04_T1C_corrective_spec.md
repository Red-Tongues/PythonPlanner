# T1-C — Corrective specification for T1 (PM draft, 22:40)

**Purpose:** answer audit A1's hold conditions (A1, A2, A3, A4, A10, A12, A26) and fold in its other findings, so T1 can become an M3a spec. Items marked **[CFO]** change or narrow something the CFO decided and need her approval. Nothing here is built until she reviews it.

## 1. Facts versus state (A1, A2, A11, A16, A27)
The design separates **immutable facts** from **derived state**.

**Immutable records (written once):**
- `packets`: identity only (id, ref, project, objective, parent, root, task family, created_at).
- `field_revisions`: packet, field, revision number, value (or a blob reference for large text), **dependency manifest hash** (§2), producer (eva cycle, generation attempt, user, interrupt), time.
- `eva_packets` / `evaluation_passes`: unchanged.
- **`generation_attempts`** (new, A11): packet, attempt number, route (local draft / Juggler), request manifest hash, started/ended, consumed time and tokens, outcome class, output reference, partial flag, and a reference to the recorded thinking (sensitive, §5).
- **`lifecycle_episodes`** (new, A2): packet, episode number, opening event, closing event, status at close.
- **`sealed_bundles`**: one per completed episode, holding the prompt revision, pre-evaluation cycle, accepted attempt, post-evaluation cycle and bundle hash.
- `events`.

**Derived state (rebuildable projections, never hand-edited):**
- current field values (the latest *valid* revision per field);
- the current episode's status;
- queue fields (priority, position, indicators).

These are SQL views, or materialised tables recomputed only by the kernel. Sam's "read it straight from `packets`" convenience becomes a **`v_packet_current` view** with the same columns.

**What "immutable" means (A2) [CFO]:** every revision, attempt, closed episode and sealed bundle is immutable. *Re-opening* a completed packet opens a **new episode**; the completed episode and its sealed bundle never change. *This is how we propose to satisfy "completed work is immutable" while letting fields be amended.*

**Parent states (A16):** a federating parent's definition revisions are immutable. Its roll-up is derived. Its episode closes as `federation_completed`; later re-federation opens a new episode. "Archived" is just the projection "no open episode, not in queue".

## 2. Validity versus drift (A3, A4, A5)
- **Dependency manifest:** each field revision stores a canonical, sorted-key manifest of exactly what it was computed from:
  - the upstream revision ids;
  - field-definition, preset and contract versions;
  - project and objective revisions;
  - sealed-bundle hashes of the dependencies;
  - the repo commit, *only for fields declared repo-sensitive*.
  
  `manifest_hash = sha256(canonical manifest)`.
- **Validity is binary:** a revision is **valid** iff recomputing its manifest from today's heads gives the same hash. An invalid value is **never usable**, at any score.
- **`drift_risk` (0–10,000) is separate.** It only **schedules precautionary refreshes**. At or above a ceiling it sets the derived gate `refresh_due`, which blocks readiness until the field is refreshed. `refresh_due` is distinct from invalid.
- **Drift inputs are impact features, not raw counts (A5):**
  - changed paths that intersect the packet's declared paths;
  - dependency-graph reachability;
  - contract/API changes;
  - affected artifact hashes.
  
  The LLM's context-sensitivity grade is **one uncertain feature**.

## 3. Failure maths: evidence, uncertainty, calibration (A6–A10, A17–A19)
- **Attempts are facts; classifications are revisions.** Each attempt's failure category and attribution is a field revision with an **attribution confidence**. Reclassification appends, and the indicators are recomputed from the facts.
- **Unknowns (A9):** they don't add task blame, but they raise an **investigation signal**, which can restrict dispatch of that packet until it's resolved.
- **Six-pass attribution is a proposal, not verified truth (A10).** It may abstain. Low confidence → unknown.
- **Shadow mode until calibrated (A6, A8, A10):** all constants (base, time weight and cap, amplification, prior, thresholds) are **versioned configuration**. Until a labelled failure corpus exists (confusion matrix per class; inter-model correlation), indicators are **computed and shown but advisory**. Statuses derived from them don't block or reroute.
  **[CFO] Do you accept a shadow period?**
- **Expected time (A8):** stored before dispatch with estimator version, uncertainty and evidence. Robust historical quantiles per route and family; a conservative fallback for new families.
- **Inheritance (A17):** categorical first. An approval links each failure cause as *resolved / unresolved / partially resolved / unrelated*. Evaluators propose; the CFO confirms. Numeric relevance weights come later, with evidence.
- **Objective escalation (A18):** needs ≥ N independent packets within a window, shared-cause confidence above a threshold, and correlated outages suppressed. An evaluator defect is never counted as an objective problem.
- **Order (A7):** the update is `(1+I)(1+c) − 1` before capping, which is commutative. Property-test permutation invariance with integer truncation and caps.
- **Explainability (A19):** each computed score stores formula id, parameter-set hash, feature vector, rounding and result.

## 4. Repo inclusion with containment (A12, A13, A14) [CFO]
- **The CFO's principle stands:** in uncharted space, failed or partial work may be kept *as long as it's tagged*.
- **Narrowing:** "kept in the repo" means a **provisional branch / quarantine worktree** (e.g. `orchestrator/provisional`), **not** the branch the CFO merges from. Promotion to `orchestrator` needs the risk and verifiability floor.
- **Never provisional in v1, at any chartedness:** CI files, build/dependency manifests, migrations, hooks, secrets/config, executable entry points.
- **Chartedness splits into separate factors (A13):** blast radius, criticality (role), maturity, reversibility, validation coverage, runtime exposure. The floor is driven mainly by **risk and verifiability**.
- **Containment beyond SQL (A14):** protected branches, restricted CI permissions on provisional branches, a secret scan before any commit, explicit promotion events.

## 5. Recorded thinking (A26)
- Thinking is **optional, sensitive telemetry**: retention limit, redaction, access control, never shown outside the owner's own surfaces.
- It may be **supporting evidence** for attribution, **never sole evidence**. Acceptance and attribution rest on outputs, tool traces, diffs and test results.

## 6. Federation activation (A15)
Two phases, atomic at the end:
1. Stage the proposed children (not yet active).
2. Deterministic validation (structure, caps, depth, duplicates, path conflicts) plus a **bounded** evaluation set.
3. Compute impact.
4. Activate, or hold the branch, or pause the loop, in one transaction.

## 7. Review evidence (A22, A23)
- Every R4 item carries three statuses: **panel-supported / code-verified / test-proven**, and maps to an invariant and a test.
- PostgreSQL tests run at the intended isolation level, including deadlock retry and duplicate delivery.

## Questions for the CFO
1. Does "completed episodes and sealed bundles are immutable; re-opening starts a new episode" satisfy your immutability rule? (§1)
2. Shadow mode: the failure/drift indicators are advisory until calibrated on real failures. (§3)
3. Repo inclusion: provisional work goes to a provisional branch you don't merge from, and some file kinds are never provisional. Does that keep your intent? (§4)
4. The audit noted that you approved the "based-on" rule for *eva_packet fields*. May it extend to prompts, drafts, outputs and thinking via the dependency manifest? (§2; A20)
