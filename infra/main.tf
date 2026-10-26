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

# =============================================================================
# OSS Project -- Lightweight Single-Project Deployment
# =============================================================================
#
# This root module composes a minimal Race Condition deployment suitable for
# individual GCP projects. It reuses existing modules with smaller sizing
# defaults and feature toggles for optional components.
#
# Required:  project_id
# Optional:  enable_alloydb, enable_gke, enable_runner_cloudrun,
#            enable_maps_api_key, enable_monitoring
# =============================================================================

provider "google" {
  project = var.project_id
  region  = var.region
  # Tag every labelable resource (Cloud Run, Cloud SQL, Redis, Pub/Sub, etc.)
  # for cost attribution and demo-asset cleanup. Per-resource `labels` blocks
  # in modules (e.g. pubsub topics with `purpose=`) merge with these; the
  # combined set is exposed by the provider as `effective_labels`.
  default_labels = var.labels

  # Bill quota for "request the API enabled?"-style preflight calls
  # (Firebase, IAP, Identity Toolkit, ...) to the project being
  # provisioned rather than the Cloud SDK's well-known OAuth client
  # project (32555940559). Without this, applying `google_firebase_*`
  # resources with a user-credential ADC fails with SERVICE_DISABLED
  # against the SDK project even though firebase.googleapis.com is
  # enabled on our project. Inside Cloud Build the runner SA already
  # has its own quota project so this flag is a no-op there.
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project        = var.project_id
  region         = var.region
  default_labels = var.labels

  # Same rationale as the google provider above. google_firebase_*
  # resources live under google-beta so this flag is the one that
  # actually fixes the Firebase apply.
  user_project_override = true
  billing_project       = var.project_id
}

locals {
  # Build the API list dynamically based on feature toggles
  base_apis = [
    "agentregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "apikeys.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "generativelanguage.googleapis.com",
    "modelarmor.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "redis.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "vpcaccess.googleapis.com",
  ]

  cloudsql_apis = var.enable_alloydb ? [] : ["sqladmin.googleapis.com"]
  alloydb_apis  = var.enable_alloydb ? ["alloydb.googleapis.com"] : []
  gke_apis      = var.enable_gke ? ["container.googleapis.com"] : []
  maps_apis = var.enable_maps_api_key ? [
    "mapstools.googleapis.com",
    "places.googleapis.com",
    "weather.googleapis.com",
  ] : []
  budget_apis = var.enable_billing_budget ? ["billingbudgets.googleapis.com"] : []
  # Firebase Management + Identity Toolkit are needed when the frontend
  # serves the Firebase JS SDK to the SPA. identity_provider=local still
  # needs neither.
  firebase_apis = var.identity_provider == "firebase" ? [
    "firebase.googleapis.com",
    "identitytoolkit.googleapis.com",
  ] : []

  all_apis = concat(local.base_apis, local.cloudsql_apis, local.alloydb_apis, local.gke_apis, local.maps_apis, local.budget_apis, local.firebase_apis)
}

# =============================================================================
# Core Infrastructure (always enabled)
# =============================================================================

module "project_apis" {
  source     = "./modules/project-apis"
  project_id = var.project_id
  region     = var.region
  services   = local.all_apis
}

module "networking" {
  source         = "./modules/networking"
  project_id     = var.project_id
  project_number = module.project_apis.project_number
  region         = var.region

  depends_on = [module.project_apis]
}

module "iam" {
  source                = "./modules/iam"
  project_id            = var.project_id
  project_number        = module.project_apis.project_number
  region                = var.region
  backend_writers       = var.developers
  frontend_writers      = []
  maps_mcp_users        = var.enable_maps_api_key ? var.developers : []
  agent_engine_sa_users = var.agent_engine_sa_users

  depends_on = [module.project_apis]
}

module "redis" {
  source                    = "./modules/redis"
  project_id                = var.project_id
  region                    = var.region
  vpc_id                    = module.networking.vpc_id
  private_vpc_connection_id = module.networking.private_vpc_connection_id
  tier                      = "BASIC"
  memory_size_gb            = 1

  depends_on = [module.project_apis]
}

module "pubsub" {
  source           = "./modules/pubsub"
  project_id       = var.project_id
  project_number   = module.project_apis.project_number
  region           = var.region
  compute_sa_email = module.iam.compute_sa_email

  depends_on = [module.project_apis]
}

module "model_armor" {
  source     = "./modules/model-armor"
  project_id = var.project_id

  depends_on = [module.project_apis]
}

# =============================================================================
# Database -- Cloud SQL PostgreSQL (default) or AlloyDB (opt-in)
# =============================================================================

# Generate a strong default DB password when the operator does not supply
# var.db_initial_password. The value lives in TF state, so re-applies are
# idempotent (no password churn). Both database modules persist whichever
# password is chosen into Secret Manager.
resource "random_password" "db_initial" {
  length      = 32
  special     = false # alphanumeric -- safe in cloud-sql-postgres local-exec PGPASSWORD interpolation
  min_lower   = 4
  min_upper   = 4
  min_numeric = 4
}

locals {
  db_password = coalesce(var.db_initial_password, random_password.db_initial.result)
}

module "cloud_sql_postgres" {
  count = var.enable_alloydb ? 0 : 1

  source                    = "./modules/cloud-sql-postgres"
  project_id                = var.project_id
  project_number            = module.project_apis.project_number
  region                    = var.region
  vpc_id                    = module.networking.vpc_id
  private_vpc_connection_id = module.networking.private_vpc_connection_id
  compute_sa                = module.iam.compute_sa
  agent_engine_sa_email     = module.iam.agent_engine_sa_email
  iam_users                 = var.developers
  initial_password          = local.db_password
  deletion_protection       = var.database_deletion_protection

  depends_on = [module.project_apis]
}

module "alloydb" {
  count = var.enable_alloydb ? 1 : 0

  source                    = "./modules/alloydb"
  project_id                = var.project_id
  project_number            = module.project_apis.project_number
  region                    = var.region
  vpc_id                    = module.networking.vpc_id
  private_vpc_connection_id = module.networking.private_vpc_connection_id
  compute_sa                = module.iam.compute_sa
  agent_engine_sa_email     = module.iam.agent_engine_sa_email
  iam_users                 = var.developers
  initial_password          = local.db_password
  cpu_count                 = 2
  deletion_policy           = var.database_deletion_protection ? "DEFAULT" : "FORCE"

  depends_on = [module.project_apis]
}

# =============================================================================
# Optional: project-level billing budget with email alerts
# =============================================================================

module "billing_budget" {
  count = var.enable_billing_budget && var.monthly_budget_amount > 0 ? 1 : 0

  source                = "./modules/billing-budget"
  project_id            = var.project_id
  billing_account_id    = var.billing_account_id
  monthly_budget_amount = var.monthly_budget_amount
  currency_code         = var.budget_currency_code
  alert_emails          = var.budget_alert_emails
  display_name          = "Budget for ${var.project_id}"

  depends_on = [module.project_apis]
}

# =============================================================================
# Optional: GKE for runner_gke (GPU workloads)
# =============================================================================

module "gke_model_serving" {
  count = var.enable_gke ? 1 : 0

  source         = "./modules/gke-model-serving"
  project_id     = var.project_id
  project_number = module.project_apis.project_number
  region         = var.region

  depends_on = [module.project_apis]
}

module "gke_runner" {
  count = var.enable_gke ? 1 : 0

  source               = "./modules/gke-runner"
  project_id           = var.project_id
  region               = var.region
  vpc_id               = module.networking.vpc_id
  vpc_name             = module.networking.vpc_name
  model_storage_bucket = module.gke_model_serving[0].model_storage_bucket
  cpu_min_nodes        = 1
  cpu_max_nodes        = 3
  gpu_min_nodes        = 0
  gpu_max_nodes        = 1

  depends_on = [module.project_apis]
}

# =============================================================================
# Optional: Maps API Key
# =============================================================================

module "api_keys" {
  count = var.enable_maps_api_key ? 1 : 0

  source          = "./modules/api-keys"
  project_id      = var.project_id
  compute_sa      = module.iam.compute_sa
  agent_engine_sa = module.iam.agent_engine_sa
  backend_writers = var.developers
  maps_mcp_users  = var.developers

  depends_on = [module.project_apis]
}

# =============================================================================
# Optional: Firebase (identity_provider == "firebase")
# =============================================================================
# Links the GCP project to Firebase, registers a Web App, and exposes the
# auto-generated apiKey / authDomain so the frontend BFF can serve a
# working Firebase JS SDK config to the SPA. When identity_provider is
# anything else (e.g. "local"), this module is not instantiated and the
# legacy LOCAL-mode login page is served instead.

module "firebase" {
  count = var.identity_provider == "firebase" ? 1 : 0

  source     = "./modules/firebase"
  project_id = var.project_id

  depends_on = [module.project_apis]
}

# =============================================================================
# Optional: Monitoring
# =============================================================================

module "monitoring" {
  count = var.enable_monitoring ? 1 : 0

  source      = "./modules/monitoring"
  project_id  = var.project_id
  environment = "oss"
  alert_email = var.alert_email
  # No domain_suffix -- OSS has no DNS. The monitoring module skips the
  # gateway uptime check when domain_suffix is empty, but still provisions
  # Redis memory, NAT egress, and Redis connection alerts.

  depends_on = [module.project_apis]
}

# =============================================================================
# Cloud Run services + IAM (gated by enable_services)
#
# Two-pass apply pattern: the Phase 1 base-infra apply runs with
# enable_services=false. After image builds finish, the Phase 2 services
# apply runs with enable_services=true and image_tags populated. This
# avoids a chicken-and-egg between Cloud Run services and image tags.
# =============================================================================

locals {
  database_ip = var.enable_alloydb ? (
    length(module.alloydb) > 0 ? module.alloydb[0].ip_address : ""
    ) : (
    length(module.cloud_sql_postgres) > 0 ? module.cloud_sql_postgres[0].private_ip_address : ""
  )

  database_secret_id = var.enable_alloydb ? (
    length(module.alloydb) > 0 ? module.alloydb[0].password_secret_id : ""
    ) : (
    length(module.cloud_sql_postgres) > 0 ? module.cloud_sql_postgres[0].password_secret_id : ""
  )
}

# =============================================================================
# Phase 11: Stripe secret stubs (optional)
#
# Terraform creates the *secret* resource but never the version payload --
# the operator runs `gcloud secrets versions add stripe-api-key --data-file=-`
# after `terraform apply` so the actual sk_live_... / whsec_... values never
# touch the .tfvars file or the Terraform state.
#
# lifecycle.ignore_changes is intentionally NOT set on these resources --
# we want the operator to see drift if they accidentally rename the secret
# in the Console. The *version payload* is unmanaged because no version is
# created here at all.
# =============================================================================

resource "google_secret_manager_secret" "stripe_api_key" {
  count = var.enable_stripe_billing ? 1 : 0

  project   = var.project_id
  secret_id = "stripe-api-key"

  labels = merge(var.labels, {
    purpose = "stripe-api-key"
  })

  replication {
    auto {}
  }

  depends_on = [module.project_apis]
}

resource "google_secret_manager_secret" "stripe_webhook_secret" {
  count = var.enable_stripe_billing ? 1 : 0

  project   = var.project_id
  secret_id = "stripe-webhook-secret"

  labels = merge(var.labels, {
    purpose = "stripe-webhook-secret"
  })

  replication {
    auto {}
  }

  depends_on = [module.project_apis]
}

# Grant the compute SA read access to both Stripe secrets so the
# gateway + billing Cloud Run services can mount them.
resource "google_secret_manager_secret_iam_member" "stripe_api_key_accessor" {
  count = var.enable_stripe_billing ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.stripe_api_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = module.iam.compute_sa
}

resource "google_secret_manager_secret_iam_member" "stripe_webhook_secret_accessor" {
  count = var.enable_stripe_billing ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.stripe_webhook_secret[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = module.iam.compute_sa
}

module "cloud_run_services" {
  count = var.enable_services ? 1 : 0

  source = "./modules/cloud-run-services"

  project_id          = var.project_id
  region              = var.region
  compute_sa_email    = module.iam.compute_sa_email
  vpc_network_name    = module.networking.vpc_name
  vpc_subnet_name     = module.networking.serverless_subnet_name
  image_tags          = var.image_tags
  redis_host          = module.redis.host
  redis_port          = module.redis.port
  database_ip         = local.database_ip
  database_secret_id  = local.database_secret_id
  orchestration_topic = module.pubsub.topic_name
  telemetry_topic     = module.pubsub.telemetry_topic_name
  embedding_backend   = var.embedding_backend
  agent_urls          = var.agent_urls

  # E3a: lock CORS down to the brand domains when an LB+custom
  # domain is configured. Derived directly from var.domain_names
  # (NOT from the loadbalancer module output) so there's no module
  # ordering dependency: cloud_run_services + loadbalancer can
  # apply in either order, and a same-value re-derivation here
  # keeps the CORS allow-list and the LB host-rules in lockstep.
  # Empty var.domain_names => empty CORS string => services keep
  # the legacy `*` behavior, byte-identical to the OSS demo.
  cors_allowed_origins = join(",", [for fqdn in values(var.domain_names) : "https://${fqdn}"])

  # Per-user SaaS wiring. The deployed stack is always per-user;
  # identity_provider=local keeps a single LOCAL_DEV_USER_EMAIL
  # fixture user for OSS-demo style deploys.
  enable_billing_service   = var.enable_billing_service
  identity_provider        = var.identity_provider
  metering_sink            = var.metering_sink
  metering_topic           = module.pubsub.metering_topic_name
  billing_provider         = var.billing_provider
  stripe_api_key_secret_id = var.enable_stripe_billing ? google_secret_manager_secret.stripe_api_key[0].secret_id : ""
  stripe_webhook_secret_id = var.enable_stripe_billing ? google_secret_manager_secret.stripe_webhook_secret[0].secret_id : ""
  stripe_price_map         = var.stripe_price_map
  stripe_return_base_url   = var.stripe_return_base_url

  # Firebase Auth (only effective when identity_provider=firebase;
  # otherwise the module emits no FIREBASE_* env vars and the login
  # page falls back to LOCAL mode).
  #
  # When the Firebase module is active (identity_provider=firebase) we
  # prefer its outputs over any explicit var the operator typed in
  # terraform.tfvars: the module-provisioned values are guaranteed to
  # match the Firebase Web App we just created/imported, while the
  # tfvars values are an escape hatch for shared multi-project setups.
  firebase_project_id  = length(module.firebase) > 0 ? module.firebase[0].project_id : var.firebase_project_id
  firebase_web_api_key = length(module.firebase) > 0 ? module.firebase[0].web_api_key : var.firebase_web_api_key
  firebase_auth_domain = length(module.firebase) > 0 ? module.firebase[0].auth_domain : var.firebase_auth_domain

  # P2.B (2026-06-22 audit): Firebase App Check server-side
  # verification. Default "off" preserves byte-identical legacy
  # behaviour; the operator opts in via terraform.tfvars by setting
  # appcheck_enforce = "report-only" and supplying the numeric
  # firebase_project_number.
  appcheck_enforce        = var.appcheck_enforce
  firebase_project_number = var.firebase_project_number

  # Payment-failed read-only gate (opt-in; default off preserves
  # byte-identical legacy behavior).
  enforce_payment_failed    = var.enforce_payment_failed
  payment_failed_grace_days = var.payment_failed_grace_days

  # Observability + hardening knobs (all default empty/false =
  # legacy byte-identical). Flow into common_env in the module so
  # every Go service sees them at boot.
  csp_enforce                   = var.csp_enforce
  security_hsts                 = var.security_hsts
  log_format                    = var.log_format
  otel_exporter                 = var.otel_exporter
  otel_sample_ratio             = var.otel_sample_ratio
  recaptcha_enterprise_site_key = var.recaptcha_enterprise_site_key
  planner_per_user_isolation    = var.planner_per_user_isolation

  depends_on = [
    module.project_apis,
    module.redis,
    module.pubsub,
    module.networking,
    module.iam,
  ]
}

module "cloud_run_iam" {
  count = var.enable_services ? 1 : 0

  source = "./modules/cloud-run-iam"

  project_id                   = var.project_id
  region                       = var.region
  service_names                = module.cloud_run_services[0].service_names
  agent_engine_sa_email        = module.iam.agent_engine_sa_email
  compute_sa_email             = module.iam.compute_sa_email
  customer_uis_unauthenticated = var.customer_uis_unauthenticated
  operator_principals          = var.operator_principals
}

# =============================================================================
# Firebase Auth — Identity Platform config (sign-in providers + domains)
# =============================================================================
# Lives at top level (not inside module "firebase") so it can reference
# both module.firebase outputs AND module.cloud_run_services outputs
# without forming a dependency cycle. The cloud_run_services module
# only needs the apiKey from module.firebase, not the IDP config — so
# the order is: firebase project/web app → cloud_run_services →
# identity_platform_config (this resource).
#
# Email/Password sign-in does NOT enforce authorized_domains (those
# only apply to OAuth-redirect flows like signInWithPopup), so this
# resource is technically optional for E/P. We still add the Cloud
# Run frontend hostname to authorized_domains so that flipping on
# Google sign-in later (manual one-time OAuth client setup) works
# without an extra apply.
resource "google_identity_platform_config" "default" {
  count    = var.identity_provider == "firebase" && var.enable_services ? 1 : 0
  provider = google-beta
  project  = var.project_id

  # Defaults always include "localhost" and "<project>.firebaseapp.com" /
  # "<project>.web.app"; we extend with the Cloud Run frontend hostname
  # so signInWithPopup() works from that origin too. Custom LB domains
  # (var.domain_names) are added when set.
  authorized_domains = distinct(concat(
    [
      "localhost",
      "${var.project_id}.firebaseapp.com",
      "${var.project_id}.web.app",
      replace(module.cloud_run_services[0].service_urls["frontend"], "https://", ""),
    ],
    [for fqdn in values(var.domain_names) : fqdn],
  ))

  sign_in {
    allow_duplicate_emails = false

    email {
      enabled           = true
      password_required = true # false would enable passwordless email-link
    }
  }

  depends_on = [
    module.firebase,
    module.cloud_run_services,
  ]
}

# =============================================================================
# E3a: External HTTPS Load Balancer (opt-in via var.domain_names)
#
# Provisioned only when the operator both deploys services AND maps at
# least one of them to a brand domain. Empty var.domain_names skips
# this module entirely \u2014 the deploy stays on *.run.app URLs and the
# CORS env var stays empty (legacy `*` behavior), preserving byte
# identity with the OSS demo.
#
# DNS is intentionally outside this module unless var.dns_zone_name
# is also set, because the zone usually lives in a separate project
# or outside Google Cloud. Operator runbook is in the module header.
# =============================================================================

module "loadbalancer" {
  count = var.enable_services && length(var.domain_names) > 0 ? 1 : 0

  source = "./modules/loadbalancer"

  project_id    = var.project_id
  region        = var.region
  environment   = var.loadbalancer_environment
  services      = module.cloud_run_services[0].service_names
  domain_names  = var.domain_names
  dns_zone_name = var.dns_zone_name

  depends_on = [module.cloud_run_services]
}
