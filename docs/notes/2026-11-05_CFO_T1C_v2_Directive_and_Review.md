# Architectural Directive & Alignment Review: T1-C v2, Queue Maths, and M3a Re-alignment

**From:** The CFO  
**To:** Project Manager (Claude) & Engineering Team (Claude Code / Review Panel)  
**Date:** 5 November 2026  
**Subject:** Authoritative Responses to T1-C v2 Questions (1–7), Project Alignment Audit, and Required Course Corrections Before M3a  
**Scope Label:** **PRODUCT** (Sections A–C) & **BUILD PROCESS** (Sections D–E)

---

## Section A — Introductory Page: Sharpened CFO Responses to the Seven T1-C v2 Questions

This introductory section provides the authoritative CFO decisions on the seven sign-off questions in `docs/notes/2026-11-05_T1C_v2.md`, followed by a traceability matrix connecting these answers to prior CFO guidance (`docs/notes/2026-11-04_CFO_Argon_clarification.md`, `docs/notes/2026-11-04_T1_genpacket_history_and_failure_theory.md`, `docs/PROJECT_PLAN.md` §20.1 & §21, and `docs/PROGRESS.md`).

### 1. What "completed work is immutable" means (Mutable Developing Packet + Sealed Snapshot + Child Lineage)
**Decision: Approved (`packets` editable while developing, sealed as a permanent snapshot once `gen_eva` completes; further iterations spawn child `genPackets`).**  
"Snapshots" is the right model. While a `genPacket` is developing (before it completes generation and post-generation grading), its fields are built up by `evaPackets` and may be edited directly in SQL, with every edit copied automatically into an append-only history table (`raw_revision_outbox` → `field_revisions` + `content_blobs`). When a `genPacket` completes generation and passes through post-generation grading (`gen_eva`), **a permanent snapshot of it is frozen (`sealed_bundles`)**. Rather than re-opening and mutating that completed packet in place, any re-imagined prompt born from its grading is created as a **child `genPacket`** (`parent_id` → sealed parent).

---

### 2. When something upstream changes (`question_context` & Objective Evidence Linking)
**Decision: Upstream and linked changes must NEVER render downstream evaluations "unusable" or stall generation. Replace hard context invalidation with a per-field `question_context` toggle/counter and objective evidence-based linking.**
- **No cascade stalling:** If an upstream change (to a Project brief, Objective brief, or another packet) rendered downstream evaluations "unusable and in need of reassessment", generation would permanently stall. When evaluations or generations are produced against upstream or downstream context, they **remain tied to that context snapshot for processing and remain usable**.
- **The `question_context` indicator:** Each evaluative field is tagged with a deterministic boolean `question_context` toggle (and integer `question_context_count`) that automatically switches on if context that contributed to that specific evaluation has since changed. **This does not apply to tasks that have already been parsed/federated into children (`waiting_for_children` / `archived`)**—only to active tasks requiring processing or remediation. The presence of `question_context` tags feeds directly into the `genQueue` / `evaQueue` mathematical calculation.
- **Objective registering of linked context by `evaPackets` (Direct vs. Peripheral):** We must distinguish between *directly linked context* and *peripheral context*. Structural hierarchy (`Project \ Objective \ Task {+parsing}`) gives baseline structure, but structural tagging alone is not sufficient to highlight which context actually drove an evaluation. **The verified `SUPPORT` / `CONTRADICTION` evidence quotes produced by `evaPackets` (passes 3–6) are the key mechanism here.** Crucially, detecting whether linked context has changed must **not** depend on LLM calls—it must rely on objective, measurable SQL comparisons against the registered evidence-linked sources.

---

### 3. "Failure maths", Re-imagined Child `genPackets`, `evaQueue ⊂ genQueue`, Continuous Enrichment, and `gen_eva`
**Decision: Reject the "3 failures in a row on the same prompt" assumption. Re-imagined prompts after `gen_eva` appear back as child `genPackets` inside `genQueue` (with `evaQueue` as a subset of `genQueue`), `Ready` / `Queued` packets are NOT immune from further `evaQueue` enrichment outside positions 0–2, and achieving `Queued` status depends purely on relational mathematical context.**

- **(i) User feedback as a mathematical input:** While "failure maths is still learning" read partly as a figure of speech, one of the core drivers making the queue mathematics sophisticated **should** be formal feedback from the user as the project progresses (confirmations, waivers, manual SQL edits, overrides, and grade adjustments fed back as mathematical weights).
- **(ii) A `genPacket` is never rerun identically; post-`gen_eva` re-imagination creates a child `genPacket`:** Failure and success scores/tags (grading) apply to *all* generated content after a `genPacket` is processed through `gen_eva`. Once a `genPacket` has been fully processed and graded, **it is impossible for it to be rerun identically**. Regardless of the grade it receives—even a nearly perfect score—the task is rewritten accordingly and **returns as a child `genPacket`** carrying additional context and a prompt re-imagined on the basis of its grade.
- **(iii) `evaQueue` is a subset of `genQueue` (`evaQueue ⊂ genQueue`), and `Ready` / `Queued` `genPackets` remain open to `evaQueue` enrichment:** All active tasks live as `genPackets` in `genQueue`, and `evaQueue` is the subset of `genQueue` that requires `evaPacket` processing. Re-imagined prompts appear back as child `genPackets`, which are **typically (though not necessarily) first fed by `evaPackets` in `evaQueue` before achieving a `Ready` status in `genQueue`**. Crucially, **achieving `Ready` or `Queued` status does NOT make a `genPacket` immune from further `evaQueue` enrichment**: while a `Ready` or `Queued` `genPacket` waits in `genQueue` (outside the position 0–2 lockout zone), `evaPackets` continue enriching its optional/secondary evaluative fields, `locally_drafted_examples`, and `question_context` refreshes.
- **(iv) Mandatory `eva` fields (`Ready`) vs. Relational Mathematical Context (`Queued`):** Achieving `Ready` requires specific mandatory `eva` fields to be filled—some fields are **universally mandatory** for all `genPackets`, and others become **relationally mandatory** based on the packet's relationships (dependencies, dependents, chartedness, or task family). **Whether a child `genPacket` achieves a `Queued` status or not depends purely on the relational mathematical context:** a well-graded, non-relational generation's child starts at the bottom of `genQueue` / `evaQueue` and may never become `Queued`, whereas a poorly-graded, pivotal (highly relational) generation's child goes right to the top and is reprocessed nearly immediately.
- **(v) `gen_eva` (Post-Generation Evaluation Gate):** The post-generation evaluation packet (`gen_eva`) is a direct deterministic gate immediately after generation; **no queue maths is required to schedule `gen_eva`**. Once `gen_eva` grades the generation, it seals the parent `genPacket` and feeds the resulting grades and historic metadata into the child `genPacket`'s positioning in `genQueue` and `evaQueue`.

---

### 4. Failures with no known cause (Distinguishing Substantive Grades from Crashes)
**Decision: Distinguish sharply between a *Crash* (infrastructure/transport fault) and a *Poorly-Graded Generation* (`gen_eva` outcome).**  
A **Crash** occurs when the channel/adapter times out, disconnects, or fails to produce gradable output (`generation_output IS NULL`). That is an operational fault tracked via a separate crash counter and route-health nudge—never conflated with task quality. A **Substantive Failure** is simply a completed generation that received a low grade in `gen_eva`; it seals the parent attempt with its low grade and spawns a high-priority re-imagined child `genPacket`.

---

### 5. Failure history after a split or clarification (Linked Lineage History)
**Decision: Preserve full linked lineage history via the `parent_id` / `root_id` tree instead of an arbitrary 10% scalar penalty carry-over.**  
Because every re-imagination, split, or clarification creates child `genPackets` linked to their parent (`parent_id`, `root_id`), the parent's exact `gen_eva` grade bundle, evidence tags, and crash telemetry remain directly queryable across the ancestor chain in SQL without requiring a synthetic "10% penalty" or manual per-cause confirmation.

---

### 6. Extending the "based-on" rule intra-packet
**Decision: Approved (`prompt_description` → pre-`eva` / `locally_drafted_examples` → `generation_output` + thinking → `gen_eva` → sealed snapshot → child `genPacket`).**  
Within a single `genPacket`, each stage records the hash of the earlier intra-packet stage it was derived from, and the re-imagined child `genPacket` records the sealed `gen_eva` bundle ID/hash of the parent that spawned it.

---

### 7. Editing a packet or running calculations while a `genPacket` is in generation (The Position 0–2 Lockout Rule)
**Decision: Rejected PM recommendation. Calculations must NEVER override, cancel, or stale an active generation. Exclude `evaPackets` linked to `genQueue` positions 0, 1, or 2 from `evaQueue`.**
- **Calculations never override generations:** Once a generation is running, background calculations, `evaPackets`, or upstream edits must never cancel the run or discard its output. Let the generation complete and pass through `gen_eva`.
- **The `genQueue` Position 0 / 1 / 2 Lockout Rule:** Even though `Ready` and `Queued` `genPackets` normally remain in `evaQueue` for ongoing enrichment, **`evaPackets` should NOT be included in `evaQueue` (and cannot be run) if they belong to, or are directly related to, a `genPacket` in `genQueue` positions `0`, `1`, or `2`**. Because `evaPackets` run much faster than `genPackets`, this 3-slot buffer guarantees that no `evaPacket` mutates a `genPacket` or its direct context right as it enters generation.

---

### Alignment & Traceability Matrix Against Prior CFO Guidance

| Prior Guidance Source | Key Prior Decisions | Alignment with Today's 7 Decisions |
|---|---|---|
| `docs/notes/2026-11-04_CFO_Argon_clarification.md` (Parts A, B, D) | **Part A:** `genPacket` noun vs `eva_packet` verb; `locally_drafted_examples`. **Part B:** Park Tailscale (`B1`), split M3 into M3a/M3b (`B2`), no separate test project (`B3`), no automatic re-runs because queue maths decides (`B4`). **Part D:** `genPacket` history (`D1`), federating parent tasks (`D2`), queue maths as the sole judge (`D3`), Juggler as sole production route (`D4`), build tidy (`D5`). | **100% Aligned & Extended:** Q3 formalises `B4` and `D3` (`genQueue` + `evaQueue ⊂ genQueue`, relational maths decides `Queued`, no identical re-runs). Q2 protects federated parents (`D2`) from `question_context`. Q7 protects high-value Juggler runs (`D4`) from calculation overrides. |
| `docs/notes/2026-11-04_T1_genpacket_history_and_failure_theory.md` (Rounds 1–4, `C1`–`C40`) | `C1`: All fields amendable by `eva_packets`. `C2`–`C6`, `C31`–`C34`: Staleness is visible, never silent. `C7`–`C12`, `C27`–`C30`: Failure taxonomy, task families, linked history. `C15`–`C18`: User feedback & confirmation toggles. `C19`: Raw SQL edit capture. `C23`–`C26`: 4 time factors. `C39`–`C40`: Chartedness & provisional git commits. | **Refined & Unified:** Q1 adopts `C19` (`raw_revision_outbox` + `sealed_bundles`). Q2 refines `C2`–`C6`/`C31`–`C34` so upstream drift flips `question_context` via verified `evaPacket` evidence quotes rather than invalidating evaluations. Q3–Q5 replace the `K=3` same-prompt breaker with post-`gen_eva` child `genPackets` and Crash vs. Grade separation. |
| `docs/PROJECT_PLAN.md` (§20.1 & §21) & `docs/PROGRESS.md` | **§20.1:** `1.0.0.0.devN` versioning, Continuous Autonomy Slider (`0.0–1.0`), mandatory placeholder tagging (`@ORCHESTRATOR:PLACEHOLDER` / `PROVISIONAL`). **§21:** Dated decision log. **`PROGRESS.md`:** Single-schema baseline PR #3, concurrency review. | **Fully Aligned:** User involvement/feedback weights (`Q3.i`), placeholder/provisional code markers (`C39`–`C40`), and the single master schema (`PR #3`) are preserved and integrated into M3a. |

---

## Section B — Summarising Statement from the CFO

> **Statement from the CFO to the Project Manager and Engineering Team:**
>
> Looking across our recent sessions—the Argon architectural clarification, the T1 history and failure theory rounds, and now T1-C v2—the team has done impressive work on database rigor, deterministic SQL consensus, and concurrency safety. However, reading the seven T1-C v2 questions side by side made it clear to me that **our specification has drifted into treating a `genPacket` like a static, one-shot CI job that gets evaluated once, freezes its metadata at `Ready`, and then either passes or fails three times on the same prompt**.
>
> That is not what the Python Orchestrator is.
>
> Every task in the system is a `genPacket` living in `genQueue`, with **`evaQueue` operating as a subset of `genQueue`**. Once a `genPacket` is generated and graded by the immediate post-generation gate (`gen_eva`), it is **never rerun identically**. Instead, that `genPacket` is sealed as a permanent snapshot with its grade, and a **re-imagined prompt appears back as a child `genPacket`** enriched with the parent's output, `gen_eva` critique, and updated context. That child `genPacket` enters `genQueue`—typically (though not necessarily) being fed first by `evaPackets` in the `evaQueue` subset before achieving `Ready` status.
>
> Two rules govern what happens next, and both must be unmistakable in our code and SQL views:
> 1. **Whether a `Ready` `genPacket` achieves `Queued` status depends purely on the relational mathematical context**—a child of a well-graded, non-relational generation starts at the bottom of `genQueue` and may never become `Queued`, whereas a child of a poorly-graded, pivotal generation goes straight to the top and is reprocessed immediately.
> 2. **Achieving `Ready` or `Queued` status never makes a `genPacket` immune from further `evaQueue` enrichment**—while waiting in `genQueue`, `Ready` and `Queued` packets continue to receive `evaPackets` in `evaQueue` (filling optional fields, `locally_drafted_examples`, and `question_context` refreshes) all the way up `genQueue` **until** they, or a `genPacket` they are directly related to, enter **`genQueue` positions 0, 1, or 2**, where `evaPackets` are excluded so fast local calculations never collide with or override active generations.
>
> Equally important, upstream edits must **not** render downstream evaluations unusable or stall generation. Evaluations remain tied to the context snapshot that produced them and stay usable while a per-field `question_context` toggle alerts the queue maths—and the verified evidence quotes already produced by our `evaPackets` give us an objective, non-LLM way to register which upstream context is directly linked to which field.
>
> The remainder of this document grades our current codebase (`main` and PR #3) and planning notes against this architecture, identifies the exact modules and spec sections that need adjustment or rollback, and provides non-production reference rewrites to guide the M3a specification.

---

## Section C — Reference & Evaluation of Existing Code and Plans, and Reference Draft Rewrites

### Overall Project Trajectory Grade: **B+ (Strong Engineering Foundation; Conceptual Course-Correction Required Before M3a)**

| Subsystem / Track | Grade | Alignment Assessment Against CFO Architecture |
|---|---|---|
| **Database & Dialect Engineering** (`db/models.py`, `db/ddl.py`, `sql/views.py`, `PR #3`) | **A** | Single master schema, generated baseline, fixed-point $10^4$ consensus parity across SQLite & PostgreSQL 16, and 599 passing tests are excellent and ready for M3a. |
| **6-Pass Quickfire Engine** (`evaluation/evaluator.py`, `consensus.py`) | **A-** | Stateless 6-pass execution, exact quote verification (`verify_quote`), and SQL consensus are solid. Needs objective evidence-source link registration for `question_context` and scheduling via `v_eva_queue`. |
| **Lineage, Sealing & Re-imagination** (`repository.py`, `branching.py`, `T1-C v2` §1) | **B** | Existing `parent_id`/`root_id` tree on `packets` and `T1-C v2`'s `raw_revision_outbox` + `sealed_bundles` fit the snapshot + child `genPacket` model well. Must retire `supersedes_id` whole-row cloning and wire post-`gen_eva` child `genPacket` creation. |
| **Staleness & Context Binding** (`gates.py`, `v_field_consensus`, `T1-C v2` §2) | **C** | **Misaligned in code:** `require_current_context` folds Project/Objective briefs into `packets.input_hash`, wiping out (`pending`/`NULL`) all evaluations on any upstream edit. Must be replaced with `question_context` and objective evidence links. |
| **Queue & Failure Model** (`queue.py`, `v_queue`, `readiness.py`, `Q1`, `T1-C v2` §3) | **C-** | **Misaligned:** Conflates crashes with `gen_eva` grades; enforces `K=3` same-prompt retry breaker; stops evaluating once `Queued`; lacks `evaQueue ⊂ genQueue`, universal vs. relational mandatory gates, and relational `Ready → Queued` admission. |
| **Run Concurrency & Generation Protection** (`runs.py`, `T1-C v2` §6.1 & Q7) | **C** | **Reversed priority:** `binding_mismatch` in `runs.py` stales active/accepted generations when governance or context changes, and `T1-C v2` Q7 proposed cancelling running generations on SQL edit. Must protect generations and enforce the `genQueue` Position 0–2 lockout. |

---

### C.i — Detailed Reference and Evaluation of Existing Code and Plans

#### 1. Whole-Row `supersedes_id` Cloning vs. Mutable Developing Packet + Sealed Snapshot + Re-imagined Child `genPacket`
- **Current code & spec references:** `src/orchestrator/repository.py:217–245` (`update_packet` raises `ImmutableRecordError` when editing core fields outside `DRAFT`, forcing `supersede_packet` to clone the row via `supersedes_id`); `src/orchestrator/db/ddl.py:45–95` (`trg_packets_immutable_when_locked`); `docs/notes/2026-11-05_T1C_v2.md` §1 (`raw_revision_outbox`, `field_revisions`, `content_blobs`, `sealed_bundles`).
- **Required change / rollback:** Adopt `T1-C v2` §1's `raw_revision_outbox` + `field_revisions` + `content_blobs` so developing `genPackets` (`draft`, `evaluating_pre`, `ready`, `queued`) can be updated in place (including directly in SQL) with automatic append-only history capture. When `gen_eva` completes, freeze the completed `genPacket` into `sealed_bundles` (`locked = True`, `status = 'completed'`) and **spawn the re-imagined prompt as a child `genPacket`** (`parent_id = parent.id`, `root_id = parent.root_id or parent.id`, `depth = parent.depth + 1`) rather than re-opening the same row across "episodes" or cloning via `supersedes_id`.

#### 2. Hard Upstream Context Invalidation vs. `question_context` & Objective `evaPacket` Evidence Linking
- **Current code & spec references:** `src/orchestrator/evaluation/evaluator.py:137–168` (`packet_document` hashes `project.brief` and `objective.brief` into `packets.input_hash`); `src/orchestrator/gates.py:88–118` & `180–215` (`require_current_context` and `bump_scope` kick `ready`/`queued` packets back to `evaluating_pre` on any Project/Objective edit); `src/orchestrator/sql/views.py:252–290` (`v_field_consensus` sets `is_current = 0`, `status = 'pending'`, and `final_value = NULL` when `e.target_hash != p.input_hash`).
- **Required change / rollback:**
  1. Restrict `packets.input_hash` to intra-packet stage-1 fields only (`title`, `prompt_description`, `response_contract`, `repo_target_path`).
  2. Add an `eva_context_links` table populated deterministically when `evaPackets` run: record structural context (`project`, `objective`, `packet_dependencies`) AND inspect verified `SUPPORT`/`CONTRADICTION` quotes in `evaluation_passes.evidence` (passes 3–6) to classify each link as `direct` (explicitly cited in verified evidence or hard dependency) or `peripheral` (ambient structural context).
  3. Update `v_field_consensus` so upstream/linked changes **never null out `final_value` or drop `is_current` to 0**; instead, compute `question_context` (`0/1`) and `question_context_count` (`direct` links weighted above `peripheral`), while exempting packets in `waiting_for_children` or `archived`.

#### 3. `evaQueue ⊂ genQueue`, Continuous Enrichment of `Ready` / `Queued` Packets, Universal vs. Relational Mandatory Fields, and Relational `Ready → Queued` Admission
- **Current code & spec references:** `src/orchestrator/evaluation/evaluator.py:67–76` (`PRE_STATUSES_TO_EVALUATE` excludes `PacketStatus.QUEUED`, making queued packets immune from evaluation); `src/orchestrator/sql/views.py:385–465` (`v_queue` only ranks `ready`/`queued` packets with a static 5-term sum); `src/orchestrator/evaluation/readiness.py:100–195` (`assess_readiness` checks only static `eva_presets.required`); `src/orchestrator/queue.py:161–235` (`admit_next` blindly promotes any `ready` packet to `queued`).
- **Required change / rollback:**
  1. Replace `v_queue` with `v_gen_queue` (all active unsealed `genPackets`) and its subset view `v_eva_queue` (`evaQueue ⊂ genQueue`).
  2. **Ensure `Ready` and `Queued` `genPackets` remain in `v_eva_queue` for continuous enrichment:** `v_eva_queue` must include statuses `('draft', 'evaluating_pre', 'ready', 'queued')` whenever a packet has missing mandatory fields, missing optional/secondary fields, missing `locally_drafted_examples`, or `question_context_count > 0`—**excluding** packets in `genQueue` positions `0`, `1`, or `2` or directly linked to positions `0`, `1`, or `2`.
  3. Split readiness checks in `readiness.py` into **universally mandatory `eva` fields** and **relationally mandatory `eva` fields** (triggered by dependency edges, dependents, chartedness, or federating-parent status).
  4. Gate `Ready → Queued` transition in `queue.py:admit_next` on the relational mathematical context (`relational_score_e4 >= relational_admission_floor_e4`) so well-graded, non-relational child `genPackets` stay `ready` at the bottom of `genQueue` while poorly-graded, pivotal child `genPackets` immediately achieve `queued`.

#### 4. Separating Crashes from `gen_eva` Grades & Removing `enforce_failure_budgets`
- **Current code & spec references:** `src/orchestrator/queue.py:109–158` (`enforce_failure_budgets` parks packets after 3 failed runs into `awaiting_confirmation`); `docs/notes/2026-11-05_T1C_v2.md` §3 (`K=3` consecutive failures on the same prompt revision → `needs_review`).
- **Required change / rollback:** Remove `enforce_failure_budgets` from `queue.py` and drop the `K=3` same-prompt circuit breaker from `T1-C v2` §3. Distinguish between **Crashes** (`generation_output IS NULL` due to timeout/adapter fault → increment `crash_count`, keep `genPacket` unsealed, nudge route health) and **Completed Generations** (`generation_output IS NOT NULL` → immediately run `gen_eva` gate without queue maths, seal parent `genPacket` with its `gen_eva` grade, and spawn the re-imagined child `genPacket`).

#### 5. Protecting Active Generations & Enforcing the `genQueue` Position 0 / 1 / 2 Lockout Rule
- **Current code & spec references:** `src/orchestrator/runs.py:169–215` (`binding_mismatch` stales active/accepted runs if `gate_revision` or `input_hash` changes); `docs/notes/2026-11-05_T1C_v2.md` §6.1 & Q7 ("cancel the run, so your edit wins").
- **Required change / rollback:** Remove `gate_revision` and upstream `input_hash` checks from `binding_mismatch` in `runs.py` so background calculations, `evaPackets`, and context edits never cancel or stale an active generation. Enforce the `genQueue` Position 0 / 1 / 2 Lockout Rule in `v_eva_queue` and `evaluator.py` so `evaPackets` are never scheduled on, or on direct links of, `genPackets` in `genQueue` positions `0`, `1`, or `2`.

---

### C.ii — Quick Draft Rewrites (Non-Production Reference Sketches for the Engineering Team)

#### Sketch 1: `v_gen_queue` and Subset `v_eva_queue` with Continuous `Ready`/`Queued` Enrichment & Position 0–2 Lockout (`sql/views.py`)

```sql
-- Reference sketch: Master genQueue + Subset evaQueue (src/orchestrator/sql/views.py)
CREATE VIEW v_gen_queue AS
WITH active_packets AS (
    SELECT
        p.id AS packet_id,
        p.ref AS packet_ref,
        p.parent_id,
        p.root_id,
        p.status,
        p.created_at,
        -- Parent's sealed gen_eva grade (0 for ungraded roots; high e.g. 9500 for well-graded parent)
        COALESCE(parent_bundle.composite_grade_e4, 0) AS parent_grade_e4,
        -- Objective & dependency relational centrality (0..10000)
        COALESCE(rel.centrality_e4, 0) AS relational_centrality_e4,
        COALESCE(fh.total_question_context, 0) AS question_context_count,
        COALESCE(fh.missing_mandatory_fields, 0) AS missing_mandatory_fields,
        COALESCE(fh.missing_optional_fields, 0) AS missing_optional_fields,
        COALESCE(fh.missing_local_drafts, 0) AS missing_local_drafts,
        COALESCE(uf.user_feedback_bonus_e4, 0) AS user_feedback_bonus_e4
    FROM packets p
    LEFT JOIN sealed_bundles parent_bundle ON parent_bundle.packet_id = p.parent_id
    LEFT JOIN v_packet_relational_centrality rel ON rel.packet_id = p.id
    LEFT JOIN v_packet_field_health fh ON fh.packet_id = p.id
    LEFT JOIN v_packet_user_feedback uf ON uf.packet_id = p.id
    WHERE NOT p.locked
      AND p.status NOT IN ('completed', 'archived', 'closed_unverified', 'waiting_for_children')
),
scored AS (
    SELECT
        a.*,
        -- Poorly-graded + pivotal (high relational centrality) -> near 10,000 (top of queue)
        -- Well-graded + non-relational -> near 0 (bottom of queue; won't cross Queued threshold)
        CAST(
            ((10000 - a.parent_grade_e4) * (3000 + 7000 * a.relational_centrality_e4 / 10000)) / 10000
            + (a.question_context_count * 1000)
            + a.user_feedback_bonus_e4
        AS INTEGER) AS relational_score_e4
    FROM active_packets a
)
SELECT
    s.*,
    -- genQueue Position: 0 = running; 1..N = ordered across active genPackets
    CASE
        WHEN s.status = 'running' THEN 0
        ELSE ROW_NUMBER() OVER (
            ORDER BY
                CASE WHEN s.status = 'queued' THEN 0 WHEN s.status = 'ready' THEN 1 ELSE 2 END,
                s.relational_score_e4 DESC,
                s.created_at ASC,
                s.packet_ref ASC
        )
    END AS gen_position
FROM scored s;


-- evaQueue is a SUBSET of genQueue:
-- 1. Includes 'draft', 'evaluating_pre', AND 'ready' / 'queued' genPackets (not immune from enrichment!)
-- 2. Excludes genPackets in genQueue positions 0, 1, or 2 AND any genPacket directly related to them.
CREATE VIEW v_eva_queue AS
WITH lockout_zone AS (
    SELECT packet_id FROM v_gen_queue WHERE gen_position IN (0, 1, 2)
    UNION
    SELECT pd.depends_on_id AS packet_id
    FROM packet_dependencies pd
    JOIN v_gen_queue gq ON gq.packet_id = pd.packet_id
    WHERE gq.gen_position IN (0, 1, 2)
    UNION
    SELECT pd.packet_id
    FROM packet_dependencies pd
    JOIN v_gen_queue gq ON gq.packet_id = pd.depends_on_id
    WHERE gq.gen_position IN (0, 1, 2)
    UNION
    SELECT p.parent_id AS packet_id
    FROM packets p
    JOIN v_gen_queue gq ON gq.packet_id = p.id
    WHERE gq.gen_position IN (0, 1, 2) AND p.parent_id IS NOT NULL
)
SELECT
    gq.packet_id,
    gq.packet_ref,
    gq.status,
    gq.gen_position,
    gq.relational_score_e4,
    gq.missing_mandatory_fields,
    gq.missing_optional_fields,
    gq.missing_local_drafts,
    gq.question_context_count
FROM v_gen_queue gq
WHERE gq.status IN ('draft', 'evaluating_pre', 'ready', 'queued')
  AND (
      gq.missing_mandatory_fields > 0
      OR gq.missing_optional_fields > 0
      OR gq.missing_local_drafts > 0
      OR gq.question_context_count > 0
  )
  AND gq.packet_id NOT IN (SELECT packet_id FROM lockout_zone)
ORDER BY gq.relational_score_e4 DESC, gq.created_at ASC;
```

#### Sketch 2: Objective Evidence-Link Registration & `question_context` (`evaluation/evaluator.py`)

```python
# Reference sketch: src/orchestrator/evaluation/evaluator.py
def intra_packet_hash(packet: Packet) -> str:
    """Hash ONLY the packet's own stage-1 definition. Upstream brief edits never alter this."""
    return sha256(
        packet.title,
        packet.prompt_description,
        packet.response_contract,
        packet.repo_target_path,
    )


def register_context_links(
    session: Session,
    eva_packet: EvaPacket,
    project: Project,
    objective: Objective | None,
    dependencies: list[Packet],
    verified_quotes: list[VerifiedQuote],
) -> list[EvaContextLink]:
    """Objective, non-LLM registration of context sources for question_context tracking.

    Uses verified SUPPORT/CONTRADICTION evidence quotes from passes 3-6 to upgrade a structural
    source from 'peripheral' to 'direct' whenever the evaPacket cited evidence from that source.
    """
    cited_entities = {(q.source_type, q.source_ref) for q in verified_quotes}
    links: list[EvaContextLink] = []

    links.append(
        EvaContextLink(
            eva_packet_id=eva_packet.id,
            source_type="project",
            source_ref=project.ref,
            source_hash=sha256(project.name, project.brief),
            link_strength="direct" if ("project", project.ref) in cited_entities else "peripheral",
        )
    )
    if objective is not None:
        links.append(
            EvaContextLink(
                eva_packet_id=eva_packet.id,
                source_type="objective",
                source_ref=objective.ref,
                source_hash=sha256(objective.name, objective.brief),
                link_strength="direct" if ("objective", objective.ref) in cited_entities else "peripheral",
            )
        )
    for dep in dependencies:
        links.append(
            EvaContextLink(
                eva_packet_id=eva_packet.id,
                source_type="packet",
                source_ref=dep.ref,
                source_hash=intra_packet_hash(dep),
                link_strength="direct",
            )
        )
    return links
```

#### Sketch 3: Universal vs. Relationally Mandatory `eva` Fields & Relational `Ready → Queued` Admission (`readiness.py` & `queue.py`)

```python
# Reference sketch: src/orchestrator/evaluation/readiness.py & src/orchestrator/queue.py
UNIVERSALLY_MANDATORY_FIELDS: tuple[str, ...] = (
    "clarity",
    "specificity",
    "testability",
    "scope_fit",
)

RELATIONALLY_MANDATORY_FIELDS: dict[str, tuple[str, ...]] = {
    "has_dependencies": ("dependency_clarity", "interface_stability"),
    "has_dependents": ("downstream_contract_clarity", "blast_radius"),
    "charted_repo_target": ("regression_risk", "sonar_hotspot_alignment"),
}


def mandatory_fields_for_packet(session: Session, packet: Packet) -> set[str]:
    """Combine universally mandatory eva fields with relationally mandatory eva fields."""
    required = set(UNIVERSALLY_MANDATORY_FIELDS)
    if has_inbound_dependencies(session, packet.id):
        required.update(RELATIONALLY_MANDATORY_FIELDS["has_dependencies"])
    if has_outbound_dependents(session, packet.id):
        required.update(RELATIONALLY_MANDATORY_FIELDS["has_dependents"])
    if is_charted_target(session, packet.repo_target_path):
        required.update(RELATIONALLY_MANDATORY_FIELDS["charted_repo_target"])
    return required


def admit_ready_packets_by_relational_context(
    session: Session,
    *,
    relational_admission_floor_e4: int = 3500,
) -> list[Packet]:
    """Only promote Ready genPackets to Queued when their relational mathematical score
    crosses the relational admission floor. Well-graded, non-relational children stay Ready
    at the bottom of genQueue unless relational context changes.
    """
    rows = session.execute(
        text(
            """
            SELECT packet_id, relational_score_e4
            FROM v_gen_queue
            WHERE status = 'ready'
              AND missing_mandatory_fields = 0
              AND relational_score_e4 >= :floor
            ORDER BY relational_score_e4 DESC
            """
        ),
        {"floor": relational_admission_floor_e4},
    ).all()
    admitted: list[Packet] = []
    for packet_id, score_e4 in rows:
        pkt = session.get(Packet, packet_id)
        if pkt is not None and pkt.status == PacketStatus.READY:
            transition(
                pkt,
                PacketStatus.QUEUED,
                KERNEL,
                f"admitted to Queued by relational score ({score_e4} >= {relational_admission_floor_e4})",
            )
            admitted.append(pkt)
    return admitted
```

#### Sketch 4: Immediate `gen_eva` Gate, Parent Snapshot Sealing, and Spawning the Re-imagined Child `genPacket` (`evaluation/gen_eva.py`)

```python
# Reference sketch: src/orchestrator/evaluation/gen_eva.py
def complete_gen_eva_and_spawn_reimagined_child(
    session: Session,
    parent: Packet,
    run: GenerationRun,
    artifact: Artifact,
    provider: LLMProvider,
) -> Packet:
    """Immediate post-generation gate (gen_eva):
    1. Evaluates generated output + recorded thinking (no queue maths required for gen_eva).
    2. Seals and locks the parent genPacket into sealed_bundles.
    3. Spawns a child genPacket with a prompt re-imagined on the basis of its gen_eva grade.
    4. Routes the child into genQueue (typically entering evaQueue first as evaluating_pre,
       or starting Ready if mandatory fields are already satisfied).
    """
    # 1. Immediate post-generation 6-pass evaluation (gen_eva)
    post_report = evaluate_packet(session, parent, provider, phase=EvaPhase.POST)
    grade_e4 = compute_composite_gen_eva_grade_e4(session, parent)

    # 2. Freeze permanent snapshot of the parent genPacket
    bundle = seal_generation_bundle(
        session, packet=parent, run=run, artifact=artifact, composite_grade_e4=grade_e4
    )
    transition(parent, PacketStatus.COMPLETED, KERNEL, f"sealed after gen_eva (grade_e4={grade_e4})")

    # 3. Spawn re-imagined prompt as a child genPacket linked to the sealed parent
    reimagined_prompt = build_reimagined_prompt(
        session, parent=parent, artifact=artifact, post_report=post_report, grade_e4=grade_e4
    )
    child = Packet(
        id=new_id(),
        ref=next_packet_ref(session),
        project_id=parent.project_id,
        objective_id=parent.objective_id,
        position=parent.position + 1,
        packet_kind=parent.packet_kind,
        title=f"{parent.title} (re-imagined)",
        prompt_description=reimagined_prompt,
        response_structure=parent.response_structure,
        response_contract=parent.response_contract,
        repo_target_path=parent.repo_target_path,
        parent_id=parent.id,
        root_id=parent.root_id or parent.id,
        depth=parent.depth + 1,
        branch_reason=f"re-imagined from bundle {bundle.id} (grade_e4={grade_e4})",
        status=PacketStatus.DRAFT,
        source="gen_eva_reimagination",
    )
    session.add(child)
    session.flush()

    # 4. Typically enters evaQueue first before achieving Ready, unless already Ready
    if assess_readiness(session, child).ready:
        transition(child, PacketStatus.READY, KERNEL, "mandatory fields satisfied on creation")
    else:
        transition(child, PacketStatus.EVALUATING_PRE, KERNEL, "entering evaQueue subset")
    return child
```

#### Sketch 5: Protecting Active Generations from Calculation Overrides (`runs.py`)

```python
# Reference sketch: src/orchestrator/runs.py
def binding_mismatch(
    session: Session, run: GenerationRun, *, now: datetime | None = None, lease: bool = True
) -> str | None:
    """CFO Rule (Q7): Calculations, evaPackets, and upstream context edits NEVER override or
    stale an active generation run. Only fencing takeover or explicit owner abort stops a run.
    """
    session.refresh(run)
    packet = session.get(Packet, run.packet_id)
    if packet is None:
        return "packet missing"
    session.refresh(packet)
    if run.fencing_token is None:
        return "legacy run without a fencing token"
    if packet.status in (PacketStatus.CLOSED_UNVERIFIED, PacketStatus.BLOCKED):
        return f"packet explicitly halted by owner ({packet.status})"
    if packet.active_run_id != run.id or packet.fencing_token != run.fencing_token:
        return f"fencing token {run.fencing_token} superseded by {packet.fencing_token}"
    if lease and run.lease_expires_at is not None and run.lease_expires_at < (now or utcnow()):
        return "lease expired"
    # NOTE: gate_revision and upstream context changes do NOT stale an active generation.
    # Furthermore, the genQueue 0/1/2 lockout prevents evaPackets from running on or against
    # packets at the front of genQueue.
    return None
```

---

## Section D — What Is Going Well and Must Be Kept

1. **Parent / Root Tree Lineage on `packets` (`parent_id`, `root_id`, `depth`, `v_branch_stats`):** Because `packets` already models hierarchical parent-child trees with root rollup views, representing re-imagined prompts after `gen_eva` as **child `genPackets`** fits the existing database structure cleanly without bolted-on tables.
2. **Single Master Schema & Generated Baseline (`db/models.py`, `db/ddl.py`, `sql/views.py`, `scripts/generate_baseline.py`):** PR #3's consolidation into one generated baseline migration verified against both SQLite and PostgreSQL 16 (599 passing tests) is clean, fast, and eliminates migration drift.
3. **Outbox Trigger & Producer Attribution PoC (`T1-C v2` §7–§8):** The `raw_revision_outbox` + `content_blobs` + `field_revisions` trigger design and the SQLite/PostgreSQL producer attribution fix (`user_sql` vs. `kernel_worker`) allow direct SQL edits on developing `genPackets` with full append-only history capture.
4. **Fixed-Point $10^4$ Consensus Views & Verified Quote Matching (`v_eva_item_consensus`, `verify_quote`):** Cross-dialect integer fixed-point consensus and deterministic quote verification in passes 3–6 provide the exact building blocks needed to register objective `direct` vs. `peripheral` context links for `question_context`.
5. **Fencing Tokens & Lease Contention Guardrails (`runs.py`):** Monotonic fencing tokens (`packets.fencing_token`) and row-level claim leases prevent double-dispatch across workers; keeping fencing while removing `gate_revision`/`input_hash` staling preserves worker safety without letting calculations override generations.
6. **Provisional Git Refs & Chartedness Protection (`T1-C v2` §4):** Persisting partial/failed outputs in `content_blobs` and `refs/provisional/<packet_id>/<attempt_no>` while gating working-tree writes by target-path chartedness (`@ORCHESTRATOR:PROVISIONAL`) honours C39 and C40.
7. **Build & Repo Tidy (`D5`):** Keeping `docs/PROJECT_PLAN.md` as the spec of record, milestone specs in `docs/milestones/`, dated notes in `docs/notes/`, and M3 split into M3a (kernel loop + CLI) and M3b (dashboard + Juggler adapter) keeps the engineering scope disciplined.

---

## Section E — Conclusion & Ordered Next Steps

With the clarifications in this directive, the architecture forms a single, self-consistent loop: developing `genPackets` are mutable (with automatic revision history) and sit in `genQueue`, where **`evaQueue` is a subset of `genQueue`** that enriches both pre-`Ready` and `Ready`/`Queued` `genPackets` right up until the **`genQueue` Position 0–2 lockout boundary**. Upstream changes never invalidate evaluations or stall generation; they flip deterministic **`question_context`** flags backed by verified `evaPacket` evidence links. Active generations are never overridden by calculations. And when a generation finishes, **`gen_eva`** immediately grades it, seals the parent `genPacket` snapshot, and spawns a **re-imagined child `genPacket`** whose promotion from `Ready` to `Queued` is governed purely by relational mathematical context.

### Ordered Action Plan for the PM and Engineering Team
1. **Step 1 — Merge PR #3 (`chore/single-schema-baseline`) to `main`:** Lock in the consolidated master schema and concurrency test suite as the foundation for M3a.
2. **Step 2 — Issue `T1-C v3 (Final)` & `Q1 v2` Alignment Patch:** Update `docs/notes/2026-11-05_T1C_v2.md` and `docs/notes/2026-11-04_Q1_queue_maths_brainstorm.md` to incorporate the seven CFO decisions above (retiring `supersedes_id` cloning, `require_current_context` hard invalidation, `enforce_failure_budgets` / `K=3` same-prompt breaker, and `binding_mismatch` calculation overrides).
3. **Step 3 — Draft `docs/milestones/M3a_genpacket_loop.md` for CFO Sign-Off:** Scope M3a strictly to:
   - (a) `raw_revision_outbox` + `field_revisions` + `content_blobs` + `sealed_bundles`;
   - (b) `eva_context_links` + `question_context` in `v_field_consensus`;
   - (c) `v_gen_queue` + subset `v_eva_queue` (with continuous `Ready`/`Queued` enrichment and the Position 0–2 lockout);
   - (d) universal vs. relationally mandatory `eva` fields (`Ready`) and relational mathematical admission (`Ready → Queued`); and
   - (e) the immediate `gen_eva` gate that seals the parent `genPacket` and spawns the re-imagined child `genPacket`, separating crashes from substantive grades.
4. **Step 4 — Implement M3a on a Feature Branch with Full SQLite & PostgreSQL Parity:** Regenerate `0001_baseline.py` via `python scripts/generate_baseline.py` and verify every acceptance criterion in `pytest` across both dialects before beginning M3b (FastAPI/HTMX dashboard & live Juggler transport).
