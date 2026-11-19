# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Native Cloud Logging + Cloud Trace observability metrics (Option #1: zero BigQuery).
# Structured JSON logs and OpenTelemetry traces are emitted by FastAPI / ADK
# directly into Cloud Logging and Cloud Trace and aggregated here into metrics.

resource "google_logging_metric" "skycare_hitl_gates" {
  project     = var.project_id
  name        = "${replace(var.project_name, "-", "_")}_hitl_gate_events"
  description = "Count of HITL approval gates triggered or resolved by SkyCare Concierge."
  filter      = "resource.type=\"cloud_run_revision\" AND jsonPayload.audit.event_type=~\"hitl_.*\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key        = "outcome"
      value_type = "STRING"
    }
  }

  label_extractors = {
    "outcome" = "EXTRACT(jsonPayload.audit.outcome)"
  }

  depends_on = [google_project_service.services]
}

resource "google_logging_metric" "skycare_pii_redactions" {
  project     = var.project_id
  name        = "${replace(var.project_name, "-", "_")}_pii_redaction_events"
  description = "Count of PII sanitization events before logging or memory consolidation."
  filter      = "resource.type=\"cloud_run_revision\" AND jsonPayload.audit.event_type=\"pii_redaction\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }

  depends_on = [google_project_service.services]
}
