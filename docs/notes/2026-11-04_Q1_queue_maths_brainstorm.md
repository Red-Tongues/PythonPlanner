# Q1 — Queue maths brainstorm (team-first, before CFO input)

**Status:** brainstorm. The CFO asked the team to develop this before she contributes.

## Step 1 (GPT-6.1)
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

## Step 2 (Gemini 3.5 Flash-Lite; Argon/Flash/Pro quota exhausted until 23:58; treated as raw input)
- **Agrees with the hybrid shape.** Tie capacity allocation explicitly to local hardware (VRAM residency of the 6-pass evaluation models) and the CFO's pace governor, rather than abstract resource pools.
- **Benefit, without AI judgement:** from graph topology. Count the dependent tasks and objectives a success would unblock, combined with critical-path position.
- **Human attention, without AI judgement:** the approval backlog (count and age of items awaiting confirmation or review). As it grows, throttle work that needs human validation and shift capacity to self-contained work.
- **Missed ideas:**
  1. **VRAM / model-residency contention:** switching costs between local evaluation, local drafting and Juggler runs.
  2. **Objective health:** boost work that targets or unblocks objectives with rising failure/review indicators, so blocked areas don't silently rot while the loop does easy peripheral tasks.

## Step 3 — GPT-6.1 Sol (5 Nov, 02:25): integer ranking policy
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

*PM: adopt as the M3a draft. Weights, m and the margin are versioned config. B, U and R still need concrete feature definitions; that is the next brainstorm step.*
