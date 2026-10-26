# Copyright 2026 Christian Haeussinger
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

output "budget_id" {
  description = "Fully-qualified resource id of the billing budget."
  value       = google_billing_budget.project.id
}

output "budget_name" {
  description = "Server-assigned budget name (billingAccounts/.../budgets/...)."
  value       = google_billing_budget.project.name
}

output "notification_channel_ids" {
  description = "Resource ids of the per-email notification channels."
  value       = [for c in google_monitoring_notification_channel.email : c.id]
}
