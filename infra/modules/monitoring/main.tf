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

resource "google_monitoring_notification_channel" "email" {
  display_name = "${title(var.environment)} Team Email"
  type         = "email"
  project      = var.project_id

  labels = {
    email_address = var.alert_email
  }
}

resource "google_monitoring_alert_policy" "redis_memory" {
  display_name = "Redis Memory Pressure (>80%)"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Memory usage ratio exceeds 80%"
    condition_threshold {
      filter          = "resource.type = \"redis_instance\" AND metric.type = \"redis.googleapis.com/stats/memory/usage_ratio\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "300s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "Redis memory usage has exceeded 80%. Check for memory leaks or excessive key growth. Triage: run `redis-cli INFO memory` on the instance and review `used_memory_peak` vs `maxmemory`. Consider flushing stale keys or scaling the instance tier."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

resource "google_monitoring_uptime_check_config" "gateway_health" {
  count = var.domain_suffix != "" ? 1 : 0

  display_name = "Gateway Health (${var.environment})"
  project      = var.project_id
  timeout      = "10s"
  period       = "60s"

  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = "gateway.${var.domain_suffix}"
    }
  }
}

resource "google_monitoring_alert_policy" "gateway_uptime" {
  count = var.domain_suffix != "" ? 1 : 0

  display_name = "Gateway Unreachable (${var.environment})"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Gateway uptime check failing"
    condition_threshold {
      filter          = "resource.type = \"uptime_url\" AND metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.labels.check_id = \"${google_monitoring_uptime_check_config.gateway_health[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "120s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.project_id"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "The gateway health check is failing. This likely indicates VPC connectivity issues (e.g., Redis unreachable) or a service crash. Triage: check gateway Cloud Run logs for Redis connection errors, verify VPC subnet and NAT health."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

resource "google_monitoring_alert_policy" "nat_egress_zero" {
  display_name = "Cloud NAT Egress Zero (${var.environment})"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "NAT sent bytes dropped to zero"
    condition_absent {
      filter   = "resource.type = \"nat_gateway\" AND metric.type = \"router.googleapis.com/nat/sent_bytes_count\""
      duration = "600s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_RATE"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "Cloud NAT has reported zero egress bytes for 10 minutes. This may indicate a VPC networking failure preventing Cloud Run services from reaching external endpoints or other Cloud Run services via .run.app URLs. Triage: check Cloud NAT status, verify VPC routes, and inspect Cloud Run service logs for connection timeouts."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

resource "google_monitoring_alert_policy" "redis_connections" {
  display_name = "Redis High Connection Count (>8000)"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Connected clients exceeds 8000"
    condition_threshold {
      filter          = "resource.type = \"redis_instance\" AND metric.type = \"redis.googleapis.com/clients/connected\""
      comparison      = "COMPARISON_GT"
      threshold_value = 8000
      duration        = "300s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "Redis connected clients have exceeded 8000 (80% of the 10,000-connection design target). Triage: check for connection leaks in application services using `redis-cli CLIENT LIST`. Review gateway and agent connection pooling settings. If legitimate load, consider scaling the Redis tier."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# =============================================================================
# E3c: operational alerting for the production deploy.
#
# Reuses google_monitoring_notification_channel.email above (the same
# channel the Redis/NAT alerts already fire on). The spec also
# mentions the billing-budget channels, but those only exist when
# enable_billing_budget=true; cross-module fan-out would force an
# ordering dependency for no real benefit (the email is the same
# operator either way). Keeping a single channel keeps triage simple.
#
# Every alert uses auto_close=1800s so a self-healing transient
# (e.g. one bad deploy, NEG warmup) doesn't keep paging after it
# clears.
# =============================================================================

# --- Gateway 5xx rate ---------------------------------------------------
#
# Counts Cloud Run requests with response_code_class=5xx, divided by
# total request count, alerted when the ratio exceeds 5% sustained
# for 5 minutes. Catches both 500s from app panics and 503s from
# Cloud Run rejecting overflow (max_instance_request_concurrency
# tripped). At OSS-sizing (max_instances=1, concurrency=200) the
# 503 path is the more likely fault.
#
# Uses Monitoring Query Language because the ratio of two
# request_count time series can't be expressed with a single
# condition_threshold filter \u2014 we need a division.
resource "google_monitoring_alert_policy" "gateway_5xx_rate" {
  display_name = "Gateway 5xx Rate >5% (${var.environment})"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Gateway 5xx ratio exceeds 5% over 5m"
    condition_monitoring_query_language {
      query    = <<-EOT
        fetch cloud_run_revision
        | metric 'run.googleapis.com/request_count'
        | filter (resource.service_name == 'gateway')
        | { t_5xx:
              filter (metric.response_code_class == '5xx')
              | align rate(5m)
              | group_by [], [sum(value.request_count)]
          ;
            t_all:
              align rate(5m)
              | group_by [], [sum(value.request_count)]
          }
        | ratio
        | condition val() > 0.05
      EOT
      duration = "300s"
      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "Gateway has been returning 5xx for more than 5% of requests over 5 minutes. Triage: check gateway Cloud Run logs for panics, verify Redis + Cloud SQL connectivity, and look for `max_instance_request_concurrency` saturation in the cloud_run_revision metric (which would 503 with no app-level log line)."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# --- Cloud Run instance saturation (REMOVED 2026-06-22) ----------------
#
# The previous `cloudrun_instances_saturated` policy thresholded the
# Cloud Run instance_count metric at COMPARISON_GT 0 over a 10m
# window with the intent of catching "service running at its
# max_instances ceiling". Under OSS sizing every service caps at
# max_instances=1, so "at ceiling" == "at least one warm instance"
# == "any traffic served in the last 10 minutes". The rule fired
# on every warm service of every deploy — pure noise, no signal,
# producing the "Cloud Run Revision - Instance Count is above
# threshold of 0 with a value of 1" email flood.
#
# The live policy
# (projects/<id>/alertPolicies/17671191073877869405) was disabled
# via `gcloud alpha monitoring policies update --no-enabled` on
# 2026-06-22 to stop the spam immediately; this terraform deletion
# makes the removal permanent the next time the monitoring module
# is applied. Replacement saturation signal (latency / 503 rate /
# request queue depth) is a separate piece of work; an alert is
# better silent than constantly crying wolf in the meantime.

# --- Cloud SQL CPU pressure ---------------------------------------------
#
# Cloud SQL CPU >80% sustained for 10 minutes is the canonical
# "queries are slow or about to be slow" signal. Matches whatever
# Cloud SQL instance the cloud-sql-postgres module provisioned;
# the filter is intentionally not scoped to a specific
# database_id so AlloyDB deploys (which use a different filter)
# silently no-op without an apply error.
resource "google_monitoring_alert_policy" "cloudsql_cpu" {
  display_name = "Cloud SQL CPU >80% (${var.environment})"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "Cloud SQL CPU utilization >80% for 10m"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/cpu/utilization\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "600s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "Cloud SQL CPU has stayed above 80% for 10 minutes. Triage: identify hot queries via `pg_stat_statements`, check for missing indexes (the planner-with-memory tools occasionally surface seq-scan offenders), and consider scaling the instance tier. If the regression is recent, check the latest deploy for a new N+1 query path."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# --- Pub/Sub stuck dispatch ----------------------------------------------
#
# oldest_unacked_message_age > 600s means a subscription has had a
# message sitting unacked for >10 minutes. For the orchestration
# topic this almost always means the gateway crashed or the agent
# behind a push subscription is OOM-killing on every delivery. The
# metric covers every subscription in the project; the alert
# groups by subscription_id so triage starts with "which queue is
# stuck".
resource "google_monitoring_alert_policy" "pubsub_unacked_age" {
  display_name = "Pub/Sub Unacked Message Age >10m (${var.environment})"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "oldest_unacked_message_age > 600s for 5m"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 600
      duration        = "300s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["resource.label.subscription_id"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "A Pub/Sub subscription has a message stuck unacked for >10 minutes. Triage: identify the `subscription_id` from the metric label. For the orchestration topic this is usually a gateway crash or an agent OOM-killing on every delivery; check the corresponding Cloud Run service logs and look for NACK loops. For the metering topic it means the billing service can't keep up \u2014 inspect cmd/billing logs and the database health alert."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# --- Manual user-approval signup queue (2026-06-26) ---------------------
#
# Every fresh /me/init that lands a row in StatusPending logs a
# structured "AUDIT signup_pending user_id=... email=..." line via
# the gateway's stdlib logger. The log-based metric below counts
# those lines; the alert policy fans them out to the operator's
# existing email channel so a superadmin is told that there is a
# new account in the queue waiting for review. Threshold is 0 over
# a 60s window — every new pending row should fire exactly once.
#
# A separate field-extractor for `email` would let the email body
# show which account triggered the alert, but the log entry itself
# is one click away from the alert email and that's good enough
# given the low expected frequency (handful per day at most).
resource "google_logging_metric" "gateway_signup_pending" {
  name        = "gateway/signup_pending"
  project     = var.project_id
  description = "Counts /me/init handoffs that landed in the pending-approval queue."

  filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="gateway"
    textPayload:"AUDIT signup_pending"
  EOT

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "Signups awaiting approval"
  }
}

resource "google_monitoring_alert_policy" "signup_pending" {
  display_name = "Signup awaiting approval (${var.environment})"
  project      = var.project_id
  combiner     = "OR"

  conditions {
    display_name = "New /me/init landed in pending queue"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.gateway_signup_pending.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.name]

  documentation {
    content   = "A new user signed up and is now in the pending-approval queue. Triage: open the admin dashboard (Users tab), filter by `Pending only`, and click `Approve` or `Reject`. The user is signed in but blocked by the gateway from every protected route until you act."
    mime_type = "text/markdown"
  }

  alert_strategy {
    # Auto-close fast — the alert is informational and an operator
    # who hasn't approved in 30 minutes will get a fresh fire next
    # time the metric value changes.
    auto_close = "1800s"
  }
}
