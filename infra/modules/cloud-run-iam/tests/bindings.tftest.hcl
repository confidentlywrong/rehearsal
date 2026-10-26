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

# Service-level IAM binding tests. Locks in:
#   - Gateway: invokable by both AE SA (for AE -> Cloud Run) and compute
#     SA (for Cloud Run -> Cloud Run, e.g. push subscriptions).
#   - Runners: invokable by compute SA (gateway pushes orchestration
#     messages to them via Pub/Sub push subscriptions).
#   - Customer tier: frontend bound to allUsers iff customer_uis_unauthenticated.
#   - Operator tier: admin/dash/tester NEVER bound to allUsers; each
#     entry in operator_principals gets a roles/run.invoker binding on
#     each of the three services.

variables {
  project_id            = "test-project"
  region                = "us-central1"
  agent_engine_sa_email = "ae@test-project.iam.gserviceaccount.com"
  compute_sa_email      = "compute@test-project.iam.gserviceaccount.com"
  service_names = {
    gateway                     = "gateway"
    runner_autopilot            = "runner-autopilot"
    runner_cloudrun             = "runner-cloudrun"
    leadership_runner_autopilot = "leadership-runner-autopilot"
    leadership_runner           = "leadership-runner"
    ai_echo                     = "ai-echo"
    ai_echo_reactive            = "ai-echo-reactive"
    ai_echo_autopilot           = "ai-echo-autopilot"
    frontend                    = "frontend"
    admin                       = "admin"
    dash                        = "dash"
    tester                      = "tester"
  }
}

run "gateway_has_both_invoker_bindings" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_service_iam_member.gateway_ae_invoker.member == "serviceAccount:ae@test-project.iam.gserviceaccount.com"
    error_message = "gateway must allow AE SA as run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.gateway_ae_invoker.role == "roles/run.invoker"
    error_message = "gateway AE binding must be roles/run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.gateway_compute_invoker.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "gateway must allow compute SA as run.invoker"
  }
}

run "runners_invocable_by_compute_sa" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_service_iam_member.runner_autopilot_compute.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "runner_autopilot must allow compute SA as run.invoker (for gateway push subscription)"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.runner_cloudrun_compute.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "runner_cloudrun must allow compute SA as run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.leadership_runner_autopilot_compute.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "leadership_runner_autopilot must allow compute SA as run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.leadership_runner_compute.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "leadership_runner must allow compute SA as run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.ai_echo_compute.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "ai_echo must allow compute SA as run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.ai_echo_reactive_compute.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "ai_echo_reactive must allow compute SA as run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.ai_echo_autopilot_compute.member == "serviceAccount:compute@test-project.iam.gserviceaccount.com"
    error_message = "ai_echo_autopilot must allow compute SA as run.invoker"
  }
}

run "frontend_public_by_default" {
  command = plan

  assert {
    condition     = length(google_cloud_run_v2_service_iam_member.frontend_public) == 1
    error_message = "frontend allUsers binding must be present when customer_uis_unauthenticated=true (default)"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.frontend_public[0].member == "allUsers"
    error_message = "frontend public binding member must be allUsers"
  }
}

run "frontend_locked_when_customer_uis_unauthenticated_false" {
  command = plan

  variables {
    customer_uis_unauthenticated = false
  }

  assert {
    condition     = length(google_cloud_run_v2_service_iam_member.frontend_public) == 0
    error_message = "frontend allUsers binding must be absent when customer_uis_unauthenticated=false"
  }
}

run "operator_services_never_allusers" {
  # Critical security invariant: admin/dash/tester must never receive
  # an allUsers binding from this module, regardless of any flag.
  # Tester in particular proxies to Vertex AEs with the compute SA's
  # OIDC token, so allUsers = open Vertex-quota burn.
  command = plan

  variables {
    operator_principals = []
  }

  assert {
    condition     = length(google_cloud_run_v2_service_iam_member.operator_invoker) == 0
    error_message = "operator_principals empty => no operator bindings emitted"
  }
}

run "operator_principals_get_invoker_on_all_three" {
  command = plan

  variables {
    operator_principals = [
      "user:ops@example.com",
      "user:ops2@example.com",
    ]
  }

  assert {
    # 3 services * 2 principals = 6 bindings.
    condition     = length(google_cloud_run_v2_service_iam_member.operator_invoker) == 6
    error_message = "operator_principals must produce one binding per (service, principal) pair across admin/dash/tester"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.operator_invoker["admin::user:ops@example.com"].role == "roles/run.invoker"
    error_message = "operator bindings must be roles/run.invoker"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.operator_invoker["admin::user:ops@example.com"].name == "admin"
    error_message = "operator binding for admin must reference the admin service"
  }

  assert {
    condition     = google_cloud_run_v2_service_iam_member.operator_invoker["tester::user:ops2@example.com"].member == "user:ops2@example.com"
    error_message = "tester binding must include the second user member"
  }
}

run "rejects_malformed_operator_principal" {
  command = plan

  variables {
    operator_principals = ["ops@example.com"]
  }

  expect_failures = [var.operator_principals]
}

# 2026-06-23 single-operator lockdown: confirm the new defense-in-depth
# validation rejects each broad-principal class. Each variant is a
# separate run block because Terraform stops at the first validation
# failure inside a run.
run "rejects_allusers_operator_principal" {
  command = plan

  variables {
    operator_principals = ["allUsers"]
  }

  expect_failures = [var.operator_principals]
}

run "rejects_allauthenticatedusers_operator_principal" {
  command = plan

  variables {
    operator_principals = ["allAuthenticatedUsers"]
  }

  expect_failures = [var.operator_principals]
}

run "rejects_domain_operator_principal" {
  command = plan

  variables {
    operator_principals = ["domain:example.com"]
  }

  expect_failures = [var.operator_principals]
}

run "rejects_group_operator_principal" {
  command = plan

  variables {
    # Groups are forbidden on the operator tier because group membership
    # is mutable out-of-band. See infra/modules/cloud-run-iam/variables.tf
    # for the rationale.
    operator_principals = ["group:ops-team@example.com"]
  }

  expect_failures = [var.operator_principals]
}
