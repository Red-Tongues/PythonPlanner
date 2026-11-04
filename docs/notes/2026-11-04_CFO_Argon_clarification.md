# CFO ↔ Argon clarification session — 4 November 2026 (evening)

**Source:** Sam's own Gemini chat "Project Planner Architectural Review" (Argon), with a repo zip of main @ f869e6a. Extracted by the PM at Sam's request.
**Status:** *authoritative product clarification.* Where this note conflicts with earlier specs, this note wins until the specs are updated.

## Part A — What was clarified (in Sam's words where possible)

### A1. The entry point is the prompt window, and it pauses the loop
- "Injecting a project plan or idea or prompt is the first entry point. **Doing so pauses the loop** (which re-triggers after loop readiness is achieved)."
- The **prompt window** takes a markdown project file or a plain-text prompt. It enumerates it **via a frontier model through Juggler** into a standardised SQL database.
- Because the project database is SQL, **Sam can also drill into it and edit or add to it manually**.

### A2. Course changes use the same mechanism and branch the project
- "It's the same mechanism if mid-way through the project you decide to change course completely." The enumerating LLM simply gets more context: the existing SQL database and/or repo.
- "Entering a prompt midway through is disruptive since it pauses ongoing processing and **branches the project**."
- "The feature should **not** be considered or appear like a typical LLM chat interface." Argon's suggested framing is a *branching directive / re-baseline*, not chat.

### A3. genPacket vs eva_packet: noun and verb
- **genPacket**, the noun: "the birth and life of generations… essentially the list of tasks, each with attached metadata. The metadata is steadily built up via eva_packets, mathematical processing and then eventually production."
- **eva_packet**, the verb: "a sublayer for building genPackets". It is the stateless **6-pass** quickfire job that local models use to compute one tag or score reliably and write it into a genPacket field. "Each tag and score would receive 6 passes."
- **The generation output is itself a field on the genPacket.** Filling it unlocks further (post-generation) fields. "This keeps everything relational in the loop."
- Explicitly **not** true: "every generation task must first be born as an eva_packet".
- Two uses of evaluation were not distinguished before, and now are: (a) quickfire tags and scores *before* generation; (b) evaluation *of the generation itself*. Both go through the same eva_packet mechanism, writing fields on the same genPacket.

### A4. Juggler is core plumbing
Juggler is "the bridge/router that connects your prompt window and SQL database to external frontier models". It is used to enumerate the project into SQL. So M4 being a Juggler milestone is correct. *(Open question for the PM: Juggler is also used for Workbench Production generation in the M4 spec. Is it one bridge for both enumeration and generation?)*

### A5. Problems in how the build team works (Argon, in plain English)
1. **"Three copies of the same floorplan."** The schema is written in `ddl.py`, `models.py` and the migrations, and contracts live in two folders. Fix: one master schema definition, one `contracts/` folder.
2. **"Pouring concrete before the house is built."** Frozen migrations (0001–0006) force patch-on-patch. With no live data, squash and clean up instead. *(This is a build-process rule. It is separate from the product rule that completed work is immutable, Requirement F, which stays.)*
3. **M3 scope overload.** Split it:
   - **M3a**: the genPacket loop and pause/branch working in SQL.
   - **M3b**: the mobile digest and interface on top of it.
   
   Sam can inspect SQL directly, so M3a doesn't need the digest.
4. **Old meeting notes confusing the AI coder.** 20 R/D files in `docs/reviews/`, and tests named after review rounds. Fix: fold every final decision into the master docs (ARCHITECTURE, the milestone file) and move debate files to `docs/archive/`. Name tests by capability, not by review round.

### A6. Crash during a run
Confirm "never re-run automatically" for workbench/generation runs. The genPacket's output field simply stays NULL. Local eva_packets are different: they are stateless, local, and have no side effects, so they **quietly restart** after a crash.

## Part B — Sam's follow-up to the PM (20:11)
- **Q1 (roadmap):** not certain. Concern: **meta confusion**. When does a prompt or suggestion apply to *the product*, and when to *our build workflow*?
- **Q2:** park Tailscale. **Split M3 into M3a and M3b** (decided).
- **Q3:** do **not** bring a separate project (stem organisation) into this; it adds confusion.
- **Q4: there is no "automatic re-run" question in the loop.** The loop continuously processes genPackets by queue position.
  - A failed generation leaves `generation_output` NULL and `failed_generations += 1`.
  - That may or may not change queue position, or trigger further eva_packet work, depending on the queue maths: time statistics, eva confidence, other tags, and tertiary fields such as *importance*, *priority* and *queue position*. Context matters too: a 15-second failure early in a quiet project may change nothing; a second 2-minute failure among many competitors probably drops the packet back.
- **New gap raised:** empty output is rare; **poor or messy output is the real problem**, and Pydantic alone can't solve it. We need a more sophisticated **output and recorded-thinking evaluation tool**, so a genPacket can be reassessed quickly after a generation attempt (Juggler can read the thinking). Open for discussion.
- **PM accountability:** managing the project includes spotting confusion and resolving it through **direct discussion**.

## Part C — The working style to replicate (Sam's method with Argon)
1. **Open with a broad diagnostic question**, not a task list: "review the project… look for where the objectives are not completely clear or perhaps confused".
2. **Take it piece by piece:** "please respond only to the points I am addressing".
3. **Explain the owner's mental model in plain words**, then ask "does that make more sense?"
4. **Ask what is still unaddressed** after each clarification.
5. **Ask for plain English** when it gets technical ("I am not the engineer"), using analogies, why it matters, and the one instruction to give.
6. **Use the model to sanity-check a decision** with a short "how about this concern?", rather than a long specification.

**PM commitments from this:**
- Use short, conversational, one-topic exchanges with panel models (and with Sam) for clarification work. Save the long structured prompts for reviews of code or specs that are already settled.
- Before generating more design, run a **confusion check**: ask a model what is unclear or contradictory, and bring those points to Sam as plain questions.
- Label every decision and document as **PRODUCT** (how the orchestrator behaves) or **BUILD PROCESS** (how our team builds it).
