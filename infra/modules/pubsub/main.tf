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

resource "google_project_service_identity" "pubsub" {
  provider = google-beta
  project  = var.project_id
  service  = "pubsub.googleapis.com"
}

resource "google_pubsub_topic" "specialist_orchestration" {
  name    = "specialist-orchestration"
  project = var.project_id

  labels = {
    purpose = "specialist-agent-orchestration"
  }
}

# Telemetry sink topic. The DashLog plugin in every agent publishes
# tool_start / tool_end / model_start / agent_end events here. There is
# intentionally no push subscription -- telemetry is fire-and-forget; only
# pull subscribers (admin dashboard, future log aggregators) consume it.
# Without this topic explicitly provisioned, agents auto-create it on
# first publish (which works) but the deploy.py wires PUBSUB_TOPIC_ID to
# the orchestration topic by mistake, flooding the gateway's Wrapper-proto
# parser with telemetry-shaped JSON and 400-rejecting every message.
resource "google_pubsub_topic" "agent_telemetry" {
  name    = "agent-telemetry"
  project = var.project_id

  labels = {
    purpose = "agent-telemetry"
  }
}

# Metering sink topic. The Phase 4 token-usage callback in each agent
# publishes one event per model call here when METERING_SINK=pubsub.
# A separate topic from agent-telemetry because the consumer is the
# usage-rollup drainer (writes monthly_usage / agent_breakdown rows),
# not the dash, and we want independent retention/ack semantics.
#
# Always provisioned (cheap, ~$0/mo when idle) so the toggle from
# stdout/sqlite -> pubsub is a single env-var flip, not a Terraform
# round-trip. Producers that publish before the toggle just get a
# no-op auto-create which is harmless.
resource "google_pubsub_topic" "usage_metering" {
  name    = "usage-metering"
  project = var.project_id

  labels = {
    purpose = "usage-metering"
  }

  # Drop oldest events after 7 days. The drainer commits each event
  # to Postgres synchronously, so a 7-day backlog means something is
  # very wrong with the drainer; we'd rather lose old metering than
  # bill operators for an unbounded Pub/Sub backlog.
  message_retention_duration = "604800s"
}

# ---------------------------------------------------------------------------
# Metering dead-letter topic + subscription (Slice C: drainer hardening)
#
# Without a DLQ, a single poisoned message (e.g. malformed JSON from a
# stale producer) loops forever: the drainer Nacks, Pub/Sub redelivers
# after the backoff, drainer Nacks again, ad infinitum. The redelivery
# storm masks healthy traffic, inflates Pub/Sub backlog metrics, and
# (because METERING_SINK is the source of truth for usage billing)
# eventually causes Stripe usage-record gaps that look like fraud.
#
# With this DLQ wired:
#   - 5 nacks => message moves to usage-metering-dlq (preserved 7 days)
#   - retry_policy throttles redelivery so a Postgres hiccup doesn't
#     hammer the drainer at 1 RPS
#   - ack_deadline 60s covers the worst-case Postgres write latency
#     (median <10ms, p99 ~200ms in our load tests). 10s default would
#     cause spurious redeliveries on the slow tail.
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "usage_metering_dlq" {
  name    = "usage-metering-dlq"
  project = var.project_id

  labels = {
    purpose = "usage-metering-dlq"
  }

  # Hold DLQ messages for 7 days so on-call has a week to triage
  # before they're auto-purged. Matches the main topic's retention.
  message_retention_duration = "604800s"
}

# Pull subscription the cmd/gateway Pub/Sub drainer reads from when
# METERING_SINK=pubsub. ID matches the env-default the gateway uses
# (METERING_PUBSUB_SUBSCRIPTION, defaults to "usage-metering-sub").
#
# Without this resource, the gateway boots in cloud and silently
# no-ops the drainer because pubsub.NewClient(...).Subscriber(id)
# returns a handle to a nonexistent subscription; Receive() then
# fails with NotFound on first call and the drainer goroutine
# exits. Explicitly provisioning the subscription is the actual
# bug fix \u2014 the DLQ + retry tuning is the hardening on top.
resource "google_pubsub_subscription" "usage_metering" {
  name    = "usage-metering-sub"
  topic   = google_pubsub_topic.usage_metering.name
  project = var.project_id

  labels = {
    purpose = "usage-metering-drainer"
  }

  # 60s is generous for a single Postgres INSERT but covers the p99
  # tail (~200ms observed) without forcing the drainer to use the
  # modack extension dance. Pub/Sub max is 600s; we don't need it.
  ack_deadline_seconds = 60

  # 7 days. Matches the topic. Beyond this, undelivered messages are
  # dropped (which is fine: we'd rather lose a week of metering than
  # accrue Pub/Sub charges for an unbounded backlog).
  message_retention_duration = "604800s"

  retry_policy {
    # 10s minimum stops a Stripe rate-limit storm from re-hitting
    # the upstream at full pull-RPS the moment the limit lifts.
    minimum_backoff = "10s"
    # 600s maximum (Pub/Sub max for retry_policy) caps the
    # worst-case redelivery interval during a sustained outage.
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.usage_metering_dlq.id
    max_delivery_attempts = 5
  }

  # Explicit dependency: the DLQ topic must exist AND the Pub/Sub
  # service agent must hold pubsub.publisher on it BEFORE this
  # subscription is created, otherwise the API rejects with
  # `FAILED_PRECONDITION: dead-letter topic ... has no publisher
  # permissions for the Pub/Sub service account`.
  #
  # The reverse binding (Pub/Sub SA needs subscriber on THIS
  # subscription so it can ack after a DLQ forward) is created
  # below \u2014 it references this resource's .name so Terraform
  # orders it after creation. That ordering is correct: the
  # subscriber grant is only required at delivery time, not at
  # subscription-creation time.
  depends_on = [
    google_pubsub_topic_iam_member.pubsub_sa_dlq_publisher,
  ]
}

# Pull subscription on the DLQ itself so operators can inspect
# poisoned messages with `gcloud pubsub subscriptions pull`. No
# drainer attached \u2014 manual triage only.
resource "google_pubsub_subscription" "usage_metering_dlq" {
  name    = "usage-metering-dlq-sub"
  topic   = google_pubsub_topic.usage_metering_dlq.name
  project = var.project_id

  labels = {
    purpose = "usage-metering-dlq-inspection"
  }

  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"
}

# --- IAM for DLQ delivery ---
# GCP requires the Pub/Sub service agent to hold:
#   1. roles/pubsub.publisher on the DLQ topic (to forward dead messages)
#   2. roles/pubsub.subscriber on the source subscription (to ack the
#      original message after the forward succeeds)
# Without BOTH bindings, dead-letter delivery silently fails and the
# poisoned message keeps cycling. The bindings are at the resource
# (topic / subscription) level for least privilege rather than
# project-wide.

resource "google_pubsub_topic_iam_member" "pubsub_sa_dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.usage_metering_dlq.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${var.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.pubsub]
}

resource "google_pubsub_subscription_iam_member" "pubsub_sa_sub_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.usage_metering.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${var.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.pubsub]
}

# --- IAM for the gateway drainer ---
# The compute SA needs subscriber rights to call Pull/Ack. Granted at
# the subscription level rather than project-wide so a future
# misconfigured service can't accidentally consume metering events.
resource "google_pubsub_subscription_iam_member" "compute_sa_metering_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.usage_metering.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.compute_sa_email}"
}

resource "google_pubsub_subscription" "gateway_push_orchestration" {
  name    = "gateway-push-orchestration"
  topic   = google_pubsub_topic.specialist_orchestration.name
  project = var.project_id

  # Use the internal Cloud Run URL to bypass IAP. The IAP-protected custom
  # domain (gateway.<domain_suffix>) requires OIDC audience=IAP_CLIENT_ID,
  # which is unreliable for PubSub push. The internal .run.app URL accepts
  # requests directly with standard OIDC authentication.
  push_config {
    push_endpoint = "https://gateway-${var.project_number}.${var.region}.run.app/api/v1/orchestration/push"

    oidc_token {
      service_account_email = var.compute_sa_email
      audience              = "https://gateway-${var.project_number}.${var.region}.run.app"
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${var.compute_sa_email}"
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${var.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  depends_on = [google_project_service_identity.pubsub]
}
