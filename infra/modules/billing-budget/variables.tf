# Copyright 2026 Christian Haeussinger
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

variable "project_id" {
  description = "Workload project the budget is scoped to."
  type        = string
}

variable "billing_account_id" {
  description = "Billing account id (no `billingAccounts/` prefix; just the bare ID like `01ABCD-234567-EF8901`). Find it with `gcloud beta billing accounts list`."
  type        = string

  validation {
    condition     = length(var.billing_account_id) > 0
    error_message = "billing_account_id must be set when the billing-budget module is enabled. Run `gcloud beta billing accounts list` to find it."
  }
}

variable "display_name" {
  description = "Human-readable budget name shown in the GCP console."
  type        = string
  default     = "Workload project budget"
}

variable "monthly_budget_amount" {
  description = "Monthly budget cap in `currency_code` units (whole units only). Alerts fire at 50/90/100% of this. Forecasted-spend alert also fires at 100%."
  type        = number

  validation {
    condition     = var.monthly_budget_amount > 0
    error_message = "monthly_budget_amount must be a positive integer (whole units of currency_code)."
  }
}

variable "currency_code" {
  description = "ISO-4217 currency code. Must match the billing account's currency \u2014 mismatched currency triggers `INVALID_ARGUMENT: Currency code does not match billing account` at apply time."
  type        = string
  default     = "USD"
}

variable "alert_emails" {
  description = "List of email addresses to notify when a threshold is crossed. Empty list still creates the budget (project Billing Admins always receive notifications) but adds no extra channels. Each email becomes a google_monitoring_notification_channel."
  type        = list(string)
  default     = []
}

variable "current_spend_thresholds" {
  description = "Threshold fractions on CURRENT_SPEND that fire alerts. Default = [0.25, 0.5, 0.9, 1.0]: an early 25% heads-up for the 'left it running over the weekend' case, then escalating notices. Override to e.g. [0.5, 1.0] for a quieter posture. Values must be in (0, 10] (GCP caps at 1000% i.e. 10x)."
  type        = list(number)
  default     = [0.25, 0.5, 0.9, 1.0]

  validation {
    condition     = length(var.current_spend_thresholds) > 0
    error_message = "current_spend_thresholds must have at least one entry; pass [1.0] for a single 100% alert."
  }
  validation {
    condition     = alltrue([for t in var.current_spend_thresholds : t > 0 && t <= 10])
    error_message = "each current_spend_threshold must be in (0, 10]; GCP rejects 0 and caps at 10x (1000%)."
  }
}

variable "forecast_spend_thresholds" {
  description = "Threshold fractions on FORECASTED_SPEND. Default [1.0] fires a heads-up when GCP's month-end projection crosses the budget cap. Empty list disables forecast alerts."
  type        = list(number)
  default     = [1.0]

  validation {
    condition     = alltrue([for t in var.forecast_spend_thresholds : t > 0 && t <= 10])
    error_message = "each forecast_spend_threshold must be in (0, 10]."
  }
}
