# D5 — evaPacket contracts and consensus oracle (GPT-6.1 draft, 2026-11-04) — notes for the engineer

Microsoft Copilot (GPT-6.1) drafted Pydantic v2 contracts, an Ollama JSON-schema converter and a pure consensus function implementing D2 rulings 1–7. It reports 28 passing tests (Pydantic 2.12.5). The code stays in that chat; the engineer implements M2 independently. Its findings are binding clarifications to B10:

1. **Integer boundaries.** With integer 0–10 scores and the 0.6/0.4 weights, an exact 0.15 disagreement margin is unreachable (e.g. TC-07 becomes 0.70 vs 0.54, margin 0.16; TC-31 becomes 0.70 vs 0.50). Test the boundary *directly on the comparison function* (margin `>= 0.15` at `ROUND(…, 4)`), and keep fixtures realistic rather than impossible.
2. **Tag maps.** Keep every key in the union of tags; skipped (inapplicable) entries are JSON `null`. Vocabulary tags absent from both candidates get `null` consensus rows.
3. **Ties.** No winner means an empty winning-evidence scope. Unverified-quote counts are also scoped to the selected value.
4. **Schema conversion.** Use a conservative JSON-Schema subset for Ollama; constraints that are removed stay enforced by Pydantic. Integer bounds can be expressed as enums. Native Ollama uses `format: <schema>`; the OpenAI-compatible endpoint uses `response_format`.
5. **The pure function is the reference oracle.** The SQL views must reproduce it; parity tests compare the two on both dialects.
