# Design brief D1 — genPackets and evaPackets

**Context:** Python Orchestrator (`redtongues/PythonPlanner`). This is a personal autonomous dev stack: a Python kernel with PostgreSQL (SQLite for tests), small local LLMs for evaluation, Pydantic validation and deterministic SQL/Python decisions. Key Idea: advance project work unattended (K1); everything governed, scored and evidenced (K2); LLMs propose, SQL/Python decide (K3); readable (K4); completed work is immutable (K5). Deliberate pace is preferred over throughput.

**CFO clarification (2026-11-04), in his words, condensed:**
- **genPackets** are the generative prompt packets — the real work items (Project > Objective > Task). They are stored in Postgres and carry core fields plus *evaluation fields* (tags and scores, e.g. coding intensity, ambiguity, tools needed, artifact class).
- **evaPackets** are sub-packets: *preset* evaluative prompts aimed at tiny local models, running simple **stateless** scoring and tagging. Each fills an evaluation field of a genPacket. The six-pass dual-model scheme applies: A/B extract, then cross- and self-evaluation.
- **Python and Pydantic sit in the middle.** Deterministic rules manage a queue of genPackets by having evaPackets fill their evaluation fields. Storage and delivery are entirely deterministic: prompts are just stored in SQL, while Python algorithms continuously compute, enhancing stored genPackets via evaPackets until the algorithm decides a genPacket is **ready** to be added to the **generation queue**.
- Every generative prompt is executed backed by its set of tags and scores.
- Evaluating generated output is also done by evaPackets aimed at the output, never by new genPackets.
- **Generation queue order is algorithmic, but heavily influenced and even overridden by human-led input.**

**Existing state:** M1 has a single `packets` table (`packet_kind` task | content_eval) and an append-only `evaluation_passes` table; the M2 spec plans evaluation cycles (`eval_cycle_id`), a consensus decision table, priority and branching.

## Your task
Propose a concrete design covering:
1. **Data model.** Tables and key columns for genPackets, evaPackets and their results. How evaPackets relate to evaluation cycles, genPacket versions and the append-only history. Whether evaPackets are rows in their own table or a packet kind, and why.
2. **Lifecycles.** State machines for a genPacket (draft → … → completed/locked) and an evaPacket (planned → … → done/failed), with allowed transitions.
3. **Readiness algorithm.** A deterministic rule (SQL view or Python) that decides when a genPacket enters the generation queue: required fields, confidence thresholds, disputes, dependencies, approvals, staleness when a genPacket is edited.
4. **Generation-queue ordering with human override.** Algorithmic rank plus human controls (pin, reorder, hold, force, block). State precedence rules, how overrides are recorded and expire, and how starvation is avoided.
5. **evaPacket scheduling at deliberate pace.** Which evaPackets to create first (key fields first, lazy re-evaluation on input-hash change), batching by model, budgets.
6. **The three biggest risks in the design**, and how to test it.

Reply in markdown with those six headings. Keep it concrete — table and column names, states, SQL or pseudo-code where useful — and under about 900 words.
