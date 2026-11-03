# PythonPlanner — Python Orchestrator

The custom Python kernel of an autonomous project/software-development stack. It consolidates messy planning documents into a standardised `Project > Objective > Task (Prompt Packet)` database, has small local LLMs score and tag every packet, uses SQL to make deterministic decisions from those evaluations, and dispatches work to local generation or the juggler.studio workbench, writing accepted output to a separate production repository checked by SonarQube. Completed work is immutable.

**Status:** building the v1.0.0.0 prototype — see `docs/PROGRESS.md`.

- Project plan (spec of record): [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md)
- Milestone specs: [`docs/milestones/`](docs/milestones/)
- Agent guide: [`CLAUDE.md`](CLAUDE.md)
- Original draft (archived): [`docs/archive/`](docs/archive/)

Quickstart instructions will be added as milestone M1 lands.
