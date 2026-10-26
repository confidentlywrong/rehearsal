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

# =============================================================================
# Core Outputs
# =============================================================================

output "project_id" {
  description = "GCP project ID"
  value       = var.project_id
}

output "region" {
  description = "GCP region"
  value       = var.region
}

output "project_number" {
  description = "GCP project number"
  value       = module.project_apis.project_number
}

output "artifact_registry_url" {
  description = "Artifact Registry URL for Docker images"
  value       = module.project_apis.artifact_registry_url
}

# =============================================================================
# Networking
# =============================================================================

output "vpc_id" {
  description = "VPC network ID"
  value       = module.networking.vpc_id
}

output "vpc_name" {
  description = "VPC network name"
  value       = module.networking.vpc_name
}

output "vpc_subnet" {
  description = "Serverless subnet name for Cloud Run direct VPC egress"
  value       = module.networking.serverless_subnet_name
}

# =============================================================================
# Redis
# =============================================================================

output "redis_host" {
  description = "Redis instance host"
  value       = module.redis.host
}

output "redis_port" {
  description = "Redis instance port"
  value       = module.redis.port
}

# =============================================================================
# Database
# =============================================================================

output "database_type" {
  description = "Database type in use (cloud-sql or alloydb)"
  value       = var.enable_alloydb ? "alloydb" : "cloud-sql"
}

output "database_ip" {
  description = "Database private IP address"
  value = var.enable_alloydb ? (
    length(module.alloydb) > 0 ? module.alloydb[0].ip_address : null
    ) : (
    length(module.cloud_sql_postgres) > 0 ? module.cloud_sql_postgres[0].private_ip_address : null
  )
}

output "database_connection_name" {
  description = "Cloud SQL connection name (only for Cloud SQL)"
  value       = !var.enable_alloydb && length(module.cloud_sql_postgres) > 0 ? module.cloud_sql_postgres[0].connection_name : null
}

output "database_password_secret_id" {
  description = "Secret Manager secret ID for database password"
  value = var.enable_alloydb ? (
    length(module.alloydb) > 0 ? module.alloydb[0].password_secret_id : null
    ) : (
    length(module.cloud_sql_postgres) > 0 ? module.cloud_sql_postgres[0].password_secret_id : null
  )
}

output "database_name" {
  description = "Database name within the instance (Cloud SQL: 'agent_memory'; AlloyDB: 'postgres')"
  value = var.enable_alloydb ? (
    length(module.alloydb) > 0 ? "postgres" : null
    ) : (
    length(module.cloud_sql_postgres) > 0 ? module.cloud_sql_postgres[0].database_name : null
  )
}

# =============================================================================
# Pub/Sub
# =============================================================================

output "pubsub_topic" {
  description = "Pub/Sub orchestration topic name (legacy alias; same as orchestration_topic)"
  value       = module.pubsub.topic_name
}

output "orchestration_topic" {
  description = "Pub/Sub topic for cross-agent orchestration (gateway parses these as Wrapper proto)"
  value       = module.pubsub.topic_name
}

output "telemetry_topic" {
  description = "Pub/Sub topic for fire-and-forget agent telemetry (DashLog plugin publishes here; no push subscriber)"
  value       = module.pubsub.telemetry_topic_name
}

# =============================================================================
# IAM
# =============================================================================

output "compute_sa_email" {
  description = "Compute service account email"
  value       = module.iam.compute_sa_email
}

output "agent_engine_sa_email" {
  description = "Agent Engine service account email"
  value       = module.iam.agent_engine_sa_email
}

# =============================================================================
# Agent Engine
# =============================================================================

output "staging_bucket" {
  description = "GCS staging bucket for Agent Engine deployments"
  value       = "gs://${module.project_apis.staging_bucket}"
}

output "psc_network_attachment" {
  description = "PSC network attachment for Agent Engine VPC connectivity"
  value       = module.networking.psc_network_attachment
}

# =============================================================================
# Optional: GKE
# =============================================================================

output "gke_cluster_name" {
  description = "GKE cluster name (when enabled)"
  value       = var.enable_gke && length(module.gke_runner) > 0 ? "runner-cluster" : null
}

# =============================================================================
# Feature Toggles (for downstream tooling)
# =============================================================================

output "features" {
  description = "Map of enabled features"
  value = {
    alloydb         = var.enable_alloydb
    gke             = var.enable_gke
    runner_cloudrun = var.enable_runner_cloudrun
    maps_api_key    = var.enable_maps_api_key
    monitoring      = var.enable_monitoring
    services        = var.enable_services
    billing_service = var.enable_billing_service
    stripe_billing  = var.enable_stripe_billing
  }
}

# =============================================================================
# Cloud Run services (only populated when enable_services=true)
# =============================================================================

output "service_urls" {
  description = "Map of Cloud Run service name -> .run.app URL. Empty when enable_services=false."
  value       = var.enable_services ? module.cloud_run_services[0].service_urls : {}
}

output "service_names" {
  description = "Map of Cloud Run service key -> deployed name. Empty when enable_services=false."
  value       = var.enable_services ? module.cloud_run_services[0].service_names : {}
}

output "gateway_url" {
  description = "Gateway .run.app URL. Empty string when enable_services=false."
  value       = var.enable_services ? module.cloud_run_services[0].gateway_url : ""
}

# =============================================================================
# Phase 11: SaaS billing (only populated when enable_billing_service=true)
# =============================================================================

output "billing_url" {
  description = "cmd/billing Cloud Run service URL. Empty when enable_billing_service=false."
  value       = var.enable_services && var.enable_billing_service ? module.cloud_run_services[0].billing_url : ""
}

output "stripe_webhook_url" {
  description = "Full webhook endpoint to paste into the Stripe Dashboard (billing_url + '/webhook'). Empty when billing service disabled."
  value       = var.enable_services && var.enable_billing_service ? module.cloud_run_services[0].stripe_webhook_url : ""
}

output "metering_topic" {
  description = "Pub/Sub topic name where the gateway/admin/billing services publish token-usage events when METERING_SINK=pubsub."
  value       = module.pubsub.metering_topic_name
}

output "stripe_secret_ids" {
  description = "Map of Stripe Secret Manager secret IDs (empty when enable_stripe_billing=false). Operator populates them via `gcloud secrets versions add <id> --data-file=-` after apply."
  value = var.enable_stripe_billing ? {
    api_key        = google_secret_manager_secret.stripe_api_key[0].secret_id
    webhook_secret = google_secret_manager_secret.stripe_webhook_secret[0].secret_id
  } : {}
}

output "saas_bootstrap_message" {
  description = "Reminder message printed after apply. Includes the post-apply gcloud commands needed to populate Stripe secrets when enable_stripe_billing=true."
  value = join("\n", compact([
    "Post-apply checklist:",
    var.enable_stripe_billing ? "  1. gcloud secrets versions add stripe-api-key --data-file=- --project=${var.project_id}" : "",
    var.enable_stripe_billing ? "  2. gcloud secrets versions add stripe-webhook-secret --data-file=- --project=${var.project_id}" : "",
    var.enable_billing_service ? "  3. Copy stripe_webhook_url into Stripe Dashboard \u2192 Developers \u2192 Webhooks \u2192 Add endpoint" : "",
    "  4. Promote the first superadmin manually via SQL UPDATE on users.is_superadmin (env-var bootstrap was removed 2026-06-23 to close the DR-time re-promotion risk).",
  ]))
}

# =============================================================================
# E3a: Custom domain + HTTPS LB (only populated when var.domain_names is non-empty)
# =============================================================================

output "loadbalancer_ip" {
  description = "Reserved global IP for the HTTPS LB. Operator points DNS A records at this value (or sets var.dns_zone_name so the module does it automatically). Empty string when var.domain_names is empty (LB module not instantiated)."
  value       = var.enable_services && length(var.domain_names) > 0 ? module.loadbalancer[0].lb_ip : ""
}

output "loadbalancer_service_urls" {
  description = "Map of service key -> https://<fqdn> for services routed through the LB. Services not listed in var.domain_names keep their *.run.app URL in `service_urls`. Empty map when the LB module is not instantiated."
  value       = var.enable_services && length(var.domain_names) > 0 ? module.loadbalancer[0].service_urls : {}
}

output "loadbalancer_ssl_certificate" {
  description = "Managed SSL certificate resource name. Watch provisioning status with `gcloud compute ssl-certificates describe <name> --global` \u2014 the cert stays in PROVISIONING until DNS resolves for every domain. Empty when LB module not instantiated."
  value       = var.enable_services && length(var.domain_names) > 0 ? module.loadbalancer[0].ssl_certificate_name : ""
}

output "loadbalancer_post_deploy" {
  description = "Reminder text printed after a custom-domain apply. Lists the DNS step (if not automated via dns_zone_name) and the cert-watch command."
  value = (var.enable_services && length(var.domain_names) > 0) ? join("\n", compact([
    "HTTPS Load Balancer provisioned. Post-apply checklist:",
    "  1. LB IP: <see `terraform output loadbalancer_ip`>",
    var.dns_zone_name == "" ? "  2. Point DNS A records at the LB IP for: ${join(", ", values(var.domain_names))}" : "  2. DNS A records auto-published in zone ${var.dns_zone_name}.",
    "  3. Wait 5-60 min for the managed SSL cert to leave PROVISIONING:",
    "     gcloud compute ssl-certificates describe <see `terraform output loadbalancer_ssl_certificate`> --global --project=${var.project_id}",
    "  4. CORS allow-list automatically locked to: ${join(",", [for fqdn in values(var.domain_names) : "https://${fqdn}"])}",
  ])) : "Custom-domain LB disabled \u2014 set var.domain_names to enable."
}
