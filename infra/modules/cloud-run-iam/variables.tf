# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
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

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "service_names" {
  description = "Map of service key -> Cloud Run service name (from cloud-run-services.service_names)."
  type        = map(string)
  # Required keys: gateway, runner_autopilot, runner_cloudrun, leadership_runner_autopilot, leadership_runner, ai_echo, ai_echo_reactive, ai_echo_autopilot, frontend, admin, dash, tester
}

variable "agent_engine_sa_email" {
  description = "Reasoning Engine service account that invokes the gateway."
  type        = string
}

variable "compute_sa_email" {
  description = "Default compute SA used as the runtime identity for Cloud Run services."
  type        = string
}

# ---------------------------------------------------------------------------
# Customer-facing vs operator-facing surface split.
#
# This module enforces a two-tier model that matches the deployment
# posture of an external SaaS hosted on Cloud Run:
#
#   CUSTOMER tier (`customer_uis_unauthenticated`) - the frontend BFF
#   is the ONLY service customers' browsers hit directly. It must be
#   bound to allUsers because external customers have no Google
#   identity. Auth is enforced at the application layer (Firebase ID
#   tokens verified on every /api/v1/* call via middleware.Account).
#
#   OPERATOR tier (`operator_principals`) - admin / dash / tester are
#   operator-only surfaces (admin dashboard, telemetry stream, load
#   generator). For an external SaaS where the operator IS a Google
#   identity but customers are not, the cheapest correct posture is:
#       - NEVER bind allUsers on these three.
#       - Bind roles/run.invoker to each operator principal so Cloud
#         Run's frontend rejects unauthenticated requests BEFORE they
#         reach the container.
#
#   Cost: $0/mo. No IAP. No code change in customer
#   path.
#
#   Operator access pattern after this is applied:
#       gcloud run services proxy admin --region=us-central1 --port=8080
#       # then browse to http://localhost:8080
#   OR install a "Cloud Run Invoker" browser extension that injects
#       Authorization: Bearer $(gcloud auth print-identity-token)
# ---------------------------------------------------------------------------

variable "customer_uis_unauthenticated" {
  description = "If true (default), bind allUsers as roles/run.invoker on the customer-facing frontend BFF only. REQUIRED true for an external SaaS - customers' browsers must reach /login without a Google identity. App-layer auth (Firebase ID token) is the real gate."
  type        = bool
  default     = true
}

variable "operator_principals" {
  description = "IAM members (e.g. ['user:ops@example.com', 'group:ops-team@example.com']) granted roles/run.invoker on the operator-only services: admin, dash, tester. These services are NEVER bound to allUsers. Empty list = no operator access bindings (services fully locked; use only for fully automated deploys where the operator surface is not needed)."
  type        = list(string)
  default     = []
  validation {
    condition = alltrue([
      for m in var.operator_principals :
      can(regex("^(user:|group:|serviceAccount:|domain:|principal:|principalSet:)", m))
    ])
    error_message = "operator_principals entries must be valid IAM members (user:, group:, serviceAccount:, domain:, principal:, principalSet:)."
  }
  # 2026-06-23 single-operator lockdown: defense-in-depth guard. The
  # operator surface (admin/dash/tester) MUST NEVER be reachable by an
  # untargeted principal. Reject the four ways someone could slip a
  # broad grant in via this list:
  #   - allUsers / allAuthenticatedUsers (public)
  #   - domain:<example.com>             (every Workspace user)
  #   - group:<addr>                     (membership is mutable
  #                                       out-of-band; if you must use a
  #                                       group, drop this validation and
  #                                       review group ACLs separately)
  # tfvars-level commentary already warns about this; this validation
  # makes a typo/oversight a hard plan failure instead of a silent
  # production exposure.
  validation {
    condition = alltrue([
      for m in var.operator_principals :
      !contains(["allUsers", "allAuthenticatedUsers"], m) &&
      !startswith(m, "domain:") &&
      !startswith(m, "group:")
    ])
    error_message = "operator_principals must not contain allUsers, allAuthenticatedUsers, domain:, or group: members. Only named user: or serviceAccount: principals are allowed on the operator tier (admin/dash/tester). See infra/terraform.tfvars HARD RULE comment."
  }
}
