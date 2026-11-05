# M3a — the genPacket loop (DRAFT for CFO sign-off)

**Status:** draft, **not approved**. Written 2026-11-05 from the CFO's directive (`docs/notes/2026-11-05_CFO_T1C_v2_Directive_and_Review.md`; the .docx is authoritative), Section E Step 3. No code is written until the CFO signs this off and answers, or explicitly defers, the open questions below.
**Design notes it implements:**
- T1-C v3 (Final) — `docs/notes/2026-11-05_T1C_v3_final.md`;
- Q1 v2 — `docs/notes/2026-11-04_Q1_queue_maths_brainstorm.md`.

**Reading order:** the directive → T1-C v3 → Q1 v2 → this file. Where this file and the directive differ, the directive wins, and the difference is listed as an open question.
**Serves:** Requirements C, D, F · Key Idea K1 (advances work unattended), K2 (governed), K3 (LLMs propose, SQL/Python decide), K5 (safe history).
**Builds on:** `main` after PR #3 (single master schema, generated baseline, R4 concurrency fixes).

## Goal

One self-consistent loop, visible in SQL:
- developing genPackets are editable, with automatic history;
- `evaQueue ⊂ genQueue` keeps enriching them, up to the position 0–2 lockout;
- upstream changes raise `question_context` instead of invalidating evaluations;
- `Ready` needs mandatory fields, and `Queued` needs relational maths;
- every completed generation goes straight through `gen_eva`, which seals the parent and spawns a re-imagined child genPacket;
- crashes are counted apart from grades.

## Scope — exactly Section E Step 3 (a)–(e)

| Part | Directive text | Decisions |
|---|---|---|
| **(a)** | `raw_revision_outbox` + `field_revisions` + `content_blobs` + `sealed_bundles` | Q1, Q6 |
| **(b)** | `eva_context_links` + `question_context` in `v_field_consensus` | Q2 |
| **(c)** | `v_gen_queue` + subset `v_eva_queue` (continuous `Ready`/`Queued` enrichment and the position 0–2 lockout) | Q3(iii), Q7 |
| **(d)** | universal vs relationally mandatory eva fields (`Ready`) and relational mathematical admission (`Ready → Queued`) | Q3(i), Q3(iv) |
| **(e)** | the immediate `gen_eva` gate that seals the parent genPacket and spawns the re-imagined child genPacket, separating crashes from substantive grades | Q3(ii), Q3(v), Q4, Q5, Q7 |

**Out of scope** (M3b or later, unless the CFO says otherwise, see **OQ28**):
- kernel tick loop and daemon;
- CLI beyond what tests need;
- pause/branch prompt window;
- FastAPI/HTMX dashboard and mobile digest;
- live Juggler transport (M4);
- SonarQube live scans;
- B/U/C/R feature definitions from Q1 step 3;
- reachability / API-change drift inputs (G13).

## Deliverables

### (a) Revision history and sealed snapshots
1. **Tables**, in the master schema: `db/models.py`, with triggers in `db/ddl.py`.
   - `raw_revision_outbox`: transient.
     - Columns: raw old/new values, `tx_id`/`revision_group_id`, producer, packet id/version, time.
   - `field_revisions`: append-only, hash-only.
     - Columns: packet id, field, old/new blob hash, producer, `manifest_hash`, `revision_group_id`, `based_on_hash` (Q6), time.
   - `content_blobs(hash PK, body)`: insert-once.
   - `sealed_bundles`: append-only.
     - Columns: packet id `UNIQUE`, canonical JSON blob hash, bundle hash, `composite_grade_e4`, run id, artifact hash, time.
2. **Triggers**, as proven in T1-C v2 §8:
   - the audit trigger on `packets` only INSERTs into the outbox;
   - one column-guarded trigger bumps `version` (PostgreSQL BEFORE on NEW; SQLite guarded version-only update);
   - producer attribution via `OLD.version = NEW.version`;
   - UPDATE/DELETE are rejected on `field_revisions`, `sealed_bundles` and `content_blobs`.
3. **Kernel enrichment.**
   - `drain_outbox()` writes blobs and revisions, then deletes the outbox rows.
   - It runs before any claim or seal. A packet with undrained outbox rows is not claimable or sealable.
4. **In-place edits.**
   - `repository.update_packet` edits developing genPackets (`draft`, `evaluating_pre`, `ready`, `queued`) in place.
   - `supersede_packet` is no longer used for genPackets.
   - A sealed (locked) genPacket stays immutable through the existing `trg_packets_immutable_when_locked`.

### (b) Context links and `question_context`
1. **Intra-packet input hash.** `input_hash` = hash of `title`, `prompt_description`, `response_contract`, `repo_target_path`. Project/Objective briefs leave it. (Scope of the hash: **OQ23**.)
2. **`eva_context_links`.**
   - Columns: evaPacket id, packet id, field, `source_type` (`project`/`objective`/`packet`), `source_ref`, `source_hash`, `link_strength` (`direct`/`peripheral`).
   - Written in Python when an evaPacket completes, with no LLM call.
   - A verified SUPPORT/CONTRADICTION quote from passes 3–6 upgrades its source to `direct`. Quote attribution: **OQ22**.
3. **`v_field_consensus`.**
   - It no longer nulls `final_value`, sets `status = 'pending'`, or drops `is_current` because of upstream context.
   - It adds `question_context` (0/1) and `question_context_count`, computed by SQL hash comparison of each link with the source's current hash. Weighting: **OQ4**.
   - Both are 0 for `waiting_for_children` and `archived`.
4. **Retire hard invalidation.**
   - `gates.require_current_context` no longer sends packets back to `evaluating_pre` on an upstream change.
   - `bump_scope` keeps bumping `gate_revision` for governance, but no longer causes context invalidation.

### (c) `v_gen_queue` and `v_eva_queue`
1. **`v_gen_queue`** replaces `v_queue`.
   - It covers all active, unsealed genPackets, with their relational inputs, `relational_score_e4` and `gen_position`.
   - Statuses included: **OQ2**. Position 0: **OQ1**.
2. **`v_eva_queue`** is a subset of `v_gen_queue`.
   - Statuses: `draft`, `evaluating_pre`, `ready`, `queued`.
   - Inclusion needs: missing mandatory fields, missing optional fields, missing `locally_drafted_examples`, or `question_context_count > 0`.
   - It excludes the **lockout zone**: positions 0–2 and anything directly related to them (**OQ3**).
3. **Evaluator.**
   - `PRE_STATUSES_TO_EVALUATE` includes `ready` and `queued`.
   - The evaluator picks work from `v_eva_queue`.
   - It refuses, with an event, to run an evaPacket for a locked-out packet even when called directly (defence in depth).
4. **Supporting views.**
   - `v_packet_field_health`: mandatory/optional/drafts missing, `question_context` total.
   - `v_packet_relational_centrality`: **OQ8**.
   - `v_packet_user_feedback`: **OQ9**.

### (d) `Ready` and `Queued` gates
1. **`readiness.mandatory_fields_for_packet`** = universally mandatory fields ∪ relationally mandatory fields.
   - The relational fields are triggered by dependencies, dependents, chartedness and task family.
   - Field names and catalogue: **OQ10**. How this combines with today's readiness predicate: **OQ11**.
2. **`queue.admit_next`** promotes `ready → queued` only when `relational_score_e4 ≥ relational_admission_floor_e4`.
   - The floor is versioned config (value: **OQ6**). Score bounds: **OQ7**.
3. **`queue.enforce_failure_budgets`** is removed, together with its transition rows and confirmations (see *State-machine changes*).

### (e) `gen_eva`, sealing, child genPackets, crashes
1. **New `evaluation/gen_eva.py`.** On a generation that produced gradable output, the kernel runs `gen_eva` **immediately**. It reads no queue view.
   - post-phase evaPackets over output + recorded thinking;
   - `composite_grade_e4` (definition: **OQ16**);
   - `sealed_bundles` row;
   - parent locked and moved to its terminal status (**OQ13**);
   - **exactly one** re-imagined child genPacket (prompt builder: **OQ17**; inherited fields: **OQ24**).
   - The child has `parent_id`, `root_id`, `depth + 1` and the parent's bundle id/hash.
   - All of this is one transaction, or idempotent on retry.
2. **Crash path.** A run that ends with no gradable output:
   - `crash_count + 1` on the packet;
   - a route-health counter on the channel/route;
   - no grade, no seal, no child.
   - The packet's next status and any re-dispatch: **OQ18**.
3. **`runs.binding_mismatch`** and the run CAS (`guard_bindings`) no longer compare `gate_revision` or `input_hash`.
   - Fencing token, lease and explicit owner halt remain (HOLD/pause: **OQ19**).
4. **Lineage history** is queryable in SQL: one documented query over `parent_id`/`root_id` returns each ancestor's grade, bundle hash and crash count (Q5).

## Existing modules and views to change (directive C.i)

The directive's line numbers are from an earlier tree. The ones below are from `main` @ 71977eb and are approximate.

| Module / view | Today | M3a change | Part |
|---|---|---|---|
| `db/models.py` | `packets` with `supersedes_id`, `input_hash`, `gate_revision`, `fencing_token` | new tables (a), (b); `packets.crash_count`, `packets.last_producer`, child's `parent_bundle_id`; route-health counter | a, b, e |
| `db/ddl.py` | `trg_packets_immutable_when_locked`, append-only and transition triggers | outbox audit trigger, version-bump trigger, append-only triggers for new tables; transition-union trigger regenerated | a, d, e |
| `sql/views.py` `v_field_consensus` (~l.245) | `is_current = 0`, `pending`, `NULL` on hash mismatch | `question_context`, `question_context_count`; never nulls for context | b |
| `sql/views.py` dependents of `v_field_consensus`: `v_field_status`, `v_packet_stats`, `v_branch_stats`, `v_branch_triggers`, `v_ready_candidates` | read `is_current`/`status` | re-checked for the new semantics; `v_ready_candidates` folded into the Ready gate | b, d |
| `sql/views.py` `v_queue` (~l.469–560) | ranks `ready`/`queued` with a static 5-term sum | replaced by `v_gen_queue` + `v_eva_queue`; new `v_packet_field_health`, `v_packet_relational_centrality`, `v_packet_user_feedback` | c, d |
| `repository.py` `update_packet` (~l.218), `supersede_packet` (~l.335) | `ImmutableRecordError` outside `DRAFT`; whole-row clone | in-place edits for developing statuses; no genPacket cloning | a |
| `evaluation/evaluator.py` `packet_document` (~l.166), `refresh_input_hash`, `PRE_STATUSES_TO_EVALUATE` (~l.106) | briefs in `input_hash`; `queued` excluded | intra-packet hash; `register_context_links`; `ready`/`queued` evaluated; lockout check | b, c |
| `evaluation/prompts.py` `source_document` | one concatenated document | may need section offsets for quote attribution (**OQ22**) | b |
| `evaluation/normalise.py` `verify_quote` | substring of the whole document | unchanged, or returns the matching section (**OQ22**) | b |
| `gates.py` `current_fingerprint` (~l.91), `require_current_context` (~l.97), `bump_scope` (~l.213) | upstream edit → `evaluating_pre` | no context invalidation; G bump kept for governance | b |
| `evaluation/readiness.py` `assess_readiness` (~l.149) | static `eva_presets.required` | universal + relational mandatory sets | d |
| `queue.py` `enforce_failure_budgets` (~l.127), `admit_next` (~l.186), `claim` (~l.304) | 3-failure parking; any ready → queued; E9 H recompute | removed; relational floor; claim keeps fencing, reads the intra-packet H | c, d, e |
| `runs.py` `binding_mismatch` (~l.202), `guard_bindings` (~l.252), `stale_run`, `reconcile_runs` | G/H staling | fencing/lease/owner-halt only | e |
| `confirmations.py` | failure-budget confirmations | removed for failure budget | d |
| `branching.py` | split children with `parent_id`/`root_id` | unchanged mechanics; reconciled with re-imagination children (**OQ13**, **OQ14**) | e |
| `transitions.py` → ARCHITECTURE §7 (generated) | D8 rows | see *State-machine changes* | d, e |
| `fields/candidate_fields.yaml`, `db/seed.py` (presets) | field catalogue and `required` flags | mandatory sets (**OQ10**); `locally_drafted_examples` field (not in the catalogue today) | c, d |
| new `evaluation/gen_eva.py` | — | (e) | e |
| `migrations/versions/0001_baseline.py` | generated | regenerated with `python scripts/generate_baseline.py`; never hand-edited | all |

## State-machine changes this implies (ARCHITECTURE §7 / D8) — need CFO confirmation

The directive changes behaviour that ARCHITECTURE §7's transition rows encode. The DB transition trigger enforces those rows, so each change must be explicit. Rows quoted from ARCHITECTURE §7.1/§7.2:

| # | Existing row(s) | Conflict with the directive | Proposed handling (to confirm) |
|---|---|---|---|
| S1 | `evaluating_post → completed` (E5: every required post field accepted…; quality policy; locks); `evaluating_post → needs_review` (post-eval failed); `evaluating_post → held` (alignment < 7,500) | Sketch 4 seals **every** graded parent as `completed`, whatever its grade. Today `completed` means "the completion gate passed" (enum docstring; M2.1 fix 5) and satisfies dependencies | **OQ13** |
| S2 | `failed → awaiting_confirmation` (failure budget exhausted); `queued → awaiting_confirmation` (failure budget / 48 h); `ready → awaiting_confirmation` (P1, failure-budget part); `awaiting_confirmation → queued` (failure-budget approval) | `enforce_failure_budgets` is retired | remove the failure-budget triggers; keep the 48 h touch and the split-confirmation parts; P1's text changes |
| S3 | `failed → queued` (retry within budget and backoff); `running → failed` | a crash is not a grade; the CFO–Argon note A6 says generation runs are "never re-run automatically" | **OQ18** |
| S4 | `ready → evaluating_pre`, `queued → evaluating_pre`, `failed → evaluating_pre` (kernel: "fingerprint or readiness changed; H ≠ current"); `awaiting_confirmation → evaluating_pre` ("approval but H stale") | upstream changes must not demote; H becomes intra-packet. The rows survive for an intra-packet H change, but their trigger narrows | keep the rows with narrowed H; whether enrichment can demote: **OQ12** |
| S5 | `ready → queued` (E9: H recomputed, full readiness predicate, "within failure budget") | admission adds the relational floor and drops the failure budget | the guard text changes |
| S6 | Run rows `planned/dispatched/awaiting_result/validating → stale` ("F superseded, G or H mismatch, packet closed/held/blocked"); `accepted → stale` (E8: F/G/H change); `awaiting_confirmation → stale` ("non-approval governance change or H change") | Q7: G/H changes never stale a running generation | drop G/H from the triggers; keep F superseded and closed/blocked; held: **OQ19** |
| S7 | `running → evaluating_pre` (E7 stale run); `running → evaluating_post` (publication guard: "G, H match"; write + commit) | G/H no longer gate publication; also, does the repo write happen before `gen_eva`? | **OQ15**, **OQ19** |
| S8 | P5: "New version rows start in `draft`"; the §7 notation "a core edit … never moves a row: it creates a new version row" | Q1: developing genPackets are edited in place; no new version rows | P5 retired; the §7 notation is rewritten |
| S9 | No `draft → ready` row (only `draft → evaluating_pre`) | Sketch 4 moves a new child `DRAFT → READY` "if mandatory fields are already satisfied" | **OQ27** |
| S10 | R1 / D10 Q5: exit-3 approval and rejection bump G on packet and run together and require "run G = packet G" | if runs no longer bind to G, these guards need another binding (confirmation version, F) | **OQ19** |

## Acceptance criteria

Every criterion is tested with `FakeProvider` (no network, GPU or real LLM). It passes on **SQLite** (`pytest -q`) and on **PostgreSQL 16** (`ORCH_TEST_POSTGRES_URL=… pytest -q -m postgres`, run in CI), unless it says "SQLite only". A criterion marked *(OQn)* has a test that cannot be finalised until that question is answered.

**General**
- **AC1.** `python scripts/generate_baseline.py` produces the committed baseline; `tests/test_schema.py` (drift, installed triggers/views, `compare_metadata`, downgrade round trip) passes on both dialects.
- **AC2.** No immutability, append-only or transition trigger is disabled in any test.
- **AC3.** Every new state change writes an event with actor, action, entity, before/after and reason (Golden Rule 5). Tested per change: SQL edit enrichment, seal, child spawn, crash, admission, lockout refusal.
- **AC4.** `ruff check . && ruff format --check .` is clean. The full suite passes. The ARCHITECTURE §7 tables are regenerated, and every row change from *State-machine changes* is listed in §7.3 with its reason.

**(a) History and sealing**
- **AC5.** T1–T6 from T1-C v2 §8 pass as tests on both dialects:
  - no-op assignment;
  - CAS-only update;
  - two-field edit including NULL → version +1 once, two outbox rows sharing one `tx_id`, no recursion;
  - a kernel CAS bump is +1, not +2;
  - a `user_sql` edit after a kernel edit is attributed to `user_sql`;
  - `SET status=…, version=version` still bumps.
- **AC6.** A direct SQL `UPDATE` of a developing genPacket succeeds, in each of `draft`, `evaluating_pre`, `ready` and `queued`. After `drain_outbox()`:
  - one `field_revisions` row per changed field, with blob hashes resolvable in `content_blobs`;
  - the outbox is empty;
  - one event for the user edit.
- **AC7.** `UPDATE` and `DELETE` on `field_revisions`, `sealed_bundles` and `content_blobs` are rejected by the database on both dialects.
- **AC8.** `queue.claim` and sealing refuse a packet with undrained outbox rows.
- **AC9.** Sealing writes exactly one `sealed_bundles` row; a second seal of the same packet fails on `UNIQUE(packet_id)`. The bundle hash equals sha256 of the stored canonical JSON. The sealed packet is `locked`, and any UPDATE of its semantic columns is rejected by `trg_packets_immutable_when_locked`.
- **AC10.** No M3a code path inserts a `packets` row with `supersedes_id` set. Edits of developing genPackets keep the row id; the test asserts the row count.

**(b) Context**
- **AC11.** Editing a project brief or an objective brief leaves `input_hash` unchanged for every packet. Editing `title`, `prompt_description`, `response_contract` or `repo_target_path` changes it. *(OQ23)*
- **AC12.** Each evaPacket writes `eva_context_links`: one project link, one objective link when there is an objective, one per dependency, with the `source_hash` at evaluation time.
  - With a verified quote from the objective brief, the objective link is `direct`; without one, it is `peripheral`.
  - Dependency links are `direct`.
  - *(OQ22)*
- **AC13.** After a project brief edit, accepted fields keep their `final_value`, `status` and `is_current` in `v_field_consensus`.
  - Fields linked to the project get `question_context = 1` and `question_context_count > 0`.
  - Unlinked fields stay at 0.
  - *(OQ4)*
- **AC14.** An upstream edit moves no packet's status (asserted by status and the absence of transition events).
- **AC15.** Packets in `waiting_for_children` or `archived` always show `question_context = 0`.
- **AC16.** Re-evaluating a field against the current context clears its `question_context`. The `question_context` computation makes no provider call (asserted by the FakeProvider call count).

**(c) Queues**
- **AC17.** `v_gen_queue` contains exactly the active unsealed genPackets of the fixture. It never contains a locked row or a `completed`, `archived`, `closed_unverified` or `waiting_for_children` packet. *(OQ2)*
- **AC18.** Every `v_eva_queue` row is a `v_gen_queue` row (subset test), with a status in `draft/evaluating_pre/ready/queued` and at least one need condition.
- **AC19.** A `ready` packet and a `queued` packet with missing optional fields or `question_context > 0`, outside the lockout, are in `v_eva_queue`, and the evaluator evaluates them.
- **AC20.** No packet at `gen_position` 0, 1 or 2, and no packet directly related to one, is in `v_eva_queue`. Calling the evaluator directly on such a packet is refused with an event. *(OQ1, OQ3)*
- **AC21.** `gen_position` and `relational_score_e4` are identical on SQLite and PostgreSQL for a 20+-packet fixture with ties. Ties are broken by `created_at`, then `ref`. *(OQ2, OQ7)*
- **AC22.** A generation running under FakeProvider survives a project brief edit, a waiver, a scope `gate_revision` bump and an evaPacket on an unrelated packet. It reaches `gen_eva` and is neither staled nor cancelled.

**(d) Gates**
- **AC23.** `mandatory_fields_for_packet` returns the universal set, plus each relational set when its trigger holds (dependencies, dependents, chartedness, task family). There is one unit test per trigger. *(OQ10)*
- **AC24.** A packet missing any mandatory field never becomes `ready`. Filling it lets readiness admit it, subject to the rest of the predicate. *(OQ11)*
- **AC25.** `admit_next` promotes `ready → queued` when `relational_score_e4 ≥ floor` (tested below, at and above the floor). Below the floor the packet stays `ready`, and `v_gen_queue` shows its score and the floor. *(OQ6)*
- **AC26.** No code path parks a packet for its failure count. The failure-budget transition rows are gone from `transitions.py` and from the DB trigger, and the transition tests are regenerated.
- **AC27.** A recorded user feedback event changes `user_feedback_bonus_e4` as defined. *(OQ9)*

**(e) gen_eva and crashes**
- **AC28.** A run with gradable output triggers `gen_eva` in the same kernel step without reading `v_gen_queue`/`v_eva_queue`, even when the packet would rank last (asserted with a spy).
- **AC29.** `gen_eva` writes post-phase passes, `composite_grade_e4`, the `sealed_bundles` row and the parent's lock and terminal status. *(OQ13, OQ16)*
- **AC30.** Each seal creates exactly one child genPacket with:
  - `parent_id` = parent, `root_id` = parent's root (or the parent itself), `depth` + 1;
  - the parent's bundle id/hash;
  - a re-imagined `prompt_description` that differs from the parent's.
  This holds for a high-grade (≥ 9,500) parent and a low-grade parent alike. *(OQ17, OQ24)*
- **AC31.** Idempotence: a failure injected between seal and child spawn, followed by recovery, gives exactly one bundle and one child.
- **AC32.** The CFO's example as a test:
  - a poorly-graded, pivotal parent's child ranks above a well-graded, non-relational parent's child;
  - the former crosses the floor and becomes `queued`;
  - the latter stays `ready`.
  *(OQ6, OQ8)*
- **AC33.** Crash: a FakeProvider timeout or empty output increments `crash_count` and the route-health counter. It writes no grade, no `sealed_bundles` row and no child, and runs no `gen_eva`. *(OQ18)*
- **AC34.** `binding_mismatch` returns `None` after a `gate_revision` or `input_hash` change. It still reports a superseded fencing token, an expired lease and an owner close/BLOCK. *(OQ19)*
- **AC35.** One documented SQL query over `parent_id`/`root_id` returns, for a three-generation lineage, each ancestor's grade, bundle hash and crash count.

## Open questions for the CFO

The directive's code sketches are non-production references. Each gap or contradiction found in them is listed here; none has been silently decided. *Default* is the PM/engineer suggestion if the CFO prefers to delegate.

**Queue positions and lockout**
- **OQ1 — Which statuses are "running" at position 0?** Sketch 1 gives position 0 only to `status = 'running'`. A generation is also in flight during `evaluating_post` (`gen_eva` itself) and while an exit-3 run is parked in `awaiting_confirmation`.
  - Several packets can run at once, and all would share position 0.
  - *Default:* position 0 = every packet with an active run (`active_run_id IS NOT NULL`) or in `evaluating_post`.
- **OQ2 — What do positions count?**
  - Sketch 1's `ROW_NUMBER()` is computed over all rows, including the running ones. Running rows therefore consume row numbers, so the next packets can start at 2 or higher, and "positions 1 and 2" are not always the next two to dispatch.
  - `v_gen_queue` also includes `held`, `blocked`, `needs_review`, `awaiting_confirmation` and `failed` packets (rank group 2). When few packets are queued, non-dispatchable packets can occupy positions 1–2 and be locked out of enrichment for no reason.
  - *Default:* number only `queued` then `ready` packets, after the running ones.
- **OQ3 — What is "directly related" for the lockout?** Sketch 1 locks out dependency edges in both directions plus the packet's `parent_id`.
  - The parent of a re-imagined child is sealed, and so already outside genQueue. The parent link then only matters for split parents in `waiting_for_children`, which are also outside.
  - Should "directly related" also cover split siblings, the same lineage (`root_id`), packets writing the same `repo_target_path`, or the packet's objective?
  - Does the lockout also cover the CFO's own SQL edits, or only evaPackets?

**Scoring units and bounds**
- **OQ4 — Units of `question_context` weighting.**
  - C.i.2 says `question_context_count` weights `direct` links above `peripheral`, but gives no weights.
  - Sketch 1 adds `question_context_count × 1000` to the score. Is the count per field, per packet (summed over fields), or per changed link?
  - What are the direct/peripheral weights, and is the count capped?
- **OQ5 — Direction of `question_context` in the score.** Sketch 1 **raises** the rank of a packet whose context is questioned (+1,000 each). That pushes it towards positions 0–2, where the lockout stops the evaPackets that would refresh it. It could then be generated on questioned context. Should `question_context` raise evaQueue priority but lower genQueue rank, or block admission until it is refreshed?
- **OQ6 — The 3,500 admission floor and root packets.**
  - For a root packet (no parent bundle) Sketch 1 uses `parent_grade_e4 = 0`, so the score is `3000 + 7000·c/10000`.
  - Any root with centrality below 715/10,000 scores under 3,500 and **never becomes `Queued`**. So an isolated first-generation task would never run.
  - Is 3,500 the intended floor? Should roots use a neutral grade instead? Is the floor versioned config?
- **OQ7 — Score bounds and which formula.**
  - `relational_score_e4` is not bounded to 10,000: `question_context` and feedback add on top. Clamp it?
  - Does Q1 step 3's `S = (V·(D − R)) // (D + C)` survive at all, for example as the order within `queued`?
  - Does T1-C v2's failure product `A` survive as a lineage-level input?
  - Sketch 1 also joins `sealed_bundles` on `packet_id = p.parent_id`. That needs exactly one bundle per packet, which v3's `UNIQUE(packet_id)` gives.
- **OQ8 — Definition of `relational_centrality_e4`.** `v_packet_relational_centrality` is not defined. Candidate inputs (Q1 step 2):
  - number of dependents unblocked;
  - critical-path position;
  - objective health;
  - chartedness;
  - task family.
  Which ones, with what weights, and normalised how?
- **OQ9 — Definition of `user_feedback_bonus_e4`.** `v_packet_user_feedback` is not defined. Questions:
  - Which events count: confirmations, waivers, SQL edits, overrides, grade adjustments?
  - Which sign does each carry? For example, is a waiver positive, and is an override down-weight negative?
  - What are the magnitudes, and does feedback decay?
  - Does it apply per packet or across the lineage?

**Ready gate**
- **OQ10 — Mandatory field names.**
  - Sketch 3 names `clarity`, `specificity`, `scope_fit`, `dependency_clarity`, `interface_stability`, `downstream_contract_clarity`, `blast_radius`, `regression_risk` and `sonar_Hotspot_alignment`. None of these is in `fields/candidate_fields.yaml` except `testability`.
  - Map them to existing fields (for example `dependency_validity`, `acceptance_criteria_coverage`, `requirement_alignment`), or add new definitions?
  - Q3(iv) names "task family" as a trigger, but Sketch 3 has no rule for it.
  - `is_charted_target` does not exist yet.
  - `locally_drafted_examples` is not a field yet.
- **OQ11 — Relation to today's readiness predicate.**
  - Today `evaluating_pre → ready` requires: required tiers accepted or soft-waived, no hard failure, dependencies satisfied, approvals, ambiguity ≤ 4,000, and H current.
  - Do universal/relational mandatory sets **replace** `eva_presets.required` or **add** to it? Do the other conditions stay?
- **OQ12 — Can enrichment demote?** If continuous enrichment of a `ready`/`queued` packet produces a disputed or hard-failed mandatory field, does the packet go back to `evaluating_pre` (existing rows), or stay?

**gen_eva, sealing and children**
- **OQ13 — What status does a sealed parent get, and what satisfies a dependency?**
  - Sketch 4 transitions every graded parent to `completed`. Today `completed` means "passed the E5 completion gate" and satisfies dependency edges.
  - A poorly graded parent marked `completed` would unblock its dependents on bad output.
  - Options:
    1. a new terminal status (for example `sealed`);
    2. `completed` only above a grade threshold;
    3. dependencies follow the lineage to the newest/best sealed member.
  - Which member of a lineage satisfies an edge that points at the root?
- **OQ14 — When does a lineage stop?**
  - "Regardless of the grade … returns as a child" means every lineage grows forever.
  - Well-graded children sit at the bottom of genQueue but stay in evaQueue (missing fields), consuming local evaluation capacity indefinitely.
  - Is there a stop rule? Options: grade ≥ threshold → the task is done and no child is spawned; or the child is created but parked.
- **OQ15 — Production repo write vs `gen_eva`.**
  - Today `running → evaluating_post` requires the write and commit to the production repo **before** post-evaluation, and the post fields bind to the commit SHA (SonarQube).
  - Does `gen_eva` grade first, with output held in `content_blobs`/provisional refs, so that only passing output is committed? Or is every output committed and then graded?
- **OQ16 — `gen_eva` grade and resources.**
  - Which post-phase fields make up `composite_grade_e4`, and with which weights?
  - "No queue maths" for `gen_eva`: does it pre-empt evaQueue work on the shared local models?
- **OQ17 — Who writes the re-imagined prompt?** Sketch 4's `build_reimagined_prompt` is undefined.
  - If it is a deterministic template, the CFO should approve it.
  - If it is an LLM, say which model or channel. Its output must pass Pydantic validation and stay stateless (Golden Rules 2–3).
- **OQ24 — What the child inherits.**
  - Sketch 4 sets `position = parent.position + 1`, which can collide with siblings in the objective's order, and the `(re-imagined)` suffix accumulates in `title`.
  - Do dependency edges to and from the parent move to the child?
  - Which evaluated fields carry over: none, the structural ones, or all, marked with `question_context`?
- **OQ27 — A new child that already satisfies the mandatory fields.** Sketch 4 moves it `draft → ready`, but D8 has no `draft → ready` row; only `draft → evaluating_pre` exists. Add the row, or route the child `draft → evaluating_pre → ready`?

**Crashes and running generations**
- **OQ18 — After a crash.**
  - Which status does the unsealed packet take: back to `queued`, `ready`, or `failed`?
  - Is it re-dispatched automatically? The CFO–Argon note (A6) says generation runs are "never re-run automatically".
  - With `enforce_failure_budgets` gone, what bounds repeated crashes: only budget and pace caps (C21/C36), or route health?
  - How is "route health" defined, and what does a nudge change?
- **OQ19 — What still stops a run?**
  - Sketch 5 halts only on `closed_unverified` and `blocked`. Does an owner HOLD or a global pause still stop an in-flight run?
  - If runs no longer bind to `gate_revision`, the exit-3 approval/rejection guards (R1, D10 Q5) need another binding. Proposed: the confirmation's version and F.
- **OQ20 — The CFO's SQL edit of a running genPacket.** The run is not cancelled (Q7). Does the edit belong to the parent's sealed snapshot (head at seal time), or is it carried into the child, with the parent's snapshot taken from the run's frozen claim snapshot?

**Schema scope and wording**
- **OQ21 — Scope of retiring `supersedes_id`.**
  - Retire it for genPackets only?
  - Projects and objectives version via `supersedes_id` today, and their head changes are what `question_context` detects. Do they keep that, or move to the same in-place + history model?
  - What happens to C-loop interruption, which today "produces new core-field versions"?
- **OQ22 — Attributing a verified quote to its source.**
  - `verify_quote` checks a substring of one concatenated document (project brief + objective brief + task). Quotes carry no `source_type`/`source_ref`, and a quote may match in several sections.
  - Dependencies are not in the document at all, so Sketch 2 marks them `direct` structurally. That conflicts with "structural tagging alone is not sufficient".
  - *Default:* attribute by section offsets (`PROJECT BRIEF` / `OBJECTIVE BRIEF` / `TASK`). A quote matching several sections marks all of them `direct`. Hard dependencies stay structurally `direct`.
- **OQ23 — What else is in `input_hash`?** T1-C v2's manifest also covered field-definition, preset and contract versions and the repo commit. After a preset or definition change, are fields hard-invalidated (as today), or flagged with `question_context`?
- **OQ25 — Starvation and oscillation rules.** Q1 step 3 had:
  - every m-th dispatch for the oldest eligible packet;
  - an incumbency margin;
  - epoch-frozen inputs.
  Do they stay, for dispatch order within `queued`, given that `Queued` is "purely relational"?
- **OQ26 — Requirement F and CLAUDE.md Golden Rule 1.** Approve the reworded Requirement F (T1-C v3 §1)? Golden Rule 1 still says "Corrections create a new version (`supersedes_id`)".
- **OQ28 — M3a's boundary.**
  - Directive D.7 calls M3a "kernel loop + CLI".
  - The CFO–Argon note B2 calls it "the genPacket loop and pause/branch working in SQL".
  - Step 3 lists only (a)–(e).
  - Confirm that the kernel tick loop, the CLI and pause/branch belong to a later M3a part or to M3b, and not to this spec.

## Deviations from the directive

None yet; nothing is built. The directive's file/line references (C.i) predate PR #3's final tree, and the table above uses the current approximate locations.
