# SkyCare Concierge (`skycare-concierge`)

An enterprise multi-agent AI **Airline Disruption & Passenger Recovery Desk** built with **Google ADK**, **Vertex AI Gemini 3.x (`gemini-3.8-flash` & `gemini-3.1-pro-preview`)**, **Vertex AI Agent Platform (`agent_platform_sessions` & Memory Bank)**, **FastAPI**, and **A2A (Agent2Agent)**.

## Highlights

- **Strategic Multi-Agent Routing**:
  - `skycare_concierge` Coordinator (`gemini-3.8-flash`) triages inquiries, preloads non-PII traveler preferences, and delegates cleanly.
  - `disruption_triage_agent` (`gemini-3.8-flash`) retrieves PNR state and evaluates deterministic EU261 / UK261 / US DOT rules.
  - `recovery_planner_agent` (`gemini-3.1-pro-preview`) performs deep multi-leg itinerary optimization, connection risk assessment, and preference fit scoring.
  - `passenger_care_agent` (`gemini-3.8-flash`) executes controlled duty-of-care vouchers and PNR rebookings behind Human-in-the-Loop (HITL) approval gates.
- **Human-in-the-Loop (HITL) Safety & Single-Use Approvals**: Vouchers `> $150.00` and all PNR rebookings create a `PENDING` supervisor request (`PENDING -> APPROVED/REJECTED -> CONSUMED`) surfaced in the Operations Desk UI and `/api/approvals`.
- **Defense-in-Depth Guardrails & Privacy**: `SkyCareGuardrailsPlugin` blocks prompt injection (deterministic rules + Flash `PromptSafetyVerdict` judge) without poisoning sessions, blocks dangerous tool parameters, and masks sensitive PII/credentials across tools, responses, logs, and memory.
- **Non-Blocking Cross-Session Memory**: `AsyncMemoryCoordinator` strips PII (`sanitize_session_for_memory`, `_ALLOWED_MEMORY_STATE_KEYS`) and schedules background consolidation without stalling user turns.
- **Zero BigQuery Architecture**: Implemented with **Option #1** (`agent_platform_sessions` + Memory Bank + WAL SQLite / GCS snapshot `ReservationRepository` + Cloud Trace & Cloud Logging metrics). **Option #2** (`cloud_sql` PostgreSQL 15) is documented as an ADR in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Custom Operations UI & A2A**: Serves the custom SkyCare Operations Desk at `/` and `/ui`, ADK Dev UI at `/dev-ui/`, SSE streaming at `/run_sse`, REST APIs at `/api/*`, and A2A v0.3 JSON-RPC + Agent Card at `/a2a/app/`.

## Project Structure

```text
skycare-concierge/
├── app/
│   ├── agent.py                 # Root coordinator, App config, compaction, caching, resumability
│   ├── fast_api_app.py          # FastAPI server, custom Web UI routes, HITL/audit APIs, A2A mount
│   ├── agents/specialists.py    # Flash triage/care and Pro recovery planner sub-agents
│   ├── app_utils/               # A2A wiring, Agent Platform runtime, Memory Bank config
│   ├── core/config.py           # Centralized SkyCareSettings and Vertex AI env/client builder
│   ├── memory/async_memory.py   # AsyncMemoryCoordinator & pre-write PII session sanitizer
│   ├── observability/logging.py # Structured JSON Cloud Logging, OTel trace IDs, audit ring buffer
│   ├── plugins/safety_guardrails.py # SkyCareGuardrailsPlugin (jailbreak, injection, PII scrubbing)
│   ├── security/                # Luhn-validated PII redactor & Secret Manager provider abstraction
│   ├── storage/repository.py    # WAL SQLite + optional GCS snapshot repository & HITL state machine
│   ├── tools/                   # Strict Pydantic schemas & 6 airline recovery tools
│   └── web/                     # Custom SkyCare Operations Desk UI (HTML/CSS/JS)
├── deployment/terraform/        # Single-project and CI/CD (WIF) Terraform IaC (no BigQuery)
├── docs/                        # Architecture, AgentOps Matrix, Security, Deployment, Demo guides
├── tests/
│   ├── eval/                    # 7-case golden dataset, custom Gemini judge, threshold config
│   ├── integration/             # Offline FastAPI E2E, live ADK stream, live SSE/A2A server tests
│   ├── load_test/               # Locust load test target
│   └── unit/                    # Security, logging, tools, repository, plugin, and memory tests
├── agents-cli-manifest.yaml
└── pyproject.toml
```

## Quick Start

### 1. Prerequisites
- Python `>=3.11,<3.14` and [`uv`](https://docs.astral.sh/uv/)
- `google-agents-cli >= 1.15.0`
- Google Cloud CLI (`gcloud`) authenticated with ADC to your target project (e.g., `your-gcp-project-id`)

### 2. Install & Run Locally
```bash
uv sync --dev --extra lint

# Run locally with in-memory ADK sessions (no remote Agent Runtime required)
USE_IN_MEMORY_SESSION=true uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8000 --reload
```

Open:
- **SkyCare Operations Desk UI**: `http://127.0.0.1:8000/`
- **ADK Dev UI**: `http://127.0.0.1:8000/dev-ui/`
- **A2A Agent Card**: `http://127.0.0.1:8000/a2a/app/.well-known/agent-card.json`

## Verification & Evaluation

```bash
# Lint, formatting check, spellcheck, and static type check (ruff, codespell, ty)
agents-cli lint

# Offline unit + API end-to-end tests
uv run pytest tests/unit tests/integration/test_offline_api_e2e.py -v

# Live ADK & FastAPI server integration tests (requires Vertex AI credentials)
USE_IN_MEMORY_SESSION=true uv run pytest tests/integration/test_agent.py tests/integration/test_server_e2e.py -v

# Live 7-case golden evaluation suite (threshold: custom_response_quality >= 4.0)
agents-cli eval run --project your-gcp-project-id --region us-east1 --concurrency 2 --qps 5
```

## Documentation Index

- [Architecture & Storage ADR](docs/ARCHITECTURE.md)
- [AgentOps Code Review Matrix (19 Criteria / 95 Points)](docs/AGENTOPS_MATRIX.md)
- [Security, PII Redaction & Secret Management](docs/SECURITY_AND_PRIVACY.md)
- [Deployment & Operations Guide](docs/DEPLOYMENT.md)
- [Live Demo & Walkthrough Guide (`SKY842`, `SKY319`, `SKY905`, `SKY404`)](docs/DEMO_GUIDE.md)
