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

# Secret Manager resources for partner credentials and webhook signing keys.
# Secret values are injected out-of-band by operators or CI/CD and never
# stored in Terraform state or committed to source control.

resource "google_secret_manager_secret" "gds_partner_api_key" {
  project   = var.project_id
  secret_id = "gds-partner-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret" "dispatch_webhook_signing_key" {
  project   = var.project_id
  secret_id = "dispatch-webhook-signing-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret_iam_member" "app_sa_gds_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.gds_partner_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "app_sa_webhook_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.dispatch_webhook_signing_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app_sa.email}"
}

output "gds_partner_secret_name" {
  description = "Secret Manager resource path for GDS partner credentials"
  value       = google_secret_manager_secret.gds_partner_api_key.name
}

output "dispatch_webhook_secret_name" {
  description = "Secret Manager resource path for dispatch webhook signing key"
  value       = google_secret_manager_secret.dispatch_webhook_signing_key.name
}
