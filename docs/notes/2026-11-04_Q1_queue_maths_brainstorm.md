# Q1 — Queue maths (v2, aligned to the CFO directive of 2026-11-05)

**Status:** **Q1 v2.** The model below is set by the CFO's directive (`docs/notes/2026-11-05_CFO_T1C_v2_Directive_and_Review.md`, decisions Q2, Q3, Q4 and Q7). Steps 1–3 further down are the team's earlier brainstorm, kept as history. Where they conflict with Q1 v2, **Q1 v2 wins**; each conflict is marked there.
**Gaps:** where the directive does not fix a number or definition, this note does not invent one. It points to an open question (`OQn`) in `docs/milestones/M3a_genpacket_loop.md`.

## Q1 v2 — the model

### 1. Two queues, one a subset of the other
- **genQueue** holds every active, unsealed genPacket.
  - It excludes locked rows and `completed`, `archived`, `closed_unverified` and `waiting_for_children`.
  - View: `v_gen_queue`, replacing `v_queue`.
- **evaQueue ⊂ genQueue** holds the genPackets that still need evaPacket work.
  - View: `v_eva_queue`.
  - A genPacket is in evaQueue when it has:
    - missing mandatory fields;
    - missing optional/secondary fields;
    - missing `locally_drafted_examples`; or
    - `question_context_count > 0`.
- There is no separate failure queue and no rerun queue.
  - A graded genPacket is sealed. Its re-imagined child is a new member of genQueue (Q3(ii)).

### 2. Positions and the 0–2 lockout (Q7)
- Every genQueue member has a `gen_position`. Position 0 is a generation in progress. Positions 1, 2, … are the next genPackets in dispatch order (`queued` before `ready` before the rest; then by relational score).
  - Which statuses count as "running" at position 0 is **OQ1**.
  - Whether non-dispatchable statuses (`held`, `blocked`, `needs_review`, `awaiting_confirmation`, `failed`) take positions is **OQ2**.
- **Lockout:** evaPackets are excluded from evaQueue, and cannot run, when they belong to, or are directly related to, a genPacket at positions 0, 1 or 2.
  - "Directly related" is **OQ3**. The directive's sketch uses dependency edges in both directions plus the parent link.
- Calculations never cancel, stale or discard a running generation (Q7). That generalises step 3's "never preempt running work".

### 3. Continuous enrichment of `Ready` / `Queued` (Q3(iii))
- `Ready` and `Queued` are **not** immune from evaQueue.
- Outside the lockout, evaPackets keep filling:
  - optional/secondary fields;
  - `locally_drafted_examples`;
  - `question_context` refreshes.
- Today's `PRE_STATUSES_TO_EVALUATE` excludes `queued`. That exclusion is retired.
- Whether enrichment can ever demote a `Ready`/`Queued` genPacket is **OQ12**.

### 4. Two gates: `Ready` = fields, `Queued` = relational maths
- **Ready gate (eligibility):** every **universally mandatory** eva field is filled for every genPacket. So is every **relationally mandatory** field.
  - Relationally mandatory fields are triggered by the packet's relationships: dependencies, dependents, chartedness, task family (Q3(iv)).
  - Field names: **OQ10**. How this gate relates to today's readiness predicate: **OQ11**.
- **Queued gate (preference):** `Ready → Queued` depends **purely on the relational mathematical context**: `relational_score_e4 ≥ relational_admission_floor_e4`.
  - A well-graded, non-relational generation's child starts at the bottom and may never be `Queued`.
  - A poorly-graded, pivotal one's child goes to the top and is reprocessed almost at once.
  - The directive's sketch floor is 3500: **OQ6**.
- The two gates keep step 3's principle: **separate eligibility from preference**. A high relational score never buys its way past a missing mandatory field or a hard gate (BLOCK, HOLD, pause, budget).

### 5. Inputs to the relational score
The directive's sketch combines these (units and bounds: **OQ4**, **OQ7**):

| Input | Meaning | Status |
|---|---|---|
| `parent_grade_e4` | the sealed parent's `gen_eva` grade; a poor grade raises the child | sketch uses 0 for roots (see **OQ6**) |
| `relational_centrality_e4` | how pivotal the packet is: dependents, dependencies, objective position | definition missing: **OQ8** |
| `question_context_count` | changed context behind its evaluations | direction of its effect: **OQ5** |
| `user_feedback_bonus_e4` | the CFO's feedback as a weight: confirmations, waivers, manual SQL edits, overrides, grade adjustments (Q3(i)) | definition missing: **OQ9** |

Sketch (non-production):
```
relational_score_e4 = ((10000 − parent_grade_e4) · (3000 + 7000 · centrality_e4 / 10000)) / 10000
                      + 1000 · question_context_count + user_feedback_bonus_e4
```

### 6. Crashes are counted separately from `gen_eva` grades (Q4)
- **Crash:** no gradable output (`generation_output IS NULL`) because of a timeout, disconnect or adapter fault.
  - It increments `crash_count` and nudges **route health**.
  - The genPacket stays unsealed.
  - It is **never** an input to the grade or the parent-grade term.
- **Graded generation:** `gen_eva` runs immediately, with no queue maths. It seals the parent and spawns the child.
  - Its grade enters the child's score through `parent_grade_e4`.
- Where a crashed genPacket goes next, and what bounds repeated crashes, is **OQ18**. `enforce_failure_budgets` and the K = 3 breaker are retired.

### 7. What happens to steps 1–3 below
| Earlier idea | Q1 v2 |
|---|---|
| Step 1 hybrid: hard gates → candidate actions → ranking → capacity allocation → explanation | **Kept as the frame.** The hard gates are now the Ready gate plus governance; the ranking is the relational score; every choice is still explained |
| Step 1 "failure history (review indicator, attempt count, linked windows and prior)" | **Replaced** by lineage history (`parent_id`/`root_id`, Q5) and the separate crash counter (Q4) |
| Step 2 benefit from graph topology; objective health | **Candidate definitions** for `relational_centrality_e4` (**OQ8**) |
| Step 2 human-attention throttle (approval backlog) | Not in the directive; parked |
| Step 3 integer policy `S = (V·(D − R)) // (D + C)` | **Not adopted for M3a.** The directive's `relational_score_e4` governs `Ready → Queued`. Whether S survives is **OQ7** |
| Step 3 "unknown-cause failures may raise U" | Superseded by Q4: crashes go to route health, never to the task's grade |
| Step 3 oscillation (epoch freeze, incumbency margin) and starvation (every m-th dispatch to the oldest) | Compatible with "never preempt running work", but possibly in tension with "purely relational" admission: **OQ25** |

---

# History — team brainstorm before the CFO's directive


## Step 1 (GPT-6.1) — *history; see Q1 v2 §7*
**What it optimises.** The most useful, trustworthy progress between the CFO's check-ins, without creating more risk or review work than she can absorb. It chooses the **next useful action**, not just the next production task:
- produce when the job is well understood;
- otherwise evaluate, draft, gather context or decompose when that makes production more worthwhile;
- use failures as signals;
- prefer steady, inspectable progress; waiting is acceptable.

**Inputs it has or will have**
- tags and scores (priority, family, ambiguity, complexity, parsability, context sensitivity, drafts, output quality);
- consensus (accepted, disputed or missing; confidence and margin; evidence);
- staleness (hashes, definition versions, drift);
- failure history (review indicator, attempt count, linked windows and prior);
- the six failure signals;
- chartedness and provisional artifacts;
- time (expected vs consumed, latency, queue age);
- dependencies (prerequisites, what it unlocks, parent/child, federation impact);
- controls and budgets (BLOCK, HOLD, FORCE, PIN; autonomy; pauses; pace). FORCE changes rank, not evidence requirements.

**Missing inputs**
- **benefit**: how much an action advances objectives, including what it unlocks;
- **human attention**: review effort, next check-in, acceptable approval backlog;
- **resource feasibility**: VRAM/context fit, contention between local evaluation and drafting, frontier availability and allowance;
- **action forecasts**: cost, success likelihood and uncertainty per action type;
- **scheduling policy**: fairness, starvation, switching costs, limits on low-value reassessment.

**Shape, recommended: hybrid**
| Option | For | Risk |
|---|---|---|
| One scored queue behind hard gates | simple, explainable | unlike actions compete on one misleading score |
| Separate queues per channel | respects different resources | channels optimise locally while the project stalls |
| **Hybrid**: shared policy, channel queues, a coordinating scheduler | balances value and resources | more parts; possible oscillation |

How the hybrid works:
1. Hard gates (permissions, current evidence, dependencies, safety, holds, budgets).
2. Candidate actions (evaluate, refresh, draft, produce, decompose, prepare review).
3. Rank eligible actions by expected benefit, uncertainty reduction, cost and risk.
4. Allocate capacity across local evaluation/drafting and Juggler production, within the CFO's pace.
5. **Explain every choice:** why this, why now, and what would change it.

**Rule:** eligibility, priority, routing and repo inclusion stay separate. A high score never buys its way past a hard constraint.

## Step 2 — *history* (Gemini 3.5 Flash-Lite; Argon/Flash/Pro quota exhausted until 23:58; treated as raw input)
- **Agrees with the hybrid shape.** Tie capacity allocation explicitly to local hardware (VRAM residency of the 6-pass evaluation models) and the CFO's pace governor, rather than abstract resource pools.
- **Benefit, without AI judgement:** from graph topology. Count the dependent tasks and objectives a success would unblock, combined with critical-path position.
- **Human attention, without AI judgement:** the approval backlog (count and age of items awaiting confirmation or review). As it grows, throttle work that needs human validation and shift capacity to self-contained work.
- **Missed ideas:**
  1. **VRAM / model-residency contention:** switching costs between local evaluation, local drafting and Juggler runs.
  2. **Objective health:** boost work that targets or unblocks objectives with rising failure/review indicators, so blocked areas don't silently rot while the loop does easy peripheral tasks.

## Step 3 — GPT-6.1 Sol (5 Nov, 02:25): integer ranking policy — *history; superseded for M3a by Q1 v2 §4–§5*
D = 10000. G = conjunction of hard gates. B, U, C, R ∈ [0, D] = benefit, expected uncertainty reduction, normalised cost, risk. Integer weights w_B + w_U = D.

- `V = (w_B·B + w_U·U) // D`
- `S = (V·(D − R)) // (D + C)`, so S ∈ [0, D].
- If G = 0 the item is **excluded**, not scored zero.
- Cost uses a versioned normalisation scale.
- Unknown-cause failures may raise U (investigation value); they never alter gates.

**Oscillation:**
- freeze inputs per scheduling epoch;
- never preempt running work;
- change a queued preference only when it beats the incumbent by a configured margin.

**Starvation:** every m-th dispatch is reserved for the oldest continuously eligible item, with deterministic ties. This guarantees eventual dispatch given finite predecessors, continuing capacity and terminating jobs.

**Principle:** separate eligibility (gates) from preference (score).

*PM (02:25, before the directive): adopt as the M3a draft. Weights, m and the margin are versioned config. B, U and R still need concrete feature definitions; that is the next brainstorm step.*
