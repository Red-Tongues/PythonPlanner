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

**Storage that stays readable for Sam**
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
