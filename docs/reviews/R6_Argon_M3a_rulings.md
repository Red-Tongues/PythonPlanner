# R6: Argon binding rulings on M3a OQ1-OQ35

On 2026-11-06 00:02 the CFO delegated binding decisions on M3a open questions OQ1-OQ35 to the auditor Argon (Gemini), to be implemented without further CFO sign-off. The PM verified the rulings and Argon corrected OQ7 (grade sign and centrality scale), OQ32 (trigger ordering) and restated OQ24; the corrected versions below are final.

| OQ | Ruling |
|---|---|
| OQ1 | Position 0 = status `running` only; all running packets share 0; `evaluating_post` and parked/held packets are not numbered. |
| OQ2 | `v_gen_queue` = status in (`running`, `queued`, `ready`) AND not held AND dependencies satisfied AND route healthy; running = 0; `ROW_NUMBER` from 1 over queued then ready, ordered by queued first, `score_e4 DESC`, `ready_since ASC`, `packet_id ASC`. |
| OQ3 | Lockout set = positions 0-2 plus packets sharing their `root_id` (lineage), hard dependencies in either direction, and the same non-null repo path; applies only to evaQueue claim/commit; human/CFO SQL edits are never locked out. |
| OQ4 | A field is questioned if any of its links is; field weight = 750 if any direct link is questioned, else 250 if peripheral; packet `question_score_e4 = min(2250, sum of field weights)`; settings `w_q_direct_e4` 750, `w_q_periph_e4` 250, `cap_q_e4` 2250. |
| OQ5 | `question_score` is SUBTRACTED in genQueue and ADDED x2 in evaQueue priority; an evaluation completed at the current `content_revision` clears `is_questioned` on its links. |
| OQ6 | Root `effective_parent_grade_e4 = 5000`; `ageing_e4 = min(1500, floor(wait_sec/60)*10)`; an isolated root (centrality 0) reaches the 3500 floor after 50 minutes. |
| OQ7 | **FINAL:** `score_e4 = round(0.6*(10000 - eff_parent_grade_e4) + 0.7*centrality_e4) - question_score_e4 + feedback_e4 + ageing_e4`; floor 3500, lowered to 2500 when queued depth < 3, bypassed at wait >= 7200 s; tie-break `score_e4 DESC`, `ready_since ASC`, `packet_id ASC`; T1-C v2 failure product retired; all constants versioned settings. |
| OQ8 | **FINAL:** `centrality_e4 = min(10000, 3000*unsealed_direct_dependents + 1500*unsealed_2hop_dependents + 2500*is_critical_objective_path)`, counting only dependents whose lineage has no sealed member with grade >= 7000. |
| OQ9 | `feedback_events`: BOOST +1500, NUDGE +500, DEMOTE -1500, PIN +3000; latest per (packet, actor) wins; decay x1 up to 24h, x0.5 24-72h, 0 after 72h; packet sum clamped to [-3000, +3000]. |
| OQ10 | Map clarity+specificity to `requirement_alignment`, `scope_fit` to `acceptance_criteria_coverage`, `dependency_clarity` to `dependency_validity`, blast_radius+regression_risk to `risk_assessment`; keep `testability`. Add to catalogue `risk_assessment` (if absent), `interface_stability`, `downstream_contract_clarity`, `sonar_hotspot_alignment`; task family falls back to `eva_presets.required`; `is_charted_target` = repo path not null; `locally_drafted_examples` = example blob present. |
| OQ11 | Mandatory = universal union relational union `eva_presets.required`; Ready also keeps no hard failure on mandatory fields, hard dependencies satisfied, approvals granted, ambiguity <= 4000; the hash-current check is removed. |
| OQ12 | Enrichment never demotes to `evaluating_pre`; `queued` -> `ready` only by score falling below the floor (not at position 0). |
| OQ13 | New locked terminal status `sealed` (`evaluating_post` -> `sealed`) under the immutability trigger; dependency edges re-pointed to the target's `root_id`; an edge is satisfied when any lineage member is sealed with `composite_grade_e4 >= 7000` (setting `dep_satisfaction_grade_e4`). |
| OQ14 | Always spawn the child, but create it held (`hold_reason` `grade_met`) when parent `composite_grade_e4 >= 8500` (setting `lineage_stop_grade_e4`); un-holding is a human action. |
| OQ15 | Output committed to `refs/gen/<run_id>` before `gen_eva` (real SHA for post-evaluation/SonarQube); target branch fast-forwarded only when grade >= 7000 and above any prior sibling. |
| OQ16 | `composite_grade_e4 = 0.30 requirement_alignment + 0.25 testability + 0.20 acceptance_criteria_coverage + 0.15 risk_assessment + 0.10 sonar_hotspot_alignment` (if sonar N/A, +0.05 each to testability and risk_assessment); `gen_eva` is claimed before evaQueue work but never interrupts an in-flight call. |
| OQ17 | Deterministic template (objective + parent blob ref + top-3 failed critiques + verified contradiction quotes); if grade < 5000 a stateless Pydantic-validated `fast_synth` LLM rewrite, falling back to the template on failure. |
| OQ18 | Crash: run -> `crashed`, `crash_count+1`, packet `running` -> `queued` with cooldown `min(3600, 30*2^(crash_count-1))` s; hold at `crash_count` 5 (`crash_limit`); route unhealthy 600 s after 3 crashes within 300 s; consistent with A6 because no gradable output exists; these are the only automatic bounds. |
| OQ19 | Hard CFO_ABORT / GLOBAL_PAUSE revoke the lease (`running` -> `queued`); soft HOLD sets held and does not stop the run; exit-3 guards bind `confirmation_version` + fencing token, not gate/content revision. |
| OQ20 | `runs.claim_content_revision` recorded at dispatch; the seal reconstructs that revision from `field_revisions`; edits during the run carry to the child. |
| OQ21 | Retire `supersedes_id` for genPackets, projects and objectives (in-place + history); loop interruptions become run events + child lineage. |
| OQ22 | Section-offset attribution; a quote spanning several sections marks each direct (`direct_quote`); hard dependencies are direct (`hard_dep`); ambient is peripheral. |
| OQ23 | Preset/field-definition/contract version bumps flip `question_context` (direct), never hard-invalidate. |
| OQ24 | **FINAL child:** `parent_id`, `root_id = coalesce(parent.root_id, parent.id)`, `lineage_depth+1`; position copied, uniqueness scoped (`objective_id`, `position`, `lineage_depth`); title stored without suffix, display derived; edges not rewritten (they target `root_id`); all latest pre-eval field results copied with scores so mandatory presence holds, each carried field marked questioned (direct) so evaQueue re-evaluates. |
| OQ25 | Keep every 5th dispatch takes oldest queued, incumbency margin 250, dispatch reads the tick snapshot. |
| OQ26 | Reword Requirement F and CLAUDE.md Golden Rule 1 to: developing packets and upstream context mutate in place with trigger-captured `field_revisions`; completed runs seal their claim-time snapshot into `sealed_bundles` and spawn a child via `parent_id`/`root_id`. |
| OQ27 | Add `draft` -> `ready` for children whose carried fields satisfy mandatory sets. |
| OQ28 | M3a = schema, triggers, views, `gen_eva` and admission-tick functions, parity tests; daemon, CLI and pause/branch are M3b. |
| OQ29 | Human edits at positions 0-2 allowed, they bump `content_revision` and flow to the child; only automated evaQueue writes are locked out. |
| OQ30 | In-flight evaluations finish; commit checks the lockout snapshot and content revision in one CAS, discarding into append-only `eva_discard_log` on conflict. |
| OQ31 | `queue_epoch_snapshot(epoch_id, packet_id, queue_pos, is_locked_out, score_e4)` written each admission tick and on dispatch/completion; lockout and dispatch read it, never live `ROW_NUMBER`. |
| OQ32 | **FINAL:** transition/immutability guard is a `BEFORE UPDATE` (and `BEFORE DELETE`) trigger, outbox capture an `AFTER UPDATE` trigger, so a rejected transition never writes outbox rows on either dialect; parity test asserts zero outbox rows and unchanged `content_revision` on rejection. |
| OQ33 | `gate_revision` bumps only on status, held, approvals, route; `content_revision` bumps only in the outbox path; governance CAS uses `gate_revision`, evaluation CAS `content_revision`. |
| OQ34 | The drain re-runs `verify_quote` for links to an edited source and flips broken ones to questioned (direct). |
| OQ35 | Synchronous in-transaction drain into `field_revisions`, `content_blobs` (sha256) and link flags. |

## Transitions

Add: `evaluating_post` -> `sealed`, `draft` -> `ready`, `queued` -> `ready`, `running` -> `queued`.

Remove: `evaluating_post` -> `completed` (genPackets), `ready` -> `evaluating_pre`, `queued` -> `evaluating_pre`, `running` -> `failed`.

## Flags for CFO (not adopted)

- F1: Skip child creation entirely when grade >= 8500.
- F2: A verified CONTRADICTION on a direct mandatory field blocks `ready` -> `queued` until one evaQueue pass.
