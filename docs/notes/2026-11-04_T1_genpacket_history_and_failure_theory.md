# T1 — genPacket history and failure theory (working draft for CFO review)

**Method:** short, one-topic conversations (as in the CFO's Argon session). Topic A with **Gemini Argon**; topic B with **GPT-6.1 Sol**. Both worked from the authoritative note `2026-11-04_CFO_Argon_clarification.md`. **Status: proposals; nothing is built from this until the CFO reviews it.**

## A. Amendable fields without losing history (Argon)
**Three kinds of field**
| Kind | Examples | Rule |
|---|---|---|
| Never change once set | identity and lineage (id, ref, project, objective, parent, root, created_at); raw records of what happened (evaluation passes, run attempts, events, frozen run snapshots) | written once, never updated |
| Amendable, with history kept | tags and scores, `locally_drafted_examples`, `generation_output` + recorded thinking, post-generation evaluations, even `prompt_description` after a course change | each new value is a **new revision**; the old ones stay |
| Recalculated all the time | priority, importance, queue position, failure indicators | live numbers computed from the history; not "content" |

**What "immutable" means here**
- Every revision is permanent the moment it's written. "Amending" = appending a newer revision and moving the packet's *current* pointer.
- **"Completed" = a locked bundle:** prompt revision + pre-evaluation cycle + output revision + post-evaluation cycle, sealed together.
- The packet can later **re-open** (for example after a course change). The sealed bundle stays in its history as a frozen milestone. *(This replaces cloning whole packets via `supersedes_id`.)*

**Storage that stays readable for the CFO**
- **`packets`** shows the **current** values right on the row (current prompt, tags/scores, drafts, output), so most inspection needs no joins.
- **`eva_packets` / `evaluation_passes`** already hold the history of every tag and score.
- **One `packet_field_revisions` ledger** holds the heavier text fields (prompt, drafts, output, recorded thinking): packet, field, revision number, content, *based-on hash*, run, time.
- Not one table per field (dozens of tables), and not one dumping-ground table for everything.

**Knock-on staleness: decided by SQL, never by an LLM**
Fields sit in four stages:
1. definition (prompt, response structure);
2. pre-evaluation and drafts;
3. production output + thinking;
4. post-evaluation.

Each revision records the hash of the upstream stage it was based on. **A revision is current only if its based-on hash matches the current upstream.**
- A new prompt (stage 1) → stages 2–4 show as stale; their history is kept.
- A new output (stage 3) → only post-evaluation (stage 4) is stale; the pre-generation tags stay valid.

**Federating parents**
- The parent's "output" is a **child-task list**, generated through Juggler (the same mechanism as enumeration). The queue holds it back until there is enough context.
- When the list passes post-evaluation, the children are created (`parent_id` → parent) and the parent becomes **archived**. Archived means: out of the queue, spawning output locked, but still in the tree to roll up the children's progress. After a course change it can append a new federation revision.
- **Federation does not pause the loop.** It is an expected, local expansion. Only the human prompt window pauses, because it can contradict running work. With the branching scope set to confirm, only that branch waits.

**Main risk named:** "Franken-packets", where evaluations get mixed with the wrong output revision. The based-on hash rule exists to prevent this.

## B. What counts as a failure (GPT-6.1)
**Kinds of failure**
- no usable result (nothing, refusal, truncated);
- unusable structure;
- poor substance;
- placeholder work (always flagged, C39);
- wrong target (fine but off-task);
- execution interruption (timeout, crash);
- external blockage (Juggler/tool/service/resources);
- unclear job (contradictory, missing context, impossible, premature).

**Attribution**
- **Counts towards "job needs review":** rejected substance, off-task work, missing required implementation, evidence the job is unclear, impossible or too big.
- **Doesn't count:** outages, broken tools, host crashes, accidental cancellation, context lost by the orchestrator.
- **Investigate first:** empty output, refusal, timeout, resource exhaustion. These say what happened, not why.
- **Unknown stays unknown**, and counts as zero until attributed.

**Counters**
- `failed_generations` = plain count of every unsuccessful attempt: honest history.
- A separate **job-review indicator** counts only task-attributable failures.

**Exponential weighting (starting point)**
- Each task-attributable failure adds to the indicator, and its weight **halves over a set period**.
- Duration doesn't add failure weight. Wasted time is tracked separately for the queue maths.
- A failure attributed later counts from its *original* time.
- Several linked failures implicate the **objective** only when they point to a shared task problem, not a shared outage.

**Who attributes?**
1. Cheap deterministic checks gather the facts: anything returned, truncation, structure, required files, tests, placeholder flags, tool/service errors.
2. The **6-pass eva_packet mechanism** then evaluates the classification fields: category and attribution, with evidence.
3. SQL applies the consensus rules.

Recorded thinking is supporting evidence, not proof. An evaluator malfunction is not a generation failure.

## Open questions for the CFO
1. **Decay:** should old failures fade just because time passes (even while the project is paused), or only when newer attempts provide fresh evidence? *(GPT-6.1: time-only decay lets an unresolved job look healthier just by waiting.)*
2. Do the **four stages and the based-on-hash rule** match your mental model of how fields unlock?
3. Is "**completed = sealed bundle; the packet may re-open later**" acceptable as the meaning of your immutability rule?
4. Federation **doesn't pause** the loop; only your prompt window does. Agree?

## CFO answers (21:26)
1. **Decay:** time-based fading, if it appears in the queue maths at all, must be **very discreet**. The time effect that matters is **production time used vs failure scoring**: how much generation time a failed attempt consumed. *(This corrects GPT-6.1's starting point, which gave duration no failure weight.)*
2. **Stages + based-on rule:** yes, for eva_packet fields.
3. **Stale values: right principle, needs a better definition.** LLMs inevitably contribute through their evaluations, but **statuses themselves must be mathematically derived**.
4. **Federation and pausing:** *corrected by the CFO directly to Argon.* The PM had overstated her answer. Her intuition: newly federated children aren't eligible for processing yet (they start un-evaluated), so a pause is **usually unnecessary**, **but it depends on the impact or extent of the federation**. Argon's proposal (below) gates the pause on impact.

**Next:** develop the failure *mechanics* and how they integrate with the queue maths, watching for new ideas along the way. "Failures are really useful for project development."

## Round 2 (21:30–21:40)

### Federation pause gated by impact (Argon, after the CFO's correction)
- Children enter un-evaluated, so there is a natural buffer before any production work.
- **Below threshold** (a routine breakdown into a few expected subtasks) → no pause.
- **Medium impact** (child count or scope beyond the branch threshold) → **branch hold**; unrelated objectives keep running.
- **High impact** (genesis-scale federation, or restructuring across objectives) → **full loop pause**, like a prompt-window re-baseline.
- The thresholds are mathematical: child count, scope caps, cross-objective dependencies, post-evaluation alignment and ambiguity of the child list.

### Failure mechanics (GPT-6.1, after the CFO's answers)
**Production time, normalised.**
- Compare time consumed with an **expected time fixed before the attempt** (from task-size tags, generation route and comparable work).
- A failure that used 2× its expected time weighs more than one that used ¼, and big jobs get a bigger allowance.
- Every task-attributable failure keeps a **base contribution**, so repeated quick failures still count. The time component is **bounded**, so one extreme attempt can't dominate.
- Any exponential amplification applies to *accumulated normalised evidence*.
- Calendar-time fading is negligible or absent.

**Two separate measures.** *Production cost* (time and tokens consumed, recorded for every attempt, including external and unknown failures) is kept apart from *evidence the job needs intervention* (task-attributable failures only).

**Failures as signal**, not just penalty. Each one feeds a different queue-maths input:
- **missing context** → raise the value of gathering context or finishing prerequisites;
- **excessive scope** → raise the value of decomposition by a federating parent;
- **promising but incomplete output** → raise the value of drafts or refinement (e.g. local-model `locally_drafted_examples`);
- **repeated capability mismatch** → favour a different model or tool route;
- **shared cause across linked packets** → raise objective-level clarification or dependency resolution;
- **external blockage** → make that route less attractive, without blaming the task.

Evaluators supply the evidence and classifications; defined equations derive priorities and statuses.

**Open question from GPT-6.1:** when an attempt fails overall but produces useful partial work, may that material be kept separately (as a draft or evidence) while `generation_output` stays NULL? *PM's provisional view: yes, as a revision of a separate field (e.g. a partial-output or evidence revision), never as `generation_output`.*

## Round 3 (21:40–21:50)

### Staleness, defined (Argon)
**What makes a value stale**
| Change | Effect |
|---|---|
| The packet's direct upstream stage hash changes (new prompt revision, new output revision), or the **field definition / preset version** changes | **Hard invalidation**: staleness = maximum, status = stale |
| The project or objective brief advances; dependency or sibling packets complete; the production repo moves on by N commits | **Soft drift**: a gradual score |
| Swapping or upgrading the local evaluator models | **Never stale by itself.** Model digests are kept for provenance and cache reuse, not to wipe valid evaluations |

**Score and status.** Staleness is a **score** (to be expressed in fixed point, 0–10,000) with a hard threshold that flips the **status**. The soft-drift score combines three things:
- **Context change:** project/objective version bumps since the value was evaluated.
- **Repo change:** commits or completed dependencies since then, × the packet's *context sensitivity* (= 1 − context_independence/10). An isolated utility barely notices the repo moving; an integration task goes stale fast.
- **Confidence margin:** a borderline consensus (just above the acceptance threshold) decays faster than a solid one.

**How the queue uses it**
- Rising staleness (e.g. 2,000–7,000) → the queue raises the priority of refreshing those fields in the background, using spare budget.
- At or above the ceiling (e.g. 7,500) → SQL flips the status to stale and blocks readiness until the field is refreshed.

**Where the LLM stops.** The LLM only *grades sensitivity* during normal 6-pass evaluation (context_independence, repo_consistency, dependency_validity, requirement_alignment, plus consensus confidence). **SQL counts how much the surroundings have moved and computes the staleness number and status.** The LLM never decides "stale".

### Failure indicator, worked (GPT-6.1; constants illustrative)
All values are integers, with 10,000 = 1.0. For each **task-attributable** failure:
1. `time_ratio = min(20,000, consumed_s × 10,000 // expected_s)`. The expected time is fixed before the attempt; a missing estimate needs an explicit fallback.
2. `contribution = 1,000 + 2,000 × time_ratio // 10,000`. At least 1,000, at most 5,000.
3. `amplification = indicator × contribution // 10,000`.
4. `indicator = min(10,000, indicator + contribution + amplification)`. This is multiplicative, exponential-style growth, with no calendar decay.

*Worked example* (expected time 60 s; PM verified the arithmetic):
- fail after 15 s → ratio 2,500 → contribution 1,500 → indicator **1,500**;
- fail after 120 s → ratio 20,000 → contribution 5,000; amplification 750 → indicator **7,250**.

Unknown or external failures don't move the indicator, but they still count in `failed_generations`. The evidence is kept after saturation so that later attribution can be recomputed.

**Signals → queue inputs** (each signal 0–10,000; `boost = weight × signal // 10,000`):
| Signal | Moves | Direction |
|---|---|---|
| job-review indicator | review-work priority | up |
| missing context | context/prerequisite-work priority; the original task's readiness | up; down |
| excessive scope | federating-parent decomposition priority | up |
| useful but incomplete work | draft/refinement value, incl. local-model routes | up |
| capability mismatch | this route's suitability; alternative routes | down; up |
| shared task-related cause | objective clarification/dependency resolution | up |
| external blockage | route availability | down (no job-review evidence) |

**Key idea:** a failure need not push the task down. It can **raise the priority of the work that would unblock it**.

### Open questions for the CFO (round 3)
1. **Model upgrades:** should swapping or upgrading the local evaluator models leave existing evaluations valid (Argon's proposal), or force re-evaluation?
2. **Fresh start after a fix** (GPT-6.1): after an approved clarification or split, does the job keep its review indicator, or start a **new evidence window**, keeping the old history but not carrying the score forward?
3. **Partial work:** keep useful partial output as a separate field revision while `generation_output` stays NULL (PM's provisional view)?

## CFO answers to round 3 (21:54)
1. **Model upgrades do not make evaluations stale by themselves:** agreed (Argon's proposal stands).
2. **After an approved clarification or split:** the principle is to **retain a linked history**. A new evidence window may start, but it stays linked to the old one; nothing is discarded.
   - *Backlog (later, once fields and database structure are better defined):* define what is **objectively enforceable** versus not, so it can weight the **"user involvement" slider**. Much of this lives in **objective categories within task families**, e.g. the family "tasks that create more tasks", broken down further.
3. **Partial work:** yes, keep it.
   - **New concept, "repo inclusion thresholds":** whether output (including failed or partial work) may enter the repo depends on where it's writing. **In uncharted repo space it's fine to include failures, as long as they are commented/tagged as such.** *(Fits C39: placeholders and failures are always flagged, never passed off as clean.)*

## Round 4 (21:55–22:05)

### Repo inclusion thresholds (Argon)
**Chartedness of a target path** (0–10,000; deterministic, from SQL and git):
- whether the path or its module already exists, and how many accepted artifacts have written there;
- **inbound dependencies** (packets depending on it; repo references to it): its blast radius;
- completed-packet coverage under the same objective;
- a role weight from `repository_roles` tags (docs, reference or isolated module = lower; build, deployment or shared core = higher).

*It is the mirror of context sensitivity.* Context sensitivity is how much the surroundings affect this task; chartedness is how much this task's output would affect the surroundings.

**Inclusion rule:** `artifact_quality ≥ required_floor(chartedness)`
- **Hard floor at any chartedness:** safety and security/data-integrity bounds. Unsafe or secret-leaking code is never committed.
- Artifact quality comes from the post-generation evaluations (completeness, correctness, alignment, repo consistency) plus the C39 placeholder/failure flags.
- **Uncharted** (e.g. below 2,500): the floor drops. Partial, placeholder or failed output may be committed as **provisional**, with a machine-readable banner (e.g. `# @ORCHESTRATOR:PROVISIONAL [packet=… rev=… status=partial]`), recorded as provisional on the artifact row.
- **Charted:** the floor rises steeply. Established files with downstream dependents need a clean, fully accepted evaluation; partial output stays in quarantine.

**Stopping provisional code becoming a foundation**
- A dependent packet (or one targeting the same path) **can't become ready while the upstream artifact is provisional**, unless its explicit purpose is corrective.
- As chartedness rises past the provisional ceiling, SQL boosts the packet's priority, or spawns a corrective revision, so the provisional file is replaced before dependants unlock.
- No objective can **roll up to completed** while a provisional file remains on its paths. Provisional files are tracked in SQL (path, sha256) and in git (the banner plus the C39 scanner).

### Linked history in the failure maths (GPT-6.1)
- **New window, separate evidence:** the new window's accumulator `A` starts at 0. The old window contributes a small capped **prior**:
  - **clarification:** `prior = min(1,000, old_A × 1,000 // 10,000)`, i.e. 10% of the old evidence, capped at 1,000;
  - **split:** each child gets `prior × relevance // 10,000`, so it inherits only the evidence relevant to its scope. Not an equal division: an unresolved requirement may affect every child, while "too big" may be solved by the split itself.
- `indicator = min(10,000, A + prior)`. The prior is never amplified or re-imported from ancestors. All links are kept for inspection, and repeated clarifications without new failures can't manufacture evidence.
- **By task family, later:** expected production time, time weight and cap, and the six signal weights vary by family (decomposition matters most for task-creating families). Base contribution, amplification, prior cap and status thresholds stay global until evidence says otherwise. **Version the parameter set** used by each attempt so historical scores stay explainable.

### Open questions for the CFO (round 4)
1. **Provisional commits:** does Argon's curve match your intent? Failed or partial work may be committed in uncharted space with a banner, but blocks dependants and objective completion until it's replaced.
2. **Approvals record causes** (GPT-6.1): when you approve a clarification or split, should the approval record *which failure causes it resolves*? It makes the inheritance maths much more precise, but adds a step for you. Possibly evaluators propose it and you just confirm.
