# Copyright 2026 The Confidently Wrong Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

# Lock in the shape of the billing budget so a future refactor cannot
# silently drop a threshold or break the email-channel wiring.
# All runs use `command = plan` so no real GCP API call happens.

# Mock the data source the module uses to expand `project_id` to the
# project NUMBER required by budget_filter. Only the computed `number`
# attribute can be overridden via mock_data; `project_id` is an input
# argument so it flows through from the variable.
mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "123456789012"
    }
  }
}

variables {
  project_id            = "demo-project"
  billing_account_id    = "01ABCD-234567-EF8901"
  monthly_budget_amount = 100
  currency_code         = "USD"
  alert_emails          = []
}

# ------ Shape: defaults emit 4 CURRENT + 1 FORECASTED threshold ------
run "default_thresholds_are_25_50_90_100_plus_forecast_100" {
  command = plan

  assert {
    condition     = length(google_billing_budget.project.threshold_rules) == 5
    error_message = "expected 5 threshold rules with defaults (4 CURRENT + 1 FORECASTED)"
  }
}

# ------ Custom thresholds are honoured ------
run "operator_can_silence_to_single_100_percent_alert" {
  command = plan

  variables {
    project_id                 = "demo-project"
    billing_account_id         = "01ABCD-234567-EF8901"
    monthly_budget_amount      = 100
    current_spend_thresholds   = [1.0]
    forecast_spend_thresholds  = []
  }

  assert {
    condition     = length(google_billing_budget.project.threshold_rules) == 1
    error_message = "expected a single 100% CURRENT threshold when operator overrides both lists"
  }
}

# ------ Validation: empty current_spend_thresholds rejected ------
run "rejects_empty_current_spend_thresholds" {
  command = plan

  variables {
    project_id               = "demo-project"
    billing_account_id       = "01ABCD-234567-EF8901"
    monthly_budget_amount    = 100
    current_spend_thresholds = []
  }

  expect_failures = [var.current_spend_thresholds]
}

# ------ Validation: out-of-range threshold rejected ------
run "rejects_threshold_above_10x" {
  command = plan

  variables {
    project_id               = "demo-project"
    billing_account_id       = "01ABCD-234567-EF8901"
    monthly_budget_amount    = 100
    current_spend_thresholds = [0.5, 11.0] # 11x > GCP cap of 10x
  }

  expect_failures = [var.current_spend_thresholds]
}

# ------ Validation: zero threshold rejected ------
run "rejects_threshold_at_zero" {
  command = plan

  variables {
    project_id               = "demo-project"
    billing_account_id       = "01ABCD-234567-EF8901"
    monthly_budget_amount    = 100
    current_spend_thresholds = [0, 0.5]
  }

  expect_failures = [var.current_spend_thresholds]
}

# ------ Validation: empty billing_account_id rejected ------
run "rejects_empty_billing_account_id" {
  command = plan

  variables {
    project_id            = "demo-project"
    billing_account_id    = ""
    monthly_budget_amount = 100
  }

  expect_failures = [var.billing_account_id]
}

# ------ Validation: monthly_budget_amount must be positive ------
run "rejects_zero_monthly_budget_amount" {
  command = plan

  variables {
    project_id            = "demo-project"
    billing_account_id    = "01ABCD-234567-EF8901"
    monthly_budget_amount = 0
  }

  expect_failures = [var.monthly_budget_amount]
}

# ------ Emails create one notification channel each ------
run "creates_one_channel_per_alert_email" {
  command = plan

  variables {
    project_id            = "demo-project"
    billing_account_id    = "01ABCD-234567-EF8901"
    monthly_budget_amount = 100
    alert_emails = [
      "ops@example.com",
      "finance@example.com",
    ]
  }

  assert {
    condition     = length(google_monitoring_notification_channel.email) == 2
    error_message = "expected one google_monitoring_notification_channel per alert email"
  }
}

# ------ Budget always includes the IAM-default recipients ------
run "default_iam_recipients_stay_enabled_so_billing_admins_get_notified" {
  command = plan

  variables {
    project_id            = "demo-project"
    billing_account_id    = "01ABCD-234567-EF8901"
    monthly_budget_amount = 100
    alert_emails          = ["ops@example.com"]
  }

  assert {
    condition     = google_billing_budget.project.all_updates_rule[0].disable_default_iam_recipients == false
    error_message = "disable_default_iam_recipients must remain false so project Billing Admins still receive alerts"
  }
}
