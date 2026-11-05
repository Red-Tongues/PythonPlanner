# G-audit of T1-C (Gemini Argon, 2026-11-05, 8-point brief)

**Verdict:** T1-C is **not yet ready** to be the M3a spec. It resolves A2, A3, A4, A12 and A26 conceptually; A1 and A10 only partly.
**PM verification:** G1's counterexample was re-run in Python. Order (1005, 1005, 1990) gives 4521 and (1005, 1990, 1005) gives 4519; the exact bigint form gives 4521. **Confirmed.**

| # | Sev | Finding | Fix |
|---|---|---|---|
| G1 | High | T1-C §3 says "(1+I)(1+c)−1 is commutative". That is false once each step is floor-truncated. | Exact bigint product, one final division: `A = min(10000, (∏(10000+c_k) − 10000^n) // 10000^(n−1))`. |
| G2 | **Critical** | Identity-only `packets` plus views breaks the CFO's direct SQL editing (Part A1). This is **unmarked [CFO]**. | See G12. |
| G3 | **Critical** | T1-C §1 conflicts with R4 MR1, whose CAS runs `UPDATE packets` on version, fence, run and lock columns. | See G12. |
| G4 | High | Project/objective revisions and dependency bundle hashes in the *binary* manifest contradict T1 round 3 (soft drift). This is **unmarked [CFO]**. | Binary invalidation only within a packet (prompt → pre-eval → output → post-eval) and on field-definition/contract versions. Everything upstream feeds `drift_risk`. |
| G5 | Medium | Unmarked [CFO] items:<br>• an unknown cause that restricts dispatch (conflicts with "queue maths is the only judge");<br>• categorical inheritance, which the CFO never approved;<br>• thinking downgraded below the CFO's "reassess quickly using the thinking". | Tag them and ask. Keep thinking available for diagnosis (redacted, never the sole evidence). |
| G6 | High | The validity view needs recursive manifest hashing on every SELECT, and SQLite has no `sha256()`. | Store the manifest hash when the revision is written. Check validity in Python/kernel code, not in a view. |
| G7 | Medium | Categorical inheritance makes the CFO a bottleneck. | Use T1 round 4's bounded numeric prior. |
| G8 | **Critical** | R4 groups 3–11 (Copilot agent) were never run on PostgreSQL. | PG suite green before merge. |
| G9 | High | Shadow mode with no failure governor allows an infinite failing loop. | Hard circuit breaker (e.g. N consecutive failures on the same prompt revision → fixed priority step down / needs_review) that stays active even in shadow mode. |
| G10 | High | A shared provisional branch becomes an unbuildable graveyard. | Per-attempt refs `refs/provisional/<packet>/<attempt>`, or SQL blobs. |
| G11 | Medium | Revisions grow without bound. | `content_blobs(hash, body)`, deduplicated. |
| G12 | High | Four new concepts are over-engineered for a single-user prototype. | **Mutable Head + Immutable Seal**:<br>• `packets` stays the live, editable head (R4 CAS and the CFO's edits work);<br>• an AFTER UPDATE trigger appends to `field_revisions`;<br>• completion writes an immutable `sealed_bundles` row;<br>• drop `lifecycle_episodes` (episode = bundle count + 1). |
| G13 | Medium | Reachability, API-change detection and outage suppression are speculative; no tool computes them yet. | Label them as future inputs, not mechanics. |

**Priorities:**
- **P0:** PG tests on R4 3–11; adopt G12.
- **P1:** G4, G1, G10.
- **P2:** G9, explicit [CFO] tags (G5/G7).

**Minimum before M3a:**
1. PostgreSQL green on R4.
2. One-page T1-C v2.
3. CFO sign-off.

**PM note on G12 vs Requirement F:** under G12, the *head row* stays editable after completion, and immutability moves to `sealed_bundles` and the append-only ledgers. That changes how Golden Rule 1 is applied ("never update locked packets"). It needs the CFO's explicit [CFO] approval, and CLAUDE.md must be updated if she agrees.
