# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Service-level IAM bindings for Cloud Run services.
#
# Replaces the `gcloud run services add-iam-policy-binding` shim that
# was previously in scripts/deploy/deploy.py.
#
# Defense in depth: the iam module ALSO grants project-level
# roles/run.invoker to the AE SA. Project-level grants have shown
# propagation timing issues for AE workloads on cold project bring-up,
# so this service-level grant is the authoritative one.

# ---------------------------------------------------------------------------
# Gateway: invoked by AE agents (planner, simulator) and by other Cloud
# Run services (push subscription delivery, frontend ingress).
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service_iam_member" "gateway_ae_invoker" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["gateway"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.agent_engine_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "gateway_compute_invoker" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["gateway"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

# ---------------------------------------------------------------------------
# Runners: invoked by the gateway via Pub/Sub push subscriptions
# (which are configured to use the compute SA as the OIDC identity).
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service_iam_member" "runner_autopilot_compute" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["runner_autopilot"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "runner_cloudrun_compute" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["runner_cloudrun"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "leadership_runner_autopilot_compute" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["leadership_runner_autopilot"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "leadership_runner_compute" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["leadership_runner"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "ai_echo_compute" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["ai_echo"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "ai_echo_reactive_compute" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["ai_echo_reactive"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "ai_echo_autopilot_compute" {
  project  = var.project_id
  location = var.region
  name     = var.service_names["ai_echo_autopilot"]
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.compute_sa_email}"
}

# ---------------------------------------------------------------------------
# Customer tier: frontend BFF is the ONLY service bound to allUsers.
# External customers have no Google identity; Firebase ID-token
# verification at the application layer (middleware.Account) is the
# real auth gate. See variables.tf for full rationale.
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service_iam_member" "frontend_public" {
  count = var.customer_uis_unauthenticated ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = var.service_names["frontend"]
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Operator tier: admin / dash / tester are NEVER bound to allUsers.
# Each operator principal (var.operator_principals) is granted
# roles/run.invoker on each of the three services. Cloud Run's
# frontend rejects unauthenticated requests at the GCP edge, before
# the container is invoked. Tester in particular MUST stay
# operator-only: it proxies to AEs with the compute SA's OIDC token,
# so an allUsers binding would let any internet caller burn Vertex
# quota.
# ---------------------------------------------------------------------------
locals {
  operator_services = ["admin", "dash", "tester"]
  operator_bindings = {
    for pair in setproduct(local.operator_services, var.operator_principals) :
    "${pair[0]}::${pair[1]}" => {
      service = pair[0]
      member  = pair[1]
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "operator_invoker" {
  for_each = local.operator_bindings

  project  = var.project_id
  location = var.region
  name     = var.service_names[each.value.service]
  role     = "roles/run.invoker"
  member   = each.value.member
}
