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

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "compute_sa_email" {
  description = "Service account email used as the runtime identity for all services."
  type        = string
}

variable "vpc_network_name" {
  description = "VPC network name (self-link short form, e.g. 'race-condition-vpc')."
  type        = string
}

variable "vpc_subnet_name" {
  description = "Serverless subnet name for Cloud Run direct VPC egress."
  type        = string
}

variable "image_tags" {
  description = "Map of service name -> fully-qualified image URL with tag."
  type        = map(string)
  # Required keys: gateway, admin, dash, frontend, tester, runner_autopilot, runner_cloudrun
  # Required ALSO when enable_billing_service=true: billing
}

variable "redis_host" {
  type = string
}

variable "redis_port" {
  type    = number
  default = 6379
}

variable "database_ip" {
  description = "Private IP of the Cloud SQL / AlloyDB instance."
  type        = string
}

variable "database_secret_id" {
  description = "Secret Manager secret ID holding the database password."
  type        = string
}

variable "orchestration_topic" {
  description = "Pub/Sub topic name for cross-cluster orchestration ingress."
  type        = string
}

variable "telemetry_topic" {
  description = "Pub/Sub topic name for agent telemetry."
  type        = string
  default     = "agent-telemetry"
}

variable "embedding_backend" {
  description = "Embedding strategy for planner_with_memory tools. 'vertex_ai' for OSS Cloud SQL deploys; 'alloydb_ai' for AlloyDB ai.embedding() deploys."
  type        = string
  default     = "vertex_ai"
}

variable "agent_urls" {
  description = "Comma-separated bare URLs for AE-deployed agents (gateway AGENT_URLS env, e.g. https://ae1,https://ae2). The gateway parses these and discovers each agent's name from its /a2a/v1/card. Populated by Phase 4.5 cloud-build collect-ae-urls step."
  type        = string
  default     = ""
}

variable "cors_allowed_origins" {
  description = "Value for the CORS_ALLOWED_ORIGINS env var on the public-facing HTTP services (gateway, admin, frontend, dash, tester, billing). Empty (default) means the services keep the legacy `*` allow-all behavior \u2014 fine for the OSS .run.app demo where the URLs are unguessable and Cloud Run authentication is the real gate. When the LB module is wired up, the root module derives a comma-separated `https://<fqdn>` list from var.domain_names and threads it here so production locks CORS down to the brand domains automatically."
  type        = string
  default     = ""
}

variable "min_instances" {
  description = "Default min_instance_count for all services. OSS defaults to scale-to-zero."
  type        = number
  default     = 0
}

# Right-sized per docs/plans/2026-04-16-oss-right-sizing-plan.md.
# OSS targets minimum cost: scale-to-zero, platform-minimum CPU/memory.
variable "service_sizing" {
  description = "Per-service CPU/memory/max_instances. Right-sized for OSS cost."
  type = map(object({
    cpu           = string
    memory        = string
    max_instances = number
  }))
  # max_instances = 1 for ALL services (OSS cost control: never scale
  # beyond a single instance per service). Memory differs by runtime:
  # Go services stay at 512Mi; Python+ADK runners need 1Gi (Phase 7
  # build #20 surfaced 512Mi OOM-killing the Python container during
  # startup imports before the port-8080 health probe ever opened).
  default = {
    gateway                     = { cpu = "1", memory = "512Mi", max_instances = 1 }
    admin                       = { cpu = "1", memory = "512Mi", max_instances = 1 }
    tester                      = { cpu = "1", memory = "512Mi", max_instances = 1 }
    frontend                    = { cpu = "1", memory = "512Mi", max_instances = 1 }
    dash                        = { cpu = "1", memory = "512Mi", max_instances = 1 }
    billing                     = { cpu = "1", memory = "512Mi", max_instances = 1 }
    runner_autopilot            = { cpu = "1", memory = "1Gi", max_instances = 1 }
    runner_cloudrun             = { cpu = "1", memory = "1Gi", max_instances = 1 }
    leadership_runner_autopilot = { cpu = "1", memory = "1Gi", max_instances = 1 }
    leadership_runner           = { cpu = "1", memory = "1Gi", max_instances = 1 }
    ai_echo                     = { cpu = "1", memory = "1Gi", max_instances = 1 }
    ai_echo_reactive            = { cpu = "1", memory = "1Gi", max_instances = 1 }
    ai_echo_autopilot           = { cpu = "1", memory = "1Gi", max_instances = 1 }
  }
}

# Per-service max_instance_request_concurrency. Tuning this matters
# even with max_instances=1: at the Cloud Run default of 80, instance
# #1 starts queueing at request 81 and Cloud Run replies 503 once the
# queue fills. Lifting the per-instance ceiling lets a single small
# instance absorb burst load without spinning up a second one
# (cheaper) AND without dropping requests on the floor.
#
# Defaults are conservative for the 1 vCPU / 256\u2013512 MiB footprint
# the OSS sizing locks in:
#   - gateway/frontend: thin HTTP proxies, IO-bound on Redis/Postgres
#     \u2192 200 is the Cloud Run-recommended upper end for proxies.
#   - admin/dash: heavier handlers (DB-heavy reads) \u2192 100.
#   - billing: Stripe webhook, modest \u2192 50.
#   - tester: load-generator side; we don't WANT it to absorb floods.
#   - runner_autopilot / runner_cloudrun: ADK Python agents do
#     serial inference. Concurrency > 1 would interleave LLM calls
#     in a single process \u2192 race conditions in agent state. Keep at 1.
variable "service_concurrency" {
  description = "Per-service max_instance_request_concurrency. ADK runners MUST stay at 1 (serial inference)."
  type        = map(number)
  default = {
    gateway                     = 200
    admin                       = 100
    tester                      = 50
    frontend                    = 200
    dash                        = 100
    billing                     = 50
    runner_autopilot            = 1
    runner_cloudrun             = 1
    leadership_runner_autopilot = 1
    leadership_runner           = 1
    ai_echo                     = 1
    ai_echo_reactive            = 1
    ai_echo_autopilot           = 1
  }
  validation {
    condition     = alltrue([for k, v in var.service_concurrency : v >= 1 && v <= 1000])
    error_message = "service_concurrency values must be in [1, 1000] (Cloud Run hard limit)."
  }
  validation {
    condition = (
      lookup(var.service_concurrency, "runner_autopilot", 1) == 1 &&
      lookup(var.service_concurrency, "runner_cloudrun", 1) == 1 &&
      lookup(var.service_concurrency, "leadership_runner_autopilot", 1) == 1 &&
      lookup(var.service_concurrency, "leadership_runner", 1) == 1 &&
      lookup(var.service_concurrency, "ai_echo", 1) == 1 &&
      lookup(var.service_concurrency, "ai_echo_reactive", 1) == 1 &&
      lookup(var.service_concurrency, "ai_echo_autopilot", 1) == 1
    )
    error_message = "ADK runners must keep concurrency=1; serial inference is a correctness requirement, not a perf knob."
  }
}

# ---------------------------------------------------------------------------
# Per-user SaaS toggles + database connection plumbing
# ---------------------------------------------------------------------------

variable "enable_billing_service" {
  description = "When true, deploys the cmd/billing Stripe webhook receiver as its own Cloud Run service."
  type        = bool
  default     = false
}

variable "identity_provider" {
  description = "Value for the IDENTITY_PROVIDER env var on gateway+admin. 'local' = fixture LOCAL_DEV_USER_EMAIL single-user. 'firebase' = verify Firebase ID tokens. 'iap' = trust IAP-injected headers."
  type        = string
  default     = "local"
  validation {
    condition     = contains(["local", "firebase", "iap"], var.identity_provider)
    error_message = "identity_provider must be one of: local, firebase, iap."
  }
}

variable "metering_sink" {
  description = "Where the after_model_callback writes token-usage events. 'stdout' = log only (local debug). 'sqlite' = append to local file. 'pubsub' = publish to usage-metering topic (cloud default)."
  type        = string
  default     = "stdout"
  validation {
    condition     = contains(["stdout", "sqlite", "pubsub"], var.metering_sink)
    error_message = "metering_sink must be one of: stdout, sqlite, pubsub."
  }
}

variable "metering_topic" {
  description = "Pub/Sub topic name the gateway/admin/billing services publish metering events to when metering_sink=pubsub. Pre-provisioned in modules/pubsub as 'usage-metering'."
  type        = string
  default     = "usage-metering"
}

variable "billing_provider" {
  description = "Which billing provider runs in cmd/billing. 'stub' (default) refuses every webhook event \u2014 safe default for misconfigured deploys. 'stripe' arms HMAC verification + the real handlers; requires stripe_webhook_secret_id."
  type        = string
  default     = "stub"
  validation {
    condition     = contains(["stub", "stripe"], var.billing_provider)
    error_message = "billing_provider must be one of: stub, stripe."
  }
}

variable "stripe_api_key_secret_id" {
  description = "Secret Manager secret id holding the Stripe server-side API key (sk_live_... / sk_test_...). Mounted on the gateway as STRIPE_API_KEY. Empty disables the real Checkout/Portal creators (falls back to stub)."
  type        = string
  default     = ""
}

variable "stripe_webhook_secret_id" {
  description = "Secret Manager secret id holding the Stripe webhook signing secret (whsec_...). Mounted on cmd/billing as STRIPE_WEBHOOK_SECRET. Required when billing_provider=stripe."
  type        = string
  default     = ""
}

variable "stripe_price_map" {
  description = "Comma-separated price_id:plan_tier pairs (e.g. price_1ABC:pro,price_1XYZ:team). Mounted on gateway+billing as STRIPE_PRICE_MAP. Empty disables both directions (no checkout, no webhook plan updates)."
  type        = string
  default     = ""
}

variable "stripe_return_base_url" {
  description = "Public base URL (no trailing slash) the user is redirected back to after Checkout/Portal flows. Mounted on the gateway as STRIPE_RETURN_BASE_URL. Empty forces fallback to the stub checkout creator."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Firebase Auth wiring (only used when identity_provider=firebase)
# ---------------------------------------------------------------------------

variable "firebase_project_id" {
  description = "Firebase project id for token verification. Defaults to var.project_id when empty; set explicitly when Firebase Auth lives in a different GCP project than the rest of the deploy (common in prod where the workload project is locked down and Firebase has its own tenant project)."
  type        = string
  default     = ""
}

variable "firebase_web_api_key" {
  description = "Firebase Web API key (browser-side). Mounted on the frontend BFF as FIREBASE_API_KEY and served via /config.js. NOT a secret \u2014 Firebase Web API keys identify the project and are designed to ship to browsers; access control happens via Firebase Security Rules + the gateway's token verification."
  type        = string
  default     = ""
}

variable "firebase_auth_domain" {
  description = "Custom Firebase auth domain (e.g. auth.example.com). Empty derives <firebase_project_id>.firebaseapp.com which is the default Firebase Hosting domain. Mounted on the frontend BFF as FIREBASE_AUTH_DOMAIN."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Firebase App Check server-side verification (2026-06-22 audit P2.B)
# ---------------------------------------------------------------------------

variable "appcheck_enforce" {
  description = "Gateway-side App Check mode: off (default), report-only, or enforce. See top-level variables.tf for details."
  type        = string
  default     = "off"
}

variable "firebase_project_number" {
  description = "Numeric Firebase / GCP project number, required when appcheck_enforce is non-off. Empty disables App Check even if appcheck_enforce is set."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Payment-failed read-only gate (Phase 12)
# ---------------------------------------------------------------------------

variable "enforce_payment_failed" {
  description = "When true, the gateway 402s state-changing requests from orgs whose Stripe invoice is past due AND the grace period has expired. Reads (GET) always pass; /api/v1/billing and /api/v1/me always pass so the user can update their card. Default false preserves byte-identical legacy behavior for existing deployments."
  type        = bool
  default     = false
}

variable "payment_failed_grace_days" {
  description = "Grace window (in days) after a Stripe invoice.payment_failed event during which writes are still allowed. Defaults to 7 days (Stripe's typical Smart Retry window). Ignored when enforce_payment_failed=false."
  type        = number
  default     = 7

  validation {
    condition     = var.payment_failed_grace_days >= 0
    error_message = "payment_failed_grace_days must be non-negative."
  }
}

# ---------------------------------------------------------------------------
# Database connection plumbing
# ---------------------------------------------------------------------------

variable "database_user" {
  description = "Postgres role the gateway/admin/billing services connect as. Defaults to the password-auth 'postgres' role provisioned by the cloud-sql-postgres module (paired with ALLOYDB_PASSWORD from Secret Manager). For IAM-auth, change to the Cloud Run compute-SA's truncated email."
  type        = string
  default     = "postgres"
}

variable "database_name" {
  description = "Database name inside the Postgres instance (Cloud SQL: 'agent_memory'; AlloyDB: 'postgres')."
  type        = string
  default     = "agent_memory"
}

variable "database_port" {
  description = "TCP port for the Postgres instance. 5432 is the standard."
  type        = number
  default     = 5432
}

# ---------------------------------------------------------------------------
# Observability + hardening env knobs (all default to empty/false = legacy
# byte-identical behavior). When set, these flow into the Cloud Run env on
# every Go service through local.common_env; the relevant `cmd/*/main.go`
# already reads them via os.Getenv at boot (no code change needed).
# Documented at internal/middleware/security_headers.go, internal/logging,
# internal/tracing, and .env.example.
# ---------------------------------------------------------------------------

variable "csp_enforce" {
  description = "When true, sets CSP_ENFORCE=true \u2192 Content-Security-Policy header is enforced instead of report-only. Run report-only for at least a week and watch Cloud Logging for CSP violation reports before flipping."
  type        = bool
  default     = false
}

variable "security_hsts" {
  description = "When true, sets SECURITY_HSTS=true \u2192 Strict-Transport-Security header is emitted on HTTPS responses. Only flip after the LB module + managed SSL certs are live; once browsers cache the HSTS pin, plain-http fallback is gone."
  type        = bool
  default     = false
}

variable "log_format" {
  description = "Value for the LOG_FORMAT env on every Go service. '' (default) = text logs (legacy). 'gcp' = JSON with severity/timestamp/message field renames so Cloud Logging categorizes by severity. 'json' = plain slog JSON without GCP field renames."
  type        = string
  default     = ""
  validation {
    condition     = contains(["", "text", "json", "gcp"], var.log_format)
    error_message = "log_format must be one of: '' (default = text), text, json, gcp."
  }
}

variable "otel_exporter" {
  description = "Value for the OTEL_EXPORTER env on every Go service. '' (default) = no-op tracer (legacy). 'gcp' = ship spans to Cloud Trace via opentelemetry-operations-go. 'stdout' = local debug only."
  type        = string
  default     = ""
  validation {
    condition     = contains(["", "gcp", "stdout"], var.otel_exporter)
    error_message = "otel_exporter must be one of: '' (default = no-op), gcp, stdout."
  }
}

variable "otel_sample_ratio" {
  description = "Value for the OTEL_SAMPLE_RATIO env. '' uses the package default of 0.1 (10%, keeps Cloud Trace under the 2.5M-spans/month free tier). Range [0.0, 1.0]; values outside the range are clamped at runtime."
  type        = string
  default     = ""
}

variable "recaptcha_enterprise_site_key" {
  description = "Public reCAPTCHA Enterprise site key (NOT the secret API key). Surfaced to the static /login page via /config.js \u2192 window.ENV.RECAPTCHA_ENTERPRISE_SITE_KEY so initializeAppCheck() can attach App Check tokens to Firebase Auth requests. Obtain from Firebase Console \u2192 App Check \u2192 Web app \u2192 reCAPTCHA Enterprise provider. Empty disables App Check (signup still works, just unattested)."
  type        = string
  default     = ""
}

variable "planner_per_user_isolation" {
  description = "When true, sets PLANNER_PER_USER_ISOLATION=on which enforces owner_id row-level scoping in agents/planner/skills/.../tools.py and agents/planner_with_memory/memory/tools.py. Required for genuine multi-user data isolation on shared planner backends; default off preserves single-tenant legacy behavior."
  type        = bool
  default     = false
}
