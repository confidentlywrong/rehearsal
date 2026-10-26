# Copyright 2026 Christian Haeussinger
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

# ---------------------------------------------------------------------------
# billing-budget: GCP-level cost cap for the workload project.
#
# Sends email notifications at 50%, 90%, and 100% of the monthly budget.
# This is the cheap, no-code line of defence against "I left my SaaS
# running over the weekend and burned through $500 of Vertex calls".
#
# It does NOT auto-shutdown anything. GCP billing budgets are advisory
# only \u2014 turning them into an enforcement action would require a
# Pub/Sub-subscribed Cloud Function that disables billing on the
# project (which permanently breaks every running service). That
# trade-off is intentionally left out of this module: the simple email
# alert wins for "stable, simple, effective".
# ---------------------------------------------------------------------------

data "google_project" "current" {
  project_id = var.project_id
}

# One notification channel per alert email. monitoring API must be
# enabled, which it is in base_apis.
resource "google_monitoring_notification_channel" "email" {
  for_each = toset(var.alert_emails)

  project      = var.project_id
  display_name = "Budget alert: ${each.key}"
  type         = "email"

  labels = {
    email_address = each.key
  }
}

resource "google_billing_budget" "project" {
  billing_account = var.billing_account_id
  display_name    = var.display_name

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
    # Empty credit_types_treatment uses the GCP default
    # (INCLUDE_ALL_CREDITS) which subtracts free-tier credits before
    # comparing against the threshold \u2014 i.e. the alert fires on
    # real out-of-pocket spend.
  }

  amount {
    specified_amount {
      currency_code = var.currency_code
      units         = tostring(var.monthly_budget_amount)
    }
  }

  # Threshold rules. Both lists are operator-tunable via variables
  # so a noisy 25% alert can be silenced without forking the module.
  # The default include set is [0.25, 0.5, 0.9, 1.0] CURRENT + [1.0]
  # FORECASTED — see variables.tf for rationale.
  dynamic "threshold_rules" {
    for_each = var.current_spend_thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }
  dynamic "threshold_rules" {
    for_each = var.forecast_spend_thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "FORECASTED_SPEND"
    }
  }

  dynamic "all_updates_rule" {
    for_each = length(var.alert_emails) > 0 ? [1] : []
    content {
      monitoring_notification_channels = [
        for c in google_monitoring_notification_channel.email : c.id
      ]
      # Leave default IAM recipients (project Billing Admins) enabled
      # so the project owner also gets a copy regardless of what
      # custom emails are configured.
      disable_default_iam_recipients = false
    }
  }
}
