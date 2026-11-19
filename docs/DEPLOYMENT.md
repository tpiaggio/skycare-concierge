# Deployment & Operations Guide

## 1. Prerequisites

- **Python**: `>=3.11,<3.14` managed with [`uv`](https://docs.astral.sh/uv/)
- **Google Agents CLI**: `google-agents-cli >= 1.15.0`
- **Google Cloud CLI (`gcloud`)** & **Terraform (`>=1.6`)** for infrastructure provisioning
- **Target GCP Project**: e.g., `your-gcp-project-id` with billing enabled and Vertex AI API active

> **Model Location vs. Runtime Region**:
> Gemini models (`gemini-3.8-flash` and `gemini-4-hydrogen`) run on `GOOGLE_CLOUD_LOCATION=global`, while Cloud Run and the Agent Platform Session/Memory Bank runtime run regionally in `us-east1` (`agent_runtime_location()`).

## 2. Local Development Environment

Configure Google Cloud Application Default Credentials (`gcloud auth application-default login`) and export Vertex AI environment variables:

```bash
export PATH="$HOME/.local/bin:$PATH"
export GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
export GOOGLE_CLOUD_LOCATION="global"
export GOOGLE_GENAI_USE_VERTEXAI="true"
```

### Install & Start Local Server

```bash
uv sync --dev --extra lint
USE_IN_MEMORY_SESSION=true uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8000 --reload
```

Or run with live Vertex AI Agent Platform sessions & Memory Bank (`us-east1`):

```bash
uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8000
```

Available local endpoints:
- SkyCare Passenger Concierge UI (`EN` / `ES`): `http://127.0.0.1:8000/` and `http://127.0.0.1:8000/ui`
- SkyCare Supervisor Desk UI: `http://127.0.0.1:8000/supervisor`
- ADK Developer UI: `http://127.0.0.1:8000/dev-ui/`
- OpenAPI Docs: `http://127.0.0.1:8000/docs`
- A2A Agent Card & JSON-RPC: `http://127.0.0.1:8000/a2a/app/.well-known/agent-card.json`

## 3. Quality, Security, Test & Evaluation Gates

### Lint & Type Checking
```bash
agents-cli lint
# Executes: ruff check ., ruff format . --check, codespell, ty check .
```

### Offline Unit & API Tests (Zero Network Calls)
```bash
USE_IN_MEMORY_SESSION=true uv run pytest tests/unit tests/integration/test_offline_api_e2e.py -v
```

### Live ADK & Server E2E Tests (Requires Vertex AI ADC)
```bash
USE_IN_MEMORY_SESSION=true uv run pytest tests/integration/test_agent.py tests/integration/test_server_e2e.py -v
```

### Golden Evaluation Suite (`tests/eval/`)
`tests/eval/eval_config.yaml` enforces `thresholds: { custom_response_quality: 4.0 }` over the `9` bilingual (`EN` / `ES`) airline scenarios in `tests/eval/datasets/basic-dataset.json`:

```bash
agents-cli eval run --project your-gcp-project-id --region us-east1 --concurrency 2 --qps 5
```

Generated traces and interactive HTML/JSON grade reports are saved locally to `artifacts/traces/` and `artifacts/grade_results/` (both git-ignored).

## 4. Infrastructure & Cloud Run Deployment

### Provision Single-Project GCP Infrastructure (Terraform)
```bash
agents-cli infra single-project
# Or manually from deployment/terraform/single-project/:
# terraform init && terraform apply -var-file=vars/env.tfvars
```

Terraform provisions:
- Enabled APIs including `secretmanager.googleapis.com`, Artifact Registry, Cloud Build, Cloud Run, and Vertex AI services (**no BigQuery APIs**).
- Dedicated runtime service account (`google_service_account.app_sa`) with `roles/secretmanager.secretAccessor` scoped to the application secrets.
- Secret Manager shells (`gds-partner-api-key`, `dispatch-webhook-signing-key`) in `deployment/terraform/single-project/secrets.tf`.
- Cloud Logging metrics (`hitl_gate_events`, `pii_redaction_events`).

### Populate Runtime Secrets (Out of Band)
Before enabling external GDS/webhook integrations, add secret versions manually (never commit secret values):

```bash
printf '%s' "$GDS_KEY" | gcloud secrets versions add gds-partner-api-key --data-file=- --project=your-gcp-project-id
printf '%s' "$WEBHOOK_KEY" | gcloud secrets versions add dispatch-webhook-signing-key --data-file=- --project=your-gcp-project-id
```

### Deploy Service to Cloud Run (`us-east1`)
```bash
gcloud config set project your-gcp-project-id
agents-cli deploy --region us-east1
```

### Authenticated Access Options (Cloud Run IAM Proxy vs. IAP)
`agents-cli deploy` enforces `--no-allow-unauthenticated` by default so unauthenticated callers cannot invoke Vertex AI models directly:

1. **Local Cloud Run Proxy (IAM Identity Token)**:
   ```bash
   gcloud run services proxy skycare-concierge --region us-east1 --project your-gcp-project-id --port 8080
   ```
2. **Browser SSO via Identity-Aware Proxy (IAP)**:
   ```bash
   gcloud services enable iap.googleapis.com --project=your-gcp-project-id
   gcloud beta services identity create --service=iap.googleapis.com --project=your-gcp-project-id
   gcloud beta run services update skycare-concierge --region=us-east1 --project=your-gcp-project-id --iap
   gcloud beta iap web add-iam-policy-binding \
     --resource-type=cloud-run \
     --service=skycare-concierge \
     --region=us-east1 \
     --project=your-gcp-project-id \
     --member="user:you@example.com" \
     --role="roles/iap.httpsResourceAccessor"
   ```

### CI/CD Pipeline (GitHub Actions)
`.github/workflows/` includes four workflow definitions (`ci.yaml` offline checks on every push/PR + three WIF-authenticated pipelines paired with `deployment/terraform/cicd/wif.tf`):
1. `.github/workflows/ci.yaml`: Runs `ruff`, `codespell`, `ty`, offline unit + FastAPI E2E `pytest`, and Terraform `fmt` + `validate`.
2. `.github/workflows/pr_checks.yaml`: Pull request verification workflow (WIF).
3. `.github/workflows/staging.yaml`: Staging pipeline workflow (WIF, skipped automatically when WIF vars are unset).
4. `.github/workflows/deploy-to-prod.yaml`: Production deployment workflow (WIF).
