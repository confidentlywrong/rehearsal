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

# Common env vars threaded into every Race Condition Cloud Run service.
# Mirrors scripts/deploy/deploy.py:build_env_vars()
# (the legacy gcloud-based deployer). Cross-service URLs (ADMIN_URL,
# DASH_URL, ...) are deliberately omitted: they cause TF dependency
# cycles between services. The Phase 4 Cloud Build orchestrator wires
# them after `terraform apply` completes via `gcloud run services
# update`, matching the behaviour of the legacy deploy.py.
data "google_project" "current" {
  project_id = var.project_id
}

locals {
  redis_addr = "${var.redis_host}:${var.redis_port}"

  # Cloud Run service URLs follow a deterministic pattern keyed on the
  # project number. We construct the runner URLs here so the gateway's
  # AGENT_URLS env var can include them without referencing the runner
  # resources directly (which would create a tf cycle: runners depend on
  # gateway.uri for GATEWAY_INTERNAL_URL).
  runner_autopilot_url            = "https://runner-autopilot-${data.google_project.current.number}.${var.region}.run.app"
  runner_cloudrun_url             = "https://runner-cloudrun-${data.google_project.current.number}.${var.region}.run.app"
  leadership_runner_autopilot_url = "https://leadership-runner-autopilot-${data.google_project.current.number}.${var.region}.run.app"
  leadership_runner_url           = "https://leadership-runner-${data.google_project.current.number}.${var.region}.run.app"
  ai_echo_url                     = "https://ai-echo-${data.google_project.current.number}.${var.region}.run.app"
  ai_echo_reactive_url            = "https://ai-echo-reactive-${data.google_project.current.number}.${var.region}.run.app"
  ai_echo_autopilot_url           = "https://ai-echo-autopilot-${data.google_project.current.number}.${var.region}.run.app"

  common_env = concat([
    { name = "PROJECT_ID", value = var.project_id },
    { name = "GOOGLE_CLOUD_PROJECT", value = var.project_id },
    { name = "GOOGLE_CLOUD_LOCATION", value = "global" },
    { name = "GOOGLE_GENAI_USE_ENTERPRISE", value = "true" },
    { name = "PUBSUB_PROJECT_ID", value = var.project_id },
    { name = "PUBSUB_TOPIC_ID", value = var.telemetry_topic },
    { name = "ORCHESTRATION_TOPIC_ID", value = var.orchestration_topic },
    { name = "REDIS_ADDR", value = local.redis_addr },
    { name = "REDIS_HOST", value = var.redis_host },
    { name = "REDIS_PORT", value = tostring(var.redis_port) },
    { name = "DATABASE_IP", value = var.database_ip },
    { name = "ALLOYDB_HOST", value = var.database_ip },
    { name = "ALLOYDB_PORT", value = tostring(var.database_port) },
    { name = "ALLOYDB_USER", value = var.database_user },
    { name = "ALLOYDB_DATABASE", value = var.database_name },
    { name = "EMBEDDING_BACKEND", value = var.embedding_backend },
    { name = "PYTHONPATH", value = "." },
    { name = "GIN_MODE", value = "release" },
    ],
    # E3a: when an LB+custom-domain is in front of these services
    # (var.cors_allowed_origins != ""), lock CORS down to that
    # explicit allow-list. Empty preserves the legacy `*` default
    # in middleware.CORS (cmd/gateway/main.go:454,
    # cmd/admin/main.go:458, cmd/frontend/main.go:104).
    # ADK runner services also read CORS_ALLOWED_ORIGINS via
    # agents/utils/a2a.py; sharing the same env keeps both Go and
    # Python tiers on one allow-list.
    var.cors_allowed_origins != "" ? [
      { name = "CORS_ALLOWED_ORIGINS", value = var.cors_allowed_origins },
    ] : [],

    # Observability + hardening knobs. All empty by default \u2192 no env
    # entry emitted \u2192 byte-identical to legacy. Each `cmd/*/main.go`
    # reads these via os.Getenv at boot; absence means the existing
    # default code path (text logs, no HSTS, report-only CSP, no-op
    # tracer, no App Check, single-tenant planner reads).
    var.csp_enforce ? [{ name = "CSP_ENFORCE", value = "true" }] : [],
    var.security_hsts ? [{ name = "SECURITY_HSTS", value = "true" }] : [],
    var.log_format != "" ? [{ name = "LOG_FORMAT", value = var.log_format }] : [],
    var.otel_exporter != "" ? [{ name = "OTEL_EXPORTER", value = var.otel_exporter }] : [],
    var.otel_sample_ratio != "" ? [{ name = "OTEL_SAMPLE_RATIO", value = var.otel_sample_ratio }] : [],
    var.recaptcha_enterprise_site_key != "" ? [{ name = "RECAPTCHA_ENTERPRISE_SITE_KEY", value = var.recaptcha_enterprise_site_key }] : [],
    var.planner_per_user_isolation ? [{ name = "PLANNER_PER_USER_ISOLATION", value = "on" }] : [],
  )

  # Per-user SaaS env block. Merged into gateway+admin+billing
  # services. Stripe-specific values stay empty when billing_provider
  # is "stub" — the gateway then falls back to the stub creator
  # (cmd/gateway/main.go logs `STRIPE_API_KEY empty, falling back
  # to stub` on boot).
  #
  # FIREBASE_PROJECT_ID is only emitted when identity_provider is
  # firebase. The middleware refuses to start with
  # identity_provider=firebase and no project id (falls back to
  # GOOGLE_CLOUD_PROJECT but warns), so making it explicit here makes
  # multi-project deploys (prod GCP project, separate Firebase
  # tenant) work without code changes.
  saas_env = concat([
    { name = "IDENTITY_PROVIDER", value = var.identity_provider },
    { name = "METERING_SINK", value = var.metering_sink },
    { name = "METERING_TOPIC", value = var.metering_topic },
    { name = "BILLING_PROVIDER", value = var.billing_provider },
    { name = "STRIPE_PRICE_MAP", value = var.stripe_price_map },
    { name = "STRIPE_RETURN_BASE_URL", value = var.stripe_return_base_url },
    { name = "BILLING_ENFORCE_PAYMENT_FAILED", value = var.enforce_payment_failed ? "on" : "off" },
    { name = "BILLING_PAYMENT_GRACE_DAYS", value = tostring(var.payment_failed_grace_days) },
    ], var.identity_provider == "firebase" ? [
    { name = "FIREBASE_PROJECT_ID", value = coalesce(var.firebase_project_id, var.project_id) },
  ] : [],
  # P2.B (2026-06-22 audit): Firebase App Check server-side
  # verification env. APPCHECK_ENFORCE is always set so a "noop"
  # gateway logs its mode at boot ("APPCHECK: enabled mode=…" or
  # "off"); FIREBASE_PROJECT_NUMBER is only emitted when set so the
  # middleware gracefully degrades to off if the operator forgot.
  [{ name = "APPCHECK_ENFORCE", value = var.appcheck_enforce }],
  var.firebase_project_number != "" ? [
    { name = "FIREBASE_PROJECT_NUMBER", value = var.firebase_project_number },
  ] : [])

  # Frontend BFF gets the *web* Firebase config (NOT a secret —
  # Firebase Web API keys are designed to ship to the browser; they
  # identify the project, they don't authorize anything by themselves).
  # The frontend exposes these via /config.js → window.ENV, which
  # the static /login page reads to decide LOCAL vs FIREBASE mode.
  # Emitted only when identity_provider=firebase; otherwise the login
  # page falls back to LOCAL mode (no Firebase init), which is what
  # the local-dev fixture user expects.
  frontend_firebase_env = (var.identity_provider == "firebase") ? [
    { name = "FIREBASE_PROJECT_ID", value = coalesce(var.firebase_project_id, var.project_id) },
    { name = "FIREBASE_API_KEY", value = var.firebase_web_api_key },
    { name = "FIREBASE_AUTH_DOMAIN", value = coalesce(var.firebase_auth_domain, "${coalesce(var.firebase_project_id, var.project_id)}.firebaseapp.com") },
  ] : []

  # Plain env vars that gateway+admin+billing all receive. Stripe
  # secrets are mounted via secret_key_ref and therefore appended
  # per-service (not in this slice), because value_source cannot
  # live in the same env block as a plain value in a `dynamic` loop.
  gateway_saas_env = local.saas_env
  admin_saas_env   = local.saas_env
  billing_saas_env = local.saas_env
  # frontend BFF calls middleware.AccountResolverFromEnv() (cmd/frontend/main.go:452)
  # so it needs the same IDENTITY_PROVIDER + Firebase wiring as gateway/admin.
  frontend_saas_env = local.saas_env

  # Cloud Run agent extras (runner_autopilot, runner_cloudrun).
  # REDIS_MAX_CONNECTIONS sized for the 1000-runner burst scenario
  # (in-process subscriber dispatch). 250 leaves head-room above the
  # 100 ceiling that triggered pool exhaustion in 2026-06-24 testing.
  # Memorystore BASIC tier supports 13k client connections so this is
  # far below the instance ceiling; cost is ~50MB of socket buffers.
  agent_env = [
    { name = "DISPATCH_MODE", value = "subscriber" },
    { name = "REDIS_MAX_CONNECTIONS", value = "250" },
    { name = "REDIS_SESSION_MAX_CONNECTIONS", value = "250" },
    { name = "SESSION_STORE_OVERRIDE", value = "redis" },
  ]
}

# ---------------------------------------------------------------------------
# Gateway -- entry point invoked by AE agents and UI services
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "gateway" {
  project  = var.project_id
  location = var.region
  name     = "gateway"
  # OSS UX: allow `terraform destroy` and the enable_services flip-flop
  # in cloudbuild-bootstrap to actually run. With deletion_protection
  # left at the provider default (true), a single failed Cloud Build
  # leaves the project in a state that can't be recovered by re-running
  # the same build (Phase 7 build #21).
  deletion_protection = false

  template {
    service_account = var.compute_sa_email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["gateway"].max_instances
    }

    # Slice D3: per-instance request concurrency. Default Cloud Run
    # is 80; raising it lets a single small instance absorb burst
    # without 503ing or spinning up a second instance.
    max_instance_request_concurrency = var.service_concurrency["gateway"]

    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.image_tags["gateway"]

      resources {
        limits = {
          cpu    = var.service_sizing["gateway"].cpu
          memory = var.service_sizing["gateway"].memory
        }
      }

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      env {
        name  = "AGENT_NAME"
        value = "gateway"
      }
      env {
        name = "AGENT_URLS"
        # var.agent_urls is the comma-separated list of AE engine URLs collected
        # by the cloudbuild `collect-ae-urls` step. We also need the two Cloud
        # Run runner agents (runner_autopilot, runner_cloudrun) for spawn_runners
        # to find them. We can't reference google_cloud_run_v2_service.runner_*
        # .uri directly here because the runners depend on gateway.uri (for
        # GATEWAY_INTERNAL_URL), which would create a tf cycle. Cloud Run service
        # URLs follow a deterministic pattern -- `<service>-<projectnumber>.<region>.run.app`
        # -- so we compose them here without a resource cross-reference.
        # Without this the gateway only discovers the AE engines and
        # spawn_runners() fails with "unknown agent type runner_autopilot".
        value = join(",", compact([
          var.agent_urls,
          local.runner_autopilot_url,
          local.runner_cloudrun_url,
          local.leadership_runner_autopilot_url,
          local.leadership_runner_url,
          local.ai_echo_url,
          local.ai_echo_reactive_url,
          local.ai_echo_autopilot_url,
        ]))
      }
      env {
        name  = "MAX_RUNNERS"
        value = "100"
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }

      # Per-user SaaS env vars.
      dynamic "env" {
        for_each = local.gateway_saas_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      # Stripe server-side API key (sk_live_... / sk_test_...). Mounted
      # only when the operator created the secret in Secret Manager AND
      # passed its id via stripe_api_key_secret_id. Empty id => slice is
      # empty => no env block emitted => cmd/gateway logs
      # "STRIPE_API_KEY empty, falling back to stub" on boot.
      dynamic "env" {
        for_each = var.stripe_api_key_secret_id != "" ? [1] : []
        content {
          name = "STRIPE_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.stripe_api_key_secret_id
              version = "latest"
            }
          }
        }
      }

      ports {
        container_port = 8080
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

# ---------------------------------------------------------------------------
# UI services (admin, dash, tester, frontend) -- invoked by browsers
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "admin" {
  project             = var.project_id
  location            = var.region
  name                = "admin"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["admin"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["admin"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["admin"]
      resources {
        limits = {
          cpu    = var.service_sizing["admin"].cpu
          memory = var.service_sizing["admin"].memory
        }
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "admin"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      # Same per-user SaaS env vars as the gateway. The admin BFF
      # needs IDENTITY_PROVIDER to authenticate operators against
      # the same identity layer.
      dynamic "env" {
        for_each = local.admin_saas_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "dash" {
  project             = var.project_id
  location            = var.region
  name                = "dash"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["dash"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["dash"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["dash"]
      resources {
        limits = {
          cpu    = var.service_sizing["dash"].cpu
          memory = var.service_sizing["dash"].memory
        }
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "dash"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "tester" {
  project             = var.project_id
  location            = var.region
  name                = "tester"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["tester"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["tester"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["tester"]
      resources {
        limits = {
          cpu    = var.service_sizing["tester"].cpu
          memory = var.service_sizing["tester"].memory
        }
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "tester"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      # Operator-only defence in depth. Pairs with the Cloud Run IAM
      # gate in modules/cloud-run-iam (admin/dash/tester have NO
      # allUsers binding). If anyone ever regresses the IAM binding,
      # cmd/tester rejects requests without an Authorization header
      # at the application layer. See docs/ops/operator_access.md.
      env {
        name  = "TESTER_REQUIRE_AUTH"
        value = "true"
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "frontend" {
  project             = var.project_id
  location            = var.region
  name                = "frontend"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["frontend"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["frontend"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["frontend"]
      resources {
        limits = {
          cpu    = var.service_sizing["frontend"].cpu
          memory = var.service_sizing["frontend"].memory
        }
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      dynamic "env" {
        for_each = local.frontend_saas_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "frontend"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }

      # Firebase web config for /config.js -> window.ENV. The /login
      # static page reads these to decide whether to render the
      # Firebase email/password flow. Empty when identity_provider !=
      # firebase, which makes the page fall back to LOCAL mode (no
      # Firebase SDK loaded) for the LOCAL_DEV_USER_EMAIL fixture user.
      dynamic "env" {
        for_each = local.frontend_firebase_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

# ---------------------------------------------------------------------------
# Cloud Run runner agents -- invoked by gateway via push subscription
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "runner_autopilot" {
  project             = var.project_id
  location            = var.region
  name                = "runner-autopilot"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["runner_autopilot"].max_instances
    }

    # ADK Python agents must stay serial — concurrent inference in
    # the same process interleaves agent state. Variable validation
    # enforces this stays at 1.
    max_instance_request_concurrency = var.service_concurrency["runner_autopilot"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["runner_autopilot"]
      resources {
        limits = {
          cpu    = var.service_sizing["runner_autopilot"].cpu
          memory = var.service_sizing["runner_autopilot"].memory
        }
      }
      dynamic "env" {
        for_each = concat(local.common_env, local.agent_env)
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "runner_autopilot"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "runner_cloudrun" {
  project             = var.project_id
  location            = var.region
  name                = "runner-cloudrun"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["runner_cloudrun"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["runner_cloudrun"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["runner_cloudrun"]
      resources {
        limits = {
          cpu    = var.service_sizing["runner_cloudrun"].cpu
          memory = var.service_sizing["runner_cloudrun"].memory
        }
      }
      dynamic "env" {
        for_each = concat(local.common_env, local.agent_env)
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      # AGENT_NAME explicitly set to runner_cloudrun (gotcha #3 in the
      # OSS deploy plan: prior bug had this defaulting to runner_gke).
      env {
        name  = "AGENT_NAME"
        value = "runner_cloudrun"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "leadership_runner_autopilot" {
  project             = var.project_id
  location            = var.region
  name                = "leadership-runner-autopilot"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["leadership_runner_autopilot"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["leadership_runner_autopilot"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["leadership_runner_autopilot"]
      resources {
        limits = {
          cpu    = var.service_sizing["leadership_runner_autopilot"].cpu
          memory = var.service_sizing["leadership_runner_autopilot"].memory
        }
      }
      dynamic "env" {
        for_each = concat(local.common_env, local.agent_env)
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "leadership_runner_autopilot"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "leadership_runner" {
  project             = var.project_id
  location            = var.region
  name                = "leadership-runner"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["leadership_runner"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["leadership_runner"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["leadership_runner"]
      resources {
        limits = {
          cpu    = var.service_sizing["leadership_runner"].cpu
          memory = var.service_sizing["leadership_runner"].memory
        }
      }
      dynamic "env" {
        for_each = concat(local.common_env, local.agent_env)
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "leadership_runner"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "ai_echo" {
  project             = var.project_id
  location            = var.region
  name                = "ai-echo"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["ai_echo"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["ai_echo"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["ai_echo"]
      resources {
        limits = {
          cpu    = var.service_sizing["ai_echo"].cpu
          memory = var.service_sizing["ai_echo"].memory
        }
      }
      dynamic "env" {
        for_each = concat(local.common_env, local.agent_env)
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "ai_echo"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "ai_echo_reactive" {
  project             = var.project_id
  location            = var.region
  name                = "ai-echo-reactive"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["ai_echo_reactive"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["ai_echo_reactive"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["ai_echo_reactive"]
      resources {
        limits = {
          cpu    = var.service_sizing["ai_echo_reactive"].cpu
          memory = var.service_sizing["ai_echo_reactive"].memory
        }
      }
      dynamic "env" {
        for_each = concat(local.common_env, local.agent_env)
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "ai_echo_reactive"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_service" "ai_echo_autopilot" {
  project             = var.project_id
  location            = var.region
  name                = "ai-echo-autopilot"
  deletion_protection = false

  template {
    service_account = var.compute_sa_email
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["ai_echo_autopilot"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["ai_echo_autopilot"]
    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image_tags["ai_echo_autopilot"]
      resources {
        limits = {
          cpu    = var.service_sizing["ai_echo_autopilot"].cpu
          memory = var.service_sizing["ai_echo_autopilot"].memory
        }
      }
      dynamic "env" {
        for_each = concat(local.common_env, local.agent_env)
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
      env {
        name  = "AGENT_NAME"
        value = "ai_echo_autopilot"
      }
      env {
        name  = "GATEWAY_INTERNAL_URL"
        value = google_cloud_run_v2_service.gateway.uri
      }
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }
      ports { container_port = 8080 }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}

# ---------------------------------------------------------------------------
# cmd/billing Stripe webhook receiver
#
# Deployed only when enable_billing_service=true. The image_tags map MUST
# include a "billing" key when this is enabled, otherwise apply fails
# with a clear "key billing missing" error rather than a confusing nil
# ref.
#
# Public ingress: Stripe Dashboard pushes webhook events to a URL the
# operator copies from the `billing_url` output. The handler refuses
# every event whose HMAC signature doesn't match STRIPE_WEBHOOK_SECRET,
# so leaving the URL public is safe.
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "billing" {
  count = var.enable_billing_service ? 1 : 0

  project             = var.project_id
  location            = var.region
  name                = "billing"
  deletion_protection = false

  # PUBLIC ingress: Stripe needs to reach the /webhook endpoint from
  # the open internet.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.compute_sa_email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.service_sizing["billing"].max_instances
    }

    max_instance_request_concurrency = var.service_concurrency["billing"]

    vpc_access {
      network_interfaces {
        network    = var.vpc_network_name
        subnetwork = var.vpc_subnet_name
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.image_tags["billing"]

      resources {
        limits = {
          cpu    = var.service_sizing["billing"].cpu
          memory = var.service_sizing["billing"].memory
        }
      }

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      dynamic "env" {
        for_each = local.billing_saas_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      env {
        name  = "AGENT_NAME"
        value = "billing"
      }

      # Cloud Run injects PORT=8080 itself; cmd/billing reads $PORT
      # via the standard Go pattern. We still set BILLING_PORT for
      # local-dev parity (.env defines BILLING_PORT=9122).
      env {
        name  = "BILLING_PORT"
        value = "8080"
      }

      # Postgres password (paired with ALLOYDB_HOST/PORT/USER/DATABASE
      # from common_env). cmd/billing's buildDBConnStr falls back to
      # ALLOYDB_* when DATABASE_URL is empty, so we don't need to
      # construct a libpq URL here.
      env {
        name = "ALLOYDB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = var.database_secret_id
            version = "latest"
          }
        }
      }

      # Stripe webhook signing secret (whsec_...). Mounted only when
      # the operator created the secret in Secret Manager AND passed
      # its id via stripe_webhook_secret_id. With billing_provider=stripe
      # the handler refuses to start when this is empty -- a misconfigured
      # deploy fails fast at boot rather than silently 401-ing every
      # Stripe event.
      dynamic "env" {
        for_each = var.stripe_webhook_secret_id != "" ? [1] : []
        content {
          name = "STRIPE_WEBHOOK_SECRET"
          value_source {
            secret_key_ref {
              secret  = var.stripe_webhook_secret_id
              version = "latest"
            }
          }
        }
      }

      ports {
        container_port = 8080
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }
}
