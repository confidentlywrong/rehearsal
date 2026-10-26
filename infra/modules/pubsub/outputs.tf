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

output "topic_id" {
  value = google_pubsub_topic.specialist_orchestration.id
}

output "topic_name" {
  value = google_pubsub_topic.specialist_orchestration.name
}

output "subscription_id" {
  value = google_pubsub_subscription.gateway_push_orchestration.id
}

output "telemetry_topic_id" {
  value = google_pubsub_topic.agent_telemetry.id
}

output "telemetry_topic_name" {
  value = google_pubsub_topic.agent_telemetry.name
}

output "metering_topic_id" {
  description = "Full ID of the usage-metering topic (Phase 4 token-usage events). Consumed by the gateway's rollup drainer when METERING_SINK=pubsub."
  value       = google_pubsub_topic.usage_metering.id
}

output "metering_topic_name" {
  description = "Short name of the usage-metering topic. Wire into METERING_TOPIC env on the gateway/admin services when METERING_SINK=pubsub."
  value       = google_pubsub_topic.usage_metering.name
}

output "metering_subscription_name" {
  description = "Short name of the pull subscription consumed by the cmd/gateway Pub/Sub drainer. Matches the gateway's METERING_PUBSUB_SUBSCRIPTION env default."
  value       = google_pubsub_subscription.usage_metering.name
}

output "metering_dlq_topic_name" {
  description = "Short name of the dead-letter topic for the metering subscription. Inspect with `gcloud pubsub subscriptions pull usage-metering-dlq-sub`."
  value       = google_pubsub_topic.usage_metering_dlq.name
}

output "metering_dlq_subscription_name" {
  description = "Pull subscription on the metering DLQ. Operators use this to triage poisoned messages."
  value       = google_pubsub_subscription.usage_metering_dlq.name
}
