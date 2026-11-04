# D2 — Consensus and readiness truth table (Gemini Argon draft, PM-ruled) — M2 test fixtures

Produced by Gemini (Argon) from `docs/milestones/M2_evaluation_engine.md` (B2, B3, B9). The PM spot-checked the arithmetic (TC-03, 07, 09, 10, 30 correct). **The engineer must re-derive every expected value from the rulings below in code, not copy them blindly.**

Notation: scores are `(A on b, B on a, A on a, B on b)` = (P3, P4, P5, P6). `conf(A) = (0.6·P4 + 0.4·P5)/10`; `conf(B) = (0.6·P3 + 0.4·P6)/10`. Text fields: `conf(A) = P4/10`, `conf(B) = P3/10`. Evidence is given as verified SUPPORT / CONTRADICTION.

## PM rulings on the ambiguities Gemini exposed (binding for M2 — amendment B10)
1. **Agreement confidence:** each candidate's confidence is as defined above; on agreement the field's confidence is `mean(conf(A), conf(B))`.
2. **NULL candidate:** passes that would evaluate a NULL value are *skipped*. A cycle is complete when every applicable pass is done (e.g. P1, P2, P4 and P5 when `value_b` is NULL). One-sided confidence = the non-null candidate's confidence.
3. **Evidence scoping:** SUPPORT and CONTRADICTION are counted only from passes evaluating the **winning** value (P4 + P5 for `value_a`; P3 + P6 for `value_b`; P3–P6 on agreement). Contradictions against the loser don't taint the winner (TC-15 → accepted).
4. **Tag sets:** the evaluation-pass contract is a map keyed by tag over the union `value_a ∪ value_b` (`{tag: {accuracy_score, evidence, reason}}`). `v_field_consensus` emits one row per (packet, field, tag). A *required* tier-1/2 tag set needs zero disputed tags (unless waived). A tag absent from both models counts as null.
5. **Reported confidence:** a disputed row shows `max(conf(A), conf(B))`; a null row shows `0.00`; a pending row shows NULL.
6. **Scores are integers 0–10 (D4).** Cases that use fractional scores (TC-02, 09, 12, 18, 25, 30, 32) must be rewritten with integer scores reaching the same boundary, or replaced by a direct unit test of the comparison function. Compare at `ROUND(conf, 4)`, display at 2 dp, identically on both dialects.
7. **Version binding:** consensus binds strictly to `evaluation_cycles.packet_version = packets.version`. An incomplete cycle on the current version gives `pending`, even if an older version has a complete cycle (TC-38).
8. **Tier-3 fields never gate readiness** (TC-51). Post-phase evaPackets must accept the output before `completed`/lock (TC-52).

## Cases (52)
| id | kind | a | b | P3,P4,P5,P6 | ev S/C | cycle | waiver/override | status | final | conf | readiness effect | rule |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| TC-01 | score | 8 | 8 | 6,6,6,6 | 2/0 | 6/6 | – | accepted | 8 | 0.60 | pass if threshold ≤ 8 | agreement at exactly 0.60 |
| TC-02 | score | 8 | 8 | just under 0.60 | 2/0 | 6/6 | – | disputed | – | 0.59 | blocks (needs_review) | agreement below 0.60 (integer rewrite) |
| TC-03 | bool | true | true | 7,5,6,6 | 1/0 | 6/6 | – | accepted | true | 0.60 | pass | asymmetric cross scores averaging 0.60 |
| TC-04 | score | 7 | 7 | 9,9,9,9 | 0/0 (2 unverified) | 6/6 | – | disputed | – | 0.90 | blocks | no verified SUPPORT → no acceptance |
| TC-05 | score | 7 | 7 | 8,8,8,8 | 2/2 | 6/6 | – | accepted | 7 | 0.80 | pass | C = S is not C > S |
| TC-06 | score | 7 | 7 | 8,8,8,8 | 2/3 | 6/6 | – | disputed | – | 0.80 | blocks | C > S |
| TC-07 | score | 8 | 5 | 5.5→int,7,7,5.5→int | 2/0 | 6/6 | – | accepted | 8 | 0.70 | pass | winner exactly 0.70, margin exactly 0.15 (integer rewrite) |
| TC-08 | score | 8 | 5 | 8,5,5,8 | 2/0 | 6/6 | – | accepted | 5 | 0.80 | pass | B wins, margin 0.30 |
| TC-09 | score | 8 | 5 | winner just under 0.70 | 2/0 | 6/6 | – | disputed | – | 0.69 | blocks | winner < 0.70 |
| TC-10 | score | 8 | 5 | 6,7,7,5 | 2/0 | 6/6 | – | disputed | – | 0.70 | blocks | margin 0.14 < 0.15 |
| TC-11 | score | 8 | 5 | 8,8,8,8 | 2/0 | 6/6 | – | disputed | – | 0.80 | blocks | exact tie |
| TC-12 | bool | true | false | narrow margin | 2/0 | 6/6 | – | disputed | – | max | blocks | margin < 0.15 (Gemini self-corrected; re-derive) |
| TC-13 | score | 9 | 4 | 5,8,8,5 | winner 0/0 | 6/6 | – | disputed | – | 0.80 | blocks | winner lacks verified SUPPORT |
| TC-14 | score | 9 | 4 | 5,8,8,5 | winner 1/2 | 6/6 | – | disputed | – | 0.80 | blocks | winner C > S |
| TC-15 | score | 9 | 4 | 5,8,8,5 | winner 1/0, loser 0/2 | 6/6 | – | accepted | 9 | 0.80 | pass | loser's contradictions ignored (ruling 3) |
| TC-16 | score | 6 | NULL | –,7,7,– | 1/0 | complete (4 applicable) | – | accepted | 6 | 0.70 | pass | one-sided NULL at 0.70 |
| TC-17 | score | NULL | 6 | 7,–,–,7 | 1/0 | complete | – | accepted | 6 | 0.70 | pass | mirror of TC-16 |
| TC-18 | score | 6 | NULL | just under 0.70 | 1/0 | complete | – | disputed | – | 0.69 | blocks | one-sided below 0.70 |
| TC-19 | score | 6 | NULL | –,8,8,– | 0/0 | complete | – | disputed | – | 0.80 | blocks | one-sided, no SUPPORT |
| TC-20 | score | 6 | NULL | –,8,8,– | 1/2 | complete | – | disputed | – | 0.80 | blocks | one-sided C > S |
| TC-21 | score | NULL | NULL | – | 0/0 | complete | – | null | – | 0.00 | blocks if required tier 1/2 | both NULL |
| TC-22 | bool | NULL | NULL | – | 0/2 | complete | – | null | – | 0.00 | pass if tier 3 | both NULL ignores quotes |
| TC-23 | tagset t1 | t1 | t1 | 6,6,6,6 | 1/0 | complete | – | accepted | [t1] | 0.60 | pass | per-tag agreement at 0.60 |
| TC-24 | tagset t1 | t1 | – | –,7,7,– | 1/0 | complete | – | accepted | [t1] | 0.70 | pass | one-sided tag at 0.70 |
| TC-25 | tagset t1 | t1 | – | under 0.70 | 1/0 | complete | – | disputed | [] | 0.65 | blocks | one-sided tag below 0.70 |
| TC-26 | tagset t1,t2 | t1,t2 | t1 | t1 8s; t2 –,7.5→int,… | 1/0 each | complete | – | accepted | [t1,t2] | t1 0.80, t2 ≥0.70 | pass | agreed + one-sided |
| TC-27 | tagset t1,t2 | t1,t2 | t1 | t1 8s; t2 –,6,6,– | 1/0 each | complete | – | disputed (t2) | [t1] | t2 0.60 | blocks if required (ruling 4) | partial tag dispute |
| TC-28 | tagset t1 | t1 | t1 | 8,8,8,8 | 1/2 | complete | – | disputed | [] | 0.80 | blocks | per-tag C > S |
| TC-29 | text | "alpha" | "alpha" | 6,6 | 1/0 | 4/4 | – | accepted | "alpha" | 0.60 | pass | text 4-pass agreement 0.60 |
| TC-30 | text | "alpha" | "alpha" | just under | 1/0 | 4/4 | – | disputed | – | 0.59 | blocks | text below 0.60 |
| TC-31 | text | "alpha" | "beta" | 5.5→int,7 | 2/0 | 4/4 | – | accepted | "alpha" | 0.70 | pass | text margin exactly 0.15 |
| TC-32 | text | "alpha" | "beta" | margin 0.14 | 2/0 | 4/4 | – | disputed | – | 0.70 | blocks | text margin < 0.15 |
| TC-33 | text | "alpha" | NULL | –,7 | 1/0 | complete | – | accepted | "alpha" | 0.70 | pass | text one-sided |
| TC-34 | text | "alpha" | "beta" | 8,8 | 2/0 | 4/4 | – | disputed | – | 0.80 | blocks | text tie |
| TC-35 | score | 8 | – | – | – | 2/6 | – | pending | – | NULL | blocks (evaluating_pre) | incomplete during extraction |
| TC-36 | score | 8 | 8 | P6 missing | – | 5/6 | – | pending | – | NULL | blocks | incomplete cycle |
| TC-37 | text | "alpha" | "alpha" | P4 missing | – | 3/4 | – | pending | – | NULL | blocks | incomplete text cycle |
| TC-38 | score | v2 in flight | – | – | – | v2 1/6 (v1 complete) | – | pending | – | NULL | blocks v2 | version binding (ruling 7) |
| TC-39 | score | 5 | 5 | 8s | 2/0 | complete | – | accepted | 5 | 0.80 | blocks: below the field's readiness threshold (7) | value threshold |
| TC-40 | score | 5 | 5 | 8s | 2/0 | complete | valid soft waiver | accepted | 5 | 0.80 | pass → ready | waiver on below-threshold soft field |
| TC-41 | score | 8 | 4 | 6s | 2/0 | complete | valid soft waiver | disputed | – | 0.60 | pass → ready | waiver on disputed soft field |
| TC-42 | score | NULL | NULL | – | 0/0 | complete | valid soft waiver | null | – | 0.00 | pass → ready | waiver on null soft field |
| TC-43 | score | 5 | 5 | 8s | 2/0 | v2 complete | waiver bound to v1 | accepted | 5 | 0.80 | blocks | waiver lapses on edit |
| TC-44 | score | 5 | 5 | 8s | 2/0 | complete | waiver revoked | accepted | 5 | 0.80 | blocks | revocation event |
| TC-45 | bool hard | false | false | 9s | 2/0 | complete | waiver attempted | accepted | false | 0.90 | blocks (hard) | no waiver of hard gates |
| TC-46 | bool hard | false | false | 9s | 2/0 | complete | FORCE | accepted | false | 0.90 | cannot claim | FORCE ≠ bypass |
| TC-47 | score soft T1 | 8 | 4 | 6s | 2/0 | complete | FORCE, no waiver | disputed | – | 0.60 | blocks | FORCE ≠ waiver |
| TC-48 | score T1 | 8 | 8 | 8s | 2/0 | complete | unmet typed dependency | accepted | 8 | 0.80 | blocks | dependency edge |
| TC-49 | score T1 | 8 | 8 | 8s | 2/0 | complete | approval pending | accepted | 8 | 0.80 | blocks (awaiting_confirmation) | approvals |
| TC-50 | score T1 | 8 | 8 | 8s | 2/0 | complete | HOLD or BLOCK | accepted | 8 | 0.80 | blocks admission | override precedence |
| TC-51 | score T3 | 8 | 3 | 6s | 2/0 | complete | – | disputed | – | 0.60 | pass | tier 3 doesn't gate |
| TC-52 | score post | 8 | 4 | 6s | 2/0 | complete (post) | – | disputed | – | 0.60 | blocks completion/lock | post-evaluation gate |
