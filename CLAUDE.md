# PropPay Agent — Claude Code project guide

## What this is
A local, zero-cost POC of an **agentic US domestic B2B payables desk for real estate**.
A property manager's AP run (vendor invoices) flows through LLM agents for intake,
fraud monitoring, rail selection, approvals, execution and reconciliation.
No real money moves. All bank interactions are mocked.

Full spec: `docs/SPEC.md`. Read it before starting any phase.

## Golden rule
**LLMs triage, extract and explain. Deterministic code decides whether money moves.**
- Never let an LLM output directly approve, release or route a payment.
- Every money decision comes from `app/rules/` (pure Python, unit-tested) using values from `config/*.yaml`.
- The LLM may add a risk *rationale* and *suggestions*; the rules engine has the final say.

## Stack
- Python 3.11+, managed with `uv`
- LLM: Ollama on localhost (`OLLAMA_MODEL`, default a Qwen 8B instruct model) via `langchain-ollama`
- Orchestration: LangGraph (with `interrupt` for human approvals, SQLite checkpointer)
- Data: SQLite via SQLModel
- Mock bank: FastAPI (`mock_bank/`)
- UI: Streamlit (`ui/`)
- Tests: pytest

## Conventions
- All LLM outputs are parsed into Pydantic models. On parse failure: retry once, then route to human review. Never guess.
- Agent loops are capped (`MAX_AGENT_STEPS=5`).
- Money is stored as integer cents. Never floats.
- Every state change writes a row to `audit_log` (actor = agent name or user, action, before/after, reason).
- Payment execution calls carry an idempotency key (`payment_id` + attempt number).
- Rail limits, fees and policy thresholds live in `config/`, never hard-coded.
- Keep LLM prompts in `app/prompts/` as plain text files.
- Domain terms: STP (straight-through processing), 3-way match, CCD+, positive pay, R-codes, BAI2.

## Commands
- `uv sync` installs dependencies
- `uv run python scripts/seed_data.py` resets the DB and seeds the demo data
- `uv run uvicorn mock_bank.main:app --port 8001` starts the mock bank
- `uv run python scripts/run_ap.py run|pending|approve|reject|callback` drives the AP run from the terminal (graph state in `data/checkpoints.db`)
- `uv run python scripts/prepare_demo.py` seeds + runs the overnight batch + snapshots the demo start (~7 min); `--restore` resets to it in ~1 s. Demo script: `docs/DEMO.md`
- `uv run streamlit run ui/Home.py` starts the UI (restart it after editing `ui/common.py`: Streamlit doesn't reload imported modules)
- `uv run pytest` runs the tests
- `uv run python evals/build_gold.py` rebuilds the gold set from the seeded manifest
- `uv run python evals/run_extraction_eval.py` runs the invoice-extraction eval (~7 min; `--model`, `--only`, `--verbose`)
- `uv run python evals/run_decision_eval.py` runs intake -> match -> fraud -> rules live on a temp seed and scores decisions (~8 min)

## Hardware constraint
Runs on a MacBook Pro with 18GB RAM. Keep context windows around 8K. Load one LLM at a time. Don't add GPU-heavy dependencies.
