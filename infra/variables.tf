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
# Required Variables
# =============================================================================

variable "project_id" {
  description = "GCP project ID for the OSS deployment"
  type        = string
}

variable "region" {
  description = "GCP region for all resources. Curated allowlist of regions where Cloud Run + Memorystore + Cloud SQL + Vertex AI are all available."
  type        = string
  default     = "us-central1"

  validation {
    condition = contains([
      "us-central1",
      "us-east1",
      "us-east4",
      "us-west1",
      "europe-west1",
      "europe-west4",
      "asia-southeast1",
      "asia-northeast1",
    ], var.region)
    error_message = "var.region must be one of: us-central1, us-east1, us-east4, us-west1, europe-west1, europe-west4, asia-southeast1, asia-northeast1 (regions where Cloud Run, Memorystore, Cloud SQL, and Vertex AI all coexist)."
  }
}

# =============================================================================
# Feature Toggles
# =============================================================================

variable "enable_alloydb" {
  description = "Use AlloyDB instead of Cloud SQL PostgreSQL (more expensive, more features)"
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# Stateful infra safety
# -----------------------------------------------------------------------------

variable "database_deletion_protection" {
  description = "Whether the primary database (Cloud SQL or AlloyDB) is protected from `tofu destroy`. Default true \u2014 set to false explicitly in your tfvars when you want to tear the deploy down. Applies to Cloud SQL `deletion_protection` and AlloyDB `deletion_policy` (DEFAULT vs FORCE)."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# Billing budget (cost protection)
# -----------------------------------------------------------------------------

variable "enable_billing_budget" {
  description = "Provision a project-level billing budget with email alerts at 50/90/100% of the monthly cap. Off by default so existing deploys don't surprise-create a budget on first re-apply. When true, billing_account_id and monthly_budget_amount become required."
  type        = bool
  default     = false
}

variable "billing_account_id" {
  description = "GCP billing account id (bare ID like `01ABCD-234567-EF8901`, no `billingAccounts/` prefix). Only consulted when enable_billing_budget=true. Find it via `gcloud beta billing accounts list`."
  type        = string
  default     = ""
}

variable "monthly_budget_amount" {
  description = "Monthly budget cap in `budget_currency_code` whole units. Alerts fire at 50/90/100% of this number plus a forecast-100% alert. Empty/zero disables the budget even when enable_billing_budget=true."
  type        = number
  default     = 0
}

variable "budget_currency_code" {
  description = "ISO-4217 currency code for the budget. Must match the billing account's currency (otherwise apply fails with `INVALID_ARGUMENT`)."
  type        = string
  default     = "USD"
}

variable "budget_alert_emails" {
  description = "Extra email addresses (beyond project Billing Admins, who always get notified) to receive budget alerts. One google_monitoring_notification_channel is created per address."
  type        = list(string)
  default     = []
}

variable "enable_gke" {
  description = "Deploy GKE cluster for runner_gke workloads (GPU support)"
  type        = bool
  default     = false
}

variable "enable_runner_cloudrun" {
  description = "Deploy LLM-powered runner as a Cloud Run service. Used by CI/CD deploy scripts to gate the runner_cloudrun Docker build and Cloud Run service deployment."
  type        = bool
  default     = false
}

variable "enable_maps_api_key" {
  description = "Create Maps API key in Secret Manager (requires Maps/Places/Weather API access)"
  type        = bool
  default     = false
}

variable "enable_monitoring" {
  description = "Deploy Cloud Monitoring alerts and uptime checks"
  type        = bool
  default     = false
}

variable "enable_services" {
  description = "Deploy Cloud Run services and IAM bindings. Set false for the Phase 1 base-infra apply (before image builds); set true for the Phase 2 services apply (after images are pushed)."
  type        = bool
  default     = false
}

# =============================================================================
# Per-user SaaS toggles
# =============================================================================

variable "enable_billing_service" {
  description = "Deploy the cmd/billing Stripe webhook receiver as its own Cloud Run service. Requires image_tags['billing'] when enabled."
  type        = bool
  default     = false
}

variable "enable_stripe_billing" {
  description = "Create the Stripe API key + webhook signing secret stubs in Secret Manager. Operator populates them afterwards via `gcloud secrets versions add` (see docs/ops/billing_stripe.md). Leave false to keep billing_provider=stub."
  type        = bool
  default     = false
}

variable "identity_provider" {
  description = "Which identity provider the gateway/admin trust for login. 'local' = fixture single-user (LOCAL_DEV_USER_EMAIL). 'firebase' = verify Firebase ID tokens via Firebase Admin SDK. 'iap' = trust the X-Goog-Authenticated-User-Email header from IAP."
  type        = string
  default     = "local"
}

variable "metering_sink" {
  description = "Destination for token-usage events from the after_model_callback. 'stdout' = log only (default). 'sqlite' = local file. 'pubsub' = publish to the usage-metering topic (recommended in cloud)."
  type        = string
  default     = "stdout"
}

variable "billing_provider" {
  description = "Backend behind /v1/billing/* on the gateway and the webhook handler in cmd/billing. 'stub' (default) refuses everything. 'stripe' enables the real Checkout/Portal creators + HMAC verification."
  type        = string
  default     = "stub"
}

variable "stripe_price_map" {
  description = "Comma-separated price_id:plan_tier pairs (e.g. price_1ABC:pro,price_1XYZ:team) consumed by both the gateway Checkout creator and cmd/billing webhook handler. Empty disables Stripe even when enable_stripe_billing=true."
  type        = string
  default     = ""
}

variable "stripe_return_base_url" {
  description = "Public base URL (no trailing slash) the user is redirected back to after Stripe Checkout / Customer Portal flows. Typically the frontend service URL."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# Firebase Auth (only used when identity_provider=firebase)
# -----------------------------------------------------------------------------

variable "firebase_project_id" {
  description = "Firebase project id for ID-token verification. Empty defaults to var.project_id. Set explicitly only when Firebase Auth lives in a separate GCP project from the workload (e.g. prod Firebase tenant project distinct from the locked-down workload project)."
  type        = string
  default     = ""
}

variable "firebase_web_api_key" {
  description = "Firebase Web API key copied from the Firebase console (Project settings > General > Web API key). Shipped to browsers via /config.js -> window.ENV.FIREBASE_API_KEY. NOT a secret: Firebase Web API keys identify the project, they do not authorize anything by themselves \u2014 access control is enforced by Firebase Security Rules and by the gateway's server-side ID-token verification. Required when identity_provider=firebase."
  type        = string
  default     = ""
}

variable "firebase_auth_domain" {
  description = "Custom Firebase Auth domain (e.g. auth.example.com). Empty derives '<firebase_project_id>.firebaseapp.com' which is the default Firebase Hosting domain and works out of the box."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# Firebase App Check (server-side verification, 2026-06-22 audit P2.B)
# -----------------------------------------------------------------------------

variable "appcheck_enforce" {
  description = "Server-side Firebase App Check mode for the gateway. 'off' (default) disables the middleware entirely. 'report-only' logs missing/invalid X-Firebase-AppCheck headers but lets requests through — use this for at least one rollout cycle. 'enforce' returns 401 for missing/invalid tokens. The SPA + /login page already attach the header when RECAPTCHA_ENTERPRISE_SITE_KEY is set; non-browser clients (curl, scripts) cannot produce App Check tokens and will be rejected in enforce mode."
  type        = string
  default     = "off"
  validation {
    condition     = contains(["off", "report-only", "enforce"], var.appcheck_enforce)
    error_message = "appcheck_enforce must be one of: off, report-only, enforce."
  }
}

variable "firebase_project_number" {
  description = "Numeric Firebase / GCP project number (NOT the alphanumeric project id). Required when appcheck_enforce is set, because Firebase App Check JWTs carry the project number in the iss + aud claims. Find it in the GCP console > Home > Project info, or by running `gcloud projects describe <project-id> --format='value(projectNumber)'`. Empty defaults to the project number derived from project_id at apply time."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# Payment-failed read-only gate (Phase 12)
# -----------------------------------------------------------------------------

variable "enforce_payment_failed" {
  description = "When true, the gateway 402s state-changing requests from users whose Stripe invoice is past due AND the grace period has expired. GET requests, /api/v1/billing/*, and /api/v1/me/* always pass so the user can update their card. Default false preserves byte-identical legacy behavior."
  type        = bool
  default     = false
}

variable "payment_failed_grace_days" {
  description = "Grace window in days after a Stripe invoice.payment_failed event during which writes are still allowed. Default 7 (Stripe's Smart Retry window)."
  type        = number
  default     = 7
}

# -----------------------------------------------------------------------------
# Observability + hardening knobs (all default to empty/false = legacy
# byte-identical behavior). Forwarded to cloud_run_services module.
# -----------------------------------------------------------------------------

variable "csp_enforce" {
  description = "Set CSP_ENFORCE=true on every Go service \u2192 Content-Security-Policy header is enforced instead of report-only. Run report-only for at least a week first."
  type        = bool
  default     = false
}

variable "security_hsts" {
  description = "Set SECURITY_HSTS=true on every Go service \u2192 emit Strict-Transport-Security on HTTPS. Only flip after the LB module + managed SSL certs are live; HSTS pins are sticky in browsers."
  type        = bool
  default     = false
}

variable "log_format" {
  description = "LOG_FORMAT env for every Go service. '' = legacy text. 'gcp' = Cloud Logging JSON with severity field renames (recommended in cloud). 'json' = plain slog JSON."
  type        = string
  default     = ""
}

variable "otel_exporter" {
  description = "OTEL_EXPORTER env. '' (default) = no-op tracer. 'gcp' = ship spans to Cloud Trace. 'stdout' = local debug."
  type        = string
  default     = ""
}

variable "otel_sample_ratio" {
  description = "OTEL_SAMPLE_RATIO. '' uses the package default 0.1 (10%, keeps Cloud Trace inside the 2.5M-spans/month free tier)."
  type        = string
  default     = ""
}

variable "recaptcha_enterprise_site_key" {
  description = "Public reCAPTCHA Enterprise site key for Firebase App Check on the static /login page. Obtain from Firebase Console \u2192 App Check \u2192 Web app. Empty disables App Check (signup still works, just unattested)."
  type        = string
  default     = ""
}

variable "planner_per_user_isolation" {
  description = "When true, sets PLANNER_PER_USER_ISOLATION=on so the planner agents enforce owner_id row-level scoping. Required for genuine multi-user data isolation; default off preserves single-tenant legacy behavior."
  type        = bool
  default     = false
}

# =============================================================================
# Cloud Run services (gated by enable_services)
# =============================================================================

variable "image_tags" {
  description = "Map of service name -> fully-qualified image URL with tag. Required keys when enable_services=true: gateway, admin, dash, frontend, tester, runner_autopilot, runner_cloudrun, leadership_runner_autopilot, leadership_runner, ai_echo, ai_echo_reactive, ai_echo_autopilot. ALSO required when enable_billing_service=true: billing. Populated by the Phase 4 Cloud Build orchestrator after images are built."
  type        = map(string)
  default     = {}
}

variable "embedding_backend" {
  description = "Embedding strategy for planner_with_memory. 'vertex_ai' for OSS Cloud SQL deploys; 'alloydb_ai' for AlloyDB deploys."
  type        = string
  default     = "vertex_ai"
}

variable "agent_urls" {
  description = "Comma-separated bare agent URLs threaded into the gateway's AGENT_URLS env (e.g. https://ae1,https://ae2). The gateway parses these and discovers each agent's name from its /a2a/v1/card. Populated by the Phase 4 collect-ae-urls step."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# Cloud Run IAM tier split (customer vs operator). See full rationale in
# infra/modules/cloud-run-iam/variables.tf.
#
# Defaults preserve the OSS demo posture (frontend public, no operator
# bindings). For an external SaaS:
#   - keep customer_uis_unauthenticated = true (customers have no
#     Google identity; Firebase ID-token is the app-layer auth gate).
#   - set operator_principals = ["user:you@example.com", ...] so
#     admin/dash/tester are reachable ONLY by operator identities.
#     Access pattern: `gcloud run services proxy admin --region=...`.
# -----------------------------------------------------------------------------

variable "customer_uis_unauthenticated" {
  description = "If true (default), bind allUsers as roles/run.invoker on the customer-facing frontend BFF. Required true for an external SaaS where customers have no Google identity; the real auth gate is Firebase ID-token verification in the application layer."
  type        = bool
  default     = true
}

variable "operator_principals" {
  description = "IAM members (e.g. ['user:ops@example.com', 'group:ops@example.com']) granted roles/run.invoker on admin/dash/tester. These three services are NEVER bound to allUsers. Empty (default) = no operator bindings; admin/dash/tester are reachable only by the cloud-build SA. Recommended for an external SaaS: add at least one operator user/group."
  type        = list(string)
  default     = []
}

# =============================================================================
# E3a: Custom domain + HTTPS Load Balancer (opt-in)
#
# When var.domain_names is empty (default), the loadbalancer module is
# not instantiated at all and the deployed stack stays on the legacy
# *.run.app URLs \u2014 byte-identical to the pre-E3 OSS deploy.
#
# When non-empty, the loadbalancer module provisions a single global
# HTTPS LB with one host_rule per (service_key, fqdn) pair, a managed
# SSL cert covering every fqdn, a reserved global IP, and an
# HTTP\u2192HTTPS redirect. The CORS_ALLOWED_ORIGINS env var on the
# public services is automatically derived from var.domain_names so a
# brand-domain deploy never has to remember the `*` -> explicit
# allow-list swap.
# =============================================================================

variable "domain_names" {
  description = "Map of Cloud Run service key -> fully qualified domain name (e.g. {frontend = \"app.example.com\", gateway = \"api.example.com\", admin = \"admin.example.com\", billing = \"hooks.example.com\"}). Empty map (default) skips the LB module and keeps the deploy on *.run.app URLs. Valid keys are the cloud_run_services outputs: gateway, admin, dash, frontend, tester, billing."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k in keys(var.domain_names) :
      contains(["gateway", "admin", "dash", "frontend", "tester", "billing"], k)
    ])
    error_message = "domain_names keys must be one of: gateway, admin, dash, frontend, tester, billing (the public-facing services in cloud_run_services). Runner agents are internal-only and never get a public domain."
  }
}

variable "dns_zone_name" {
  description = "Optional Cloud DNS managed_zone name. When set alongside var.domain_names, the loadbalancer module publishes one A record per entry pointing at the LB IP. Empty (default) means the operator updates DNS by hand \u2014 the typical case when the zone lives in a separate project or outside Google Cloud."
  type        = string
  default     = ""
}

variable "loadbalancer_environment" {
  description = "Name baked into LB resource names (LB IP, URL map, SSL cert, etc.). Lowercase + hyphens, 1-29 chars. Defaults to 'oss' to match the rest of this single-project root module."
  type        = string
  default     = "oss"
}

variable "labels" {
  description = "Labels applied to every Race Condition resource (Cloud Run services, Cloud SQL, Redis, Pub/Sub topics, AE engines). Used for cost attribution and demo-asset cleanup. The default value is what the public OSS demo expects; downstream deployers can override or merge in additional keys."
  type        = map(string)
  default = {
    dev-tutorial = "leadership-simulation"
  }
}

# =============================================================================
# IAM
# =============================================================================

variable "developers" {
  description = "List of developer principals (format: user:email@domain)"
  type        = list(string)
  default     = []
}

variable "agent_engine_sa_users" {
  description = "Additional principals that can act as the Agent Engine SA"
  type        = list(string)
  default     = []
}

# =============================================================================
# Database
# =============================================================================

variable "db_initial_password" {
  description = "Optional explicit password. If null, a random 32-char alphanumeric password is generated and persisted in TF state + Secret Manager (via the cloud-sql-postgres module)."
  type        = string
  sensitive   = true
  default     = null
}

# =============================================================================
# Monitoring (when enabled)
# =============================================================================

variable "alert_email" {
  description = "Email address for monitoring alert notifications"
  type        = string
  default     = ""
}
