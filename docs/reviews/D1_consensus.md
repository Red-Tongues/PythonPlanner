# D1 — consensus: genPackets and evaPackets (adopted 2026-11-04)

Process: CFO clarification → brief (`D1_brief_gen_eva_packets.md`) → three independent designs (`D1_designs.md`) → cross-evaluation by all three → PM synthesis. The designers were MC (Microsoft Copilot, GPT-6.1), GE (Gemini, Argon) and GH (GitHub Copilot).

| Overall scores | of MC's design | of GE's design | of GH's design |
|---|---|---|---|
| by MC | 7.8 | 7.3 | 8.0 |
| by GE | 8.5 | 8.8 | 8.0 |
| by GH | 8.7 | 7.5 | 7.2 |
| six-pass confidence (0.6 cross + 0.4 self) | **0.81** | **0.76** | **0.76** |

No single design wins outright. The final design takes MC's invariants and queue governance, GE's lifecycle and scheduling ideas, and GH's migration path.

## Resolved points of disagreement
1. **FORCE ≠ waiver.** FORCE, PIN and REORDER change *dispatch priority only* and never bypass readiness or hard constraints (all three agree).
   - Gemini warned that two small models can deadlock on a subjective field. So a separate human **WAIVE** decision may accept a disputed or low-confidence *soft* field for one specific genPacket version.
   - A waiver is recorded with actor, reason and the version it applies to, and lapses on edit.
   - Hard constraints — schema validity, dependencies, safety and policy — can never be waived.
2. **Evolve M1's `packets` table** into the genPacket table through additive **migration 0004** (unanimous). Identifiers and lineage are kept. A CHECK constraint stops new `packet_kind = 'content_eval'` rows; legacy rows stay as history.
3. **Granularity.** One evaPacket per (packet version × phase × field preset revision × target hash).
   - **Content addressing:** results are reusable only when the target hash, preset/schema/policy revision *and* model configuration all match (GE's memoisation, tightened by MC).
   - **Lightweight `evaluation_cycles` row:** groups the attempts behind one readiness decision, keeping B1's `eval_cycle_id` (MC, GH).
4. **Readiness:** a single pure Python predicate is authoritative, re-checked at queue admission **and** at atomic worker claim (prevents time-of-check/time-of-use races). `v_ready_candidates` is an explanatory/filter view, not a competing policy.
5. **Post-generation evaluation is explicit.** genPacket states:
   - `draft → evaluating_pre → ready → queued → running → evaluating_post → completed (locked)`;
   - side states `needs_review`, `held`, `blocked`, `failed`.
   
   Output-targeted evaPackets must accept the exact artifact hash before the packet locks (GE; adopted by MC).

## Also adopted
- **Execution snapshot (MC).** Each `generation_runs` row freezes the tags, scores, consensus IDs, waivers and override IDs it ran with. Later re-evaluation can never rewrite why past work ran.
- **Typed dependency edges (MC).** `packet_dependencies (packet_id, depends_on_id, kind, satisfied_when)`. Hierarchy (parent/child) is *not* an execution dependency; there is no blanket "parent locked" rule (rejects GE's gate and GH's "dependency ready" gate).
- **Version-bound overrides (GE).** `queue_overrides` and waivers carry `target_version`; FORCE, PIN, REORDER and WAIVE lapse on revision. BLOCK and HOLD declare their scope explicitly so a protective restriction never disappears by accident. Revocation is its own append-only event (MC).
- **Fairness (MC).** One normal-priority admission is reserved per N forced/pinned admissions; ageing is capped.
- **Tiered lazy gating (GE).**
  - Tier 1 (gating): ambiguity, safety, dependencies, approvals.
  - Tier 2 (routing): artifact class, tools, coding intensity.
  - Tier 3: optional fields.
  
  A tier-1 failure or dispute sends the packet to `needs_review` and spawns nothing further.
- **VRAM-aware batching (GE).** Run all model-A passes in a batch, then all model-B passes. Single-model concurrency by default (rejects MC's two concurrent calls on one GPU). Pace ceiling from the D14 governor.
- **Stale-write guard (GE).** Projection updates use `WHERE id = :id AND input_hash = :target_hash`. Obsolete in-flight evaPackets are marked `superseded` and never block the current version (GH's blocking rule rejected).
- **Grammar-constrained decoding where the provider supports it (GE)**, plus Pydantic; parser fuzz tests.

## Invariant (MC)
**No genPacket executes unless its exact version has a complete, current, validated and auditable evaluation set — or a recorded, version-bound human waiver for specific soft fields.**
