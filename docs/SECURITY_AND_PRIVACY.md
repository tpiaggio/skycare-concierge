# Security, Privacy, and Compliance Controls

SkyCare Concierge is designed around defense-in-depth protections covering prompt inputs, tool calls, model responses, operational logs, cross-session memory, and partner API credentials.

## 1. Deterministic PII & Secret Redaction Pipeline

Implemented in `app/security/pii_redactor.py` via `redact_text(text)` and `redact_payload(value)`:

| Entity / Pattern | Detection Strategy | Replacement Token | Notes |
| --- | --- | --- | --- |
| Secret / API / Bearer tokens | Regex (`AIza...`, `Bearer ...`, `sk-...`) | `[REDACTED_SECRET]` | Executed first so keys aren't split by digit rules |
| Social Security Numbers | `\d{3}-\d{2}-\d{4}` | `[REDACTED_SSN]` | Full mask |
| Passport Numbers | Contextual regex (`passport`/`pasaporte`/`documento` `is/es/no/num/number/#/: ...`) | Preserves prefix + `[REDACTED_PASSPORT]` | Bilingual EN/ES; avoids false positives on flight numbers |
| Dates of Birth | Contextual regex (`dob` / `date of birth` / `fecha de nacimiento` `is/es`) | Preserves prefix + `[REDACTED_DOB]` | Supports ISO (`YYYY-MM-DD`) and slash (`DD/MM/YYYY`) formats |
| Email Addresses | RFC-like email regex | `[REDACTED_EMAIL]` | Full mask |
| Payment Card Numbers | 13-19 digit candidates verified via `_passes_luhn(...)` | `[REDACTED_CARD:****<last4>]` | Preserves last 4 digits; Luhn spares flight numbers & PNRs |
| Phone Numbers | International / North American phone regex | `[REDACTED_PHONE]` | Full mask |
| Structured Sensitive Keys | `_SENSITIVE_KEY_EXACT` (`password`, `cvv`, `pan`, `ssn`, `passport_number`, `token`, `api_key`, etc.) | `[REDACTED_<KEY>]` | Applied recursively across dicts, lists, and tuples |

## 2. Multi-Layer Guardrails Plugin & Session-Poisoning Defense

Implemented in `app/plugins/safety_guardrails.py` (`SkyCareGuardrailsPlugin(BasePlugin)`):

1. **`on_user_message_callback` (Pre-Ingestion Screening)**:
   - Deterministic rules (`evaluate_prompt_safety_rules`) match prompt-injection overrides, system prompt exfiltration, HITL approval bypasses, and SQL/tool injection phrases (`DROP TABLE`, `DELETE FROM`, etc.).
   - Secondary LLM Judge (`_evaluate_prompt_with_judge_llm` using `gemini-3.8-flash` at `temperature=0.0` with structured `PromptSafetyVerdict`) screens subtle jailbreaks with PII already stripped before judging (`redact_text(prompt_text).text`). Disabled deterministically in `dry_run` mode.
   - Records `session.state["is_user_prompt_safe"]`, `safety_block_category`, and `safety_block_reason`.
2. **`before_run_callback` (Execution Halt & State Recovery)**:
   - When `is_user_prompt_safe` is `False`, logs a structured warning (`outcome="blocked_policy"`), resets `is_user_prompt_safe = True` so subsequent valid user messages in the same session are not permanently locked out (session-poisoning prevention), and returns an immediate safe refusal response without invoking tools or the planner.
3. **`before_tool_callback` (Tool Parameter Validation)**:
   - Tracks per-invocation timing in a thread-safe instance map keyed by `(invocation_id, function_call_id)` (avoiding polluting ADK session `State` deltas).
   - Blocks suspicious tool argument substrings matching `_DANGEROUS_TOOL_ARG_RE` (`; DROP TABLE`, `../../`, `AIza...`, `Bearer ...`) with `ToolErrorOutput(error_code="POLICY_BLOCKED_TOOL_ARGS")`.
4. **`after_tool_callback` & `after_model_callback` (Egress PII Scrubbing)**:
   - Recursively scrubs unexpected PII from tool dicts before the model observes them (`redact_payload`) and scrubs outgoing model text parts (`redact_text`) before returning to the user, emitting `pii_redaction` audit events whenever masking occurs.

## 3. Memory Privacy Controls

Cross-session persistence (`app/memory/async_memory.py` + `app/app_utils/memory_config.py`):

- **Pre-Write Sanitization**: `sanitize_session_for_memory` redacts every message part and drops all session state keys outside `_ALLOWED_MEMORY_STATE_KEYS`.
- **Managed Topic Exclusion**: Vertex AI Memory Bank configuration explicitly excludes `ManagedTopicEnum.USER_PERSONAL_INFO` and instructs `traveler_seating_and_cabin_preferences` never to retain passport numbers, payment cards, phone numbers, emails, or SSNs.

## 4. Structured Intent-vs-Outcome Telemetry & Cloud Logging Metrics

Implemented in `app/observability/logging.py`:

- `StructuredJsonFormatter` formats single-line JSON logs for Cloud Logging containing `timestamp`, `severity`, `service`, `logger`, `message` (redacted), recursive PII-masked extras, and OpenTelemetry trace fields (`trace_id`, `span_id`, `logging.googleapis.com/trace`, `logging.googleapis.com/spanId`, `logging.googleapis.com/trace_sampled`).
- `log_intent_and_outcome(...)` captures both the declared agent/tool `intent` + `intended_action` and the actual `outcome` (`succeeded`, `awaiting_approval`, `approved`, `rejected`, `blocked_policy`, `redacted`, `error`), maintaining a bounded thread-safe audit buffer surfaced by `GET /api/audit`.
- Terraform (`deployment/terraform/single-project/telemetry.tf` and `deployment/terraform/cicd/telemetry.tf`) configures Cloud Logging metrics (`hitl_gate_events`, `pii_redaction_events`) without any BigQuery exports or tables.

## 5. Secret Management & Least-Privilege IAM

Implemented in `app/security/secrets.py` and `deployment/terraform/single-project/secrets.tf`:

- `SecretValue` masks its internal `_raw` string in both `__str__` (`***REDACTED***`) and `__repr__` (`SecretValue(***REDACTED***)`), requiring explicit `.reveal()` strictly at credential use sites (`_verify_gds_partner_connectivity_note`).
- `SecretManagerProvider` queries `projects/{project_id}/secrets/{secret_id}/versions/{version}` via `SecretManagerServiceClient` with monotonic TTL caching (`secret_cache_ttl_seconds = 300`) and falls back to `EnvSecretProvider` (`SKYCARE_SECRET_*`) in local/dry-run environments.
- Terraform provisions Secret Manager shells (`gds-partner-api-key`, `dispatch-webhook-signing-key`) and scopes IAM access (`roles/secretmanager.secretAccessor`) solely to the dedicated Cloud Run service account (`google_service_account.app_sa.email`), ensuring secret values are never placed in Terraform state or Git.
