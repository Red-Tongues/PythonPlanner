# D3 — Local models for evaPackets (draft by Microsoft Copilot GPT-6.1; verified and challenged by Gemini Argon, 2026-11-04)

**Question:** which small open-weight models should serve as evaluators A and B, plus an optional aggregator C, on one dedicated 16 GB or 24 GB GPU via Ollama? They must produce strict JSON (Pydantic-validated) for thousands of stateless evaPackets a day, at deliberate pace.

## Recommendation (starting hypotheses, not established winners)
- **16 GB:** Qwen3.5-4B (Q8_0) + Phi-4-mini-instruct (Q8_0); Granite-4.2-3B (Q8_0) is the first substitute.
- **24 GB:** Qwen3.5-4B (Q8_0) + Ministral 3 8B Instruct 2512 (Q8_0).
- Prefer a high-precision small pair over two aggressively 4-bit-quantised larger models. Choose the pair by **observed joint-error behaviour** on held-out ground truth, not by family diversity or advertised scores alone.

## Shortlist (claims to verify)
| Model | Params / licence | Start quant | Approx. working VRAM | Why |
|---|---|---|---|---|
| Qwen3.5-4B | ~4.2B language + 334M projector; Apache-2.0 | Q8_0 | 5–7 GiB | First general-purpose candidate for A; hybrid architecture needs a current runtime |
| Phi-4-mini-instruct | 3.8B; MIT | Q8_0 (Q6_K fallback) | 4.5–6 GiB | Different lineage; strong on explicit rubrics |
| Granite-4.2-3B | ~3.7B stored; Apache-2.0; released Aug 2026 | Q8_0 | 4.5–6 GiB | Classification and structured tasks; check chat template / thinking control |
| Ministral 3 8B Instruct 2512 | 8.4B language + 0.4B vision; Apache-2.0 | Q8_0 on 24 GB; Q4_K_M compact | 11–13 GiB (Q8), 7–9 GiB (Q4) | First larger B candidate |
| (two more in the source answer, incl. a Gemma 4 E4B variant — packaging in flux) | | | | |

## Residency (Ollama)
- `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`, explicit 2K–4K context, preload with `keep_alive=-1`; check `ollama ps` shows 100% GPU.
- Resident ≠ simultaneous: serialise GPU work through one orchestrator queue (matches D1's VRAM-aware batching).
- Aggregator C: deterministic aggregation on 16 GB; possibly a third ~4B Q8 model on 24 GB.

## JSON enforcement
JSON-Schema structured outputs generated from the Pydantic models:
- native `/api/chat`: pass the schema in `format`;
- OpenAI-compatible endpoint: `response_format = {"type": "json_schema", "json_schema": {"name": …, "schema": Model.model_json_schema()}}`;
- then `model_validate_json` on the result.

Plain `format: "json"` only guarantees syntax. GBNF/Outlines are alternatives with caveats.

## Throughput (analytical envelope, not measured)
Assumptions: prefill 500–1,500 tok/s, decode 30–100 tok/s, 0.2–0.4 s overhead.
- Short tag/score packet (600 in / 60 out): 1,160–3,000 calls/h → **580–1,500 A+B packets/h**.
- Score plus evidence (1,000 in / 150 out): 490–1,310 calls/h → **240–660 packets/h**.

Thousands per day is comfortable even at deliberate pace (D14).

## One-day benchmark protocol (summary)
- 0–2 h: ground truth and frozen harness.
- 2–4 h: screen all six candidates.
- 4–6 h: finalists and calibration.
- 6–7 h: locked test and pair selection by joint error.
- 7–8 h: residency soak and decision.

## Verification and challenge (Gemini Argon, web-sourced)
- **29 claims checked: 26 CONFIRMED, 3 CORRECTED.**
  - Confirmed: Qwen3.5-4B (Apache-2.0; `qwen3.5:4b-q8_0`; Q8 5.2 GB with a 676 MB vision projector), Phi-4-mini-instruct (3.84B, MIT; `phi4-mini:3.8b-q8_0`), Granite-4.2-3B (3.7B, Apache-2.0, Aug 2026), Ministral 3 8B Instruct 2512 (Apache-2.0).
  - Corrected: **Gemma 4 E4B is 8B-class in weights**, not 4B (PLE embeddings + projector) — drop it from the 16 GB shortlist.
- **Ollama caveats:**
  - `phi4-mini:3.8b-q8_0` defaults to a 4K context in its Modelfile; always set `num_ctx` explicitly.
  - Gemini reports a Qwen3.5 JSON problem when thinking is disabled (Ollama GitHub issue #14850 per Gemini) **— unverified by the PM; the benchmark must test it.**
- **Gemini's alternative pairs** (tie-break in the benchmark):
  - 16 GB: **Phi-4-mini Q8 + Granite-4.2-3B Q8** (`think: false`), or a text-only Qwen3.5-4B. About 9.8 GB in total, leaving more than 6 GB headroom.
  - 24 GB: **Ministral-3-8B Q8 + Phi-4-mini Q8**. About 15 GB, leaving room for Granite as aggregator C.
- **Throughput sanity check** (decode is memory-bandwidth bound):
  - 4B Q8 decode: RTX 4060 Ti 16 GB ≈ 32–42 tok/s; RTX 5060 Ti 16 GB ≈ 55–65 tok/s; RTX 3090 ≈ 110–130 tok/s.
  - 8B Q8 on a 3090 ≈ 65–75 tok/s.
  - GPT-6.1's envelope holds at its lower-to-middle range on 16 GB cards.
  - *CFO note:* GPU memory bandwidth, not just VRAM size, sets throughput.
- **Top unattended risk: silent model and template drift** (Ollama tags were re-published several times in Oct 2026). Pin SHA-256 digests; build immutable local models (`ollama create eva-a-locked`, with `FROM …@sha256`, a frozen TEMPLATE and explicit PARAMETERs); at startup check `/api/tags` against an allowlist and refuse to start on a mismatch. Gemini lists four further risks (VRAM fragmentation/context, thermal throttling, and others) with code-level mitigations in the source chat.

## PM decision
Benchmark both proposals: (Qwen3.5-4B, Phi-4-mini) and (Phi-4-mini, Granite-4.2-3B) on 16 GB; (Ministral-3-8B, Phi-4-mini) and (Qwen3.5-4B, Ministral-3-8B) on 24 GB. Select by joint error on ground truth. Digest pinning becomes a requirement (M2 amendment B11).
