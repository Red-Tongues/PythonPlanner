# D6 — Decision D3: combined vs split evaluation passes (GPT-6.1 analysis, 2026-11-04) — for CFO decision

**Recommendation:** adopt **COMBINED** (6 calls per field: 2 extractions + 4 evaluations, each returning score + evidence + reason in one JSON object) as the canonical design, with a **bounded HYBRID escalation**.

1. **Approve COMBINED as canonical.** Correct the draft's inconsistent mock-ups and document SPLIT (10 calls per field: 2 extractions + 4 accuracy + 4 evidence prompts) as an explicit alternative. *Revisit* if testing shows the combined workload is a material source of errors.
2. **Validate meaning, not just structure.** Enforce schema, score bounds and exact substrings, and also check evidence relevance and contradictions. *Revisit* if evaluations that are syntactically valid and quote-valid still produce unacceptable false acceptances.
3. **Bounded HYBRID escalation.** Allow one targeted repair attempt, then escalate unresolved evaluations to a split re-run. Pre-route only justified field classes, e.g. tier-1 gating fields. *Revisit* routing if escalation becomes frequent enough that going straight to SPLIT would avoid wasted work.
4. **Keep the consensus formula** `(0.6 × cross + 0.4 × self)/10`, applied per candidate.
5. **Rationale:** score and evidence are parts of one judgement. Asking for them together yields one coherent, auditable record and a third fewer calls. Neither option's accuracy advantage can be known until the actual models are benchmarked.

**PM view:** agree. This matches the current default (B-series specs) and the deliberate-pace preference; hybrid escalation fits naturally into the evaPacket retry and `needs_review` flow. **CFO decision 2026-11-04: approved — combined by default, with bounded hybrid escalation.**
