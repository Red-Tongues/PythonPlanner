# PythonPlanner — Python Orchestrator

The custom Python kernel of an autonomous project/software-development stack. It consolidates messy planning documents into a standardised `Project > Objective > Task (Prompt Packet)` database, has small local LLMs score and tag every packet, uses SQL to make deterministic decisions from those evaluations, and dispatches work to local generation or the juggler.studio workbench, writing accepted output to a separate production repository checked by SonarQube. Completed work is immutable.

**Status:** v1.0.0.0 prototype in progress. M1 (kernel foundation) is merged. M2 (evaluation engine) adds the genPacket/evaPacket model: six-pass dual-model evaluation with a deterministic fake or pinned local models, SQL consensus/statistics/branching/queue views on SQLite and PostgreSQL, readiness with waivers and queue overrides, branching and an approval queue. See `docs/PROGRESS.md` and `docs/ARCHITECTURE.md`.

- Project plan (spec of record): [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md)
- Milestone specs: [`docs/milestones/`](docs/milestones/)
- Agent guide: [`CLAUDE.md`](CLAUDE.md)
- Original draft (archived): [`docs/archive/`](docs/archive/)

## Quickstart

Requires Python 3.11 or newer.

```bash
pip install -e ".[dev]"          # add ,postgres for PostgreSQL support (psycopg)
```

```bash
orchestrator init-db                                  # migrate to head, install triggers, seed defaults
```

```bash
orchestrator ingest examples/sample_project_plan.md   # messy plan → draft packets
```

```bash
orchestrator tree                                     # Project > Objective > Task with refs and statuses
```

The output looks like this:

```
P-0001  Pantry Pal — planning dump (v0.3?? check w/ Priya)
  O-0001-01  Inventory
    PP-000001  [draft]  Barcode scanning via phone camera (use an open food database, NOT a paid API)
    PP-000002  [draft]  manual add/edit/delete of items with quantity + expiry date
  ...
```

Other commands:

| Command | What it does |
|---|---|
| `orchestrator version` | Print the version |
| `orchestrator fields [--level task\|content]` | List the candidate-field registry (`*` = key field) |
| `orchestrator packet show PP-000001` | Core and governance fields, versions and events of a packet (`PP-000001@1` = a specific version) |
| `orchestrator packet complete PP-000001` | Complete **and lock** a packet; it can then only be superseded by a new version |
| `orchestrator events [--limit N]` | The audit log, newest first |
| `orchestrator evaluate [--packet REF] [--limit N]` | Six-pass quickfire evaluation of pending packets (FakeProvider unless `ORCH_LLM_BASE_URL` is set) |
| `orchestrator consensus REF` | Each field's final value, confidence, status and verified evidence |
| `orchestrator stats [REF]` | Packet and federated branch statistics |
| `orchestrator queue [--channel C] [--all]` | The generation queue in its unique deterministic order |
| `orchestrator branch apply` | Apply fired split rules (respects the branching autonomy scope) |
| `orchestrator confirmations list\|approve ID\|reject ID` | The approval queue |
| `orchestrator waive REF FIELD --reason …` | Accept a soft field for this packet version only (never a hard gate) |
| `orchestrator override REF block\|hold\|force\|pin\|reorder --reason …` / `revoke` | Human queue decisions (FORCE changes priority only) |
| `orchestrator status` | Schema revision, evaluator models and digest-pinning result, packet counts |
| `orchestrator ingest --llm PATH` | Extract the tree with the LLM instead of the parser (same contract and confirmation) |

Configuration comes from environment variables prefixed `ORCH_` (or a `.env` file): `ORCH_DATABASE_URL` (default `sqlite:///./orchestrator.db`; e.g. `postgresql+psycopg://user:pass@host/db`), `ORCH_PRODUCTION_REPO_PATH`, `ORCH_WORKBENCH_PATH`, `ORCH_LOG_LEVEL`. Evaluator models (M2): `ORCH_LLM_PROVIDER` (`auto`, `fake`, `ollama`, `openai`), `ORCH_LLM_BASE_URL`, `ORCH_MODEL_A` / `ORCH_MODEL_B` / `ORCH_MODEL_C` and their pinned `ORCH_MODEL_A_DIGEST` etc. (evaluation is refused on a digest mismatch). Every command also accepts `--database-url`.

## Development

```bash
pytest -q                                                            # full suite on SQLite
```

```bash
ORCH_TEST_POSTGRES_URL=postgresql+psycopg://user:pass@localhost/test pytest -q    # SQLite + PostgreSQL
```

```bash
ruff check . && ruff format --check .
```

With `ORCH_TEST_POSTGRES_URL` set, every database test runs on both dialects, and `pytest -m postgres` selects only the PostgreSQL runs. The test database's `public` schema is dropped and recreated for each test, so point the variable at a throwaway database.

### Where things live

| Module | Responsibility |
|---|---|
| `orchestrator.contracts` | Strict, versioned Pydantic contracts (`ProjectTree`, `TaskCore`, `CandidateFieldDefinition`, …), enums, response-structure registry |
| `orchestrator.fields` | Candidate-field registry seed (`candidate_fields.yaml`) and loader |
| `orchestrator.db` | The master schema: SQLAlchemy models (`models.py`), every trigger (`ddl.py`), assembly (`schema.py`); the generated baseline migration (`schema_version` table); seeds |
| `orchestrator.repository` | Governed reads and writes: head/version lookups, `supersede_*`, guarded updates, each with an event |
| `orchestrator.lifecycle` | Allowed packet-status transitions; every transition writes an event |
| `orchestrator.ingest` | `.md` / `.txt` / `.docx` → `ProjectTree` → draft packets (+ a confirmation if the ingestion scope is `confirm`) |
| `orchestrator.llm` | Provider protocol, `FakeProvider`, OpenAI-compatible and Ollama providers with digest pinning |
| `orchestrator.evaluation` | evaPacket evaluator, pass plans, prompts, contracts, consensus oracle, readiness |
| `orchestrator.sql` | Every SQL view (part of the master schema) |
| `orchestrator.queue`, `governance`, `branching`, `confirmations` | Queue admission and claims; waivers and overrides; splits and promotion; approval actions |
| `orchestrator.cli` | The Typer CLI |
