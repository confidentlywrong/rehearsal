# Copyright 2026 The Confidently Wrong Authors
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
# loadbalancer: External HTTPS Load Balancer in front of Cloud Run.
#
# Production path for E3a. Sibling module to iap-gclb (which forces every
# request through IAP). This module is plain mTLS-terminating HTTPS with
# no IAP \u2014 the access-control story is whatever the Cloud Run service
# itself implements (Firebase ID tokens, public, IAM-invoker).
#
# When `var.domain_names` is empty the root module skips this whole
# module (count = 0), which is what keeps the OSS / *.run.app default
# byte-identical to the pre-E3 deploy.
#
# Topology (one of each, per LB; one LB per environment):
#
#                     Cloud DNS (A: app.example.com  -> LB_IP)
#                                          |
#                            +-------------v-------------+
#                            |  global_forwarding_rule   |
#                            |  (port 443, LB_IP)        |
#                            +-------------+-------------+
#                                          |
#                            +-------------v-------------+
#                            |  target_https_proxy        |
#                            |  -> managed_ssl_cert       |
#                            +-------------+-------------+
#                                          |
#                            +-------------v-------------+
#                            |  url_map                   |
#                            |  Host: app.example.com    -> frontend backend
#                            |  Host: api.example.com    -> gateway  backend
#                            |  Host: admin.example.com  -> admin    backend
#                            |  Host: hooks.example.com  -> billing  backend
#                            +---------------------------+
#                                          |
#                            +-------------v-------------+
#                            |  backend_service           |
#                            |  -> serverless NEG         |
#                            |  -> Cloud Run service      |
#                            +---------------------------+
#
# Operator post-deploy steps (DNS is intentionally outside this module
# because it usually lives in a separate project/zone):
#
#   1. terraform output loadbalancer_ip            # e.g. 34.110.x.x
#   2. Point each FQDN in var.domain_names at that A record. If
#      var.dns_zone_name is set, the module creates the A records
#      itself; otherwise the operator updates DNS by hand.
#   3. Wait 5-60 minutes for the managed SSL cert to provision.
#      Status: `gcloud compute ssl-certificates describe ...`. Cert
#      stays in PROVISIONING until DNS resolves and at least one
#      ACME challenge succeeds.
#   4. Hit the URLs and verify the response.
# =============================================================================

# One serverless NEG per Cloud Run service that the LB will route to.
# Each NEG is region-pinned to var.region; the LB itself is global
# (anycast IP) and fans in through the NEGs.
#
# create_before_destroy lets a `terraform apply` swap a NEG (e.g. the
# region changes) without taking the LB offline mid-update.
resource "google_compute_region_network_endpoint_group" "negs" {
  for_each              = var.services
  name                  = "${var.environment}-${each.key}-neg"
  project               = var.project_id
  network_endpoint_type = "SERVERLESS"
  region                = var.region

  cloud_run {
    service = each.value
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Backend service per Cloud Run target. We deliberately do NOT enable
# IAP here \u2014 use the iap-gclb module instead if you want IAP. This
# module is the plain "give me a brand domain in front of Cloud Run"
# path.
#
# Cloud CDN is left off by default because most workloads here are
# authenticated API traffic; flipping it on for the static frontend
# is an opt-in via var.enable_cdn_for.
resource "google_compute_backend_service" "backends" {
  for_each = var.services

  name                  = "${var.environment}-${each.key}-backend"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  port_name             = "http"
  protocol              = "HTTP"
  timeout_sec           = var.backend_timeout_seconds

  enable_cdn = contains(var.enable_cdn_for, each.key)

  backend {
    group = google_compute_region_network_endpoint_group.negs[each.key].id
  }

  # Stackdriver request logs at 100% sampling. The OSS deploy is
  # small enough that the cost is negligible; the security trail
  # justifies it.
  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

# URL map fans by Host header. Each domain in var.domain_names that
# matches a known service key becomes one host_rule -> path_matcher
# pair routing every path on that host to the corresponding backend.
#
# default_service is the first key in var.domain_names sorted
# alphabetically \u2014 hitting the LB IP directly (no Host header) or
# a stray domain lands there. We pick a deterministic fallback
# rather than 404'ing so a misconfigured DNS still returns
# something useful.
locals {
  # services keys that the operator actually exposed a domain for.
  routed_keys = sort([for k in keys(var.domain_names) : k if contains(keys(var.services), k)])
  # default_backend is the first routed backend, alphabetically.
  # Falls back to "frontend" if it exists, then to the first key in
  # var.services; the conditional handles the "no domains" edge
  # case which shouldn't happen at apply time (the root module
  # gates this whole module on length(domain_names) > 0) but keeps
  # the expression total.
  default_backend_key = length(local.routed_keys) > 0 ? local.routed_keys[0] : sort(keys(var.services))[0]
}

resource "google_compute_url_map" "url_map" {
  name            = "${var.environment}-url-map"
  project         = var.project_id
  default_service = google_compute_backend_service.backends[local.default_backend_key].id

  dynamic "host_rule" {
    for_each = var.domain_names
    content {
      hosts        = [host_rule.value]
      path_matcher = host_rule.key
    }
  }

  dynamic "path_matcher" {
    for_each = var.domain_names
    content {
      name            = path_matcher.key
      default_service = google_compute_backend_service.backends[path_matcher.key].id
    }
  }
}

# Single managed SSL certificate covering every domain (each one a
# SAN). One cert is simpler than per-host certs and stays within the
# 100-domain-per-cert Google limit; if you need >100, split into
# cert groups the way iap-gclb does.
#
# The cert is PROVISIONING until DNS resolves \u2014 see module header.
# A re-apply that changes the domain list creates a NEW cert (name
# changes via the hash) and swaps the proxy in a single transaction
# to avoid the multi-minute "no valid cert" gap.
resource "google_compute_managed_ssl_certificate" "cert" {
  # Cert name includes a content hash of the domain list so a domain
  # change forces a recreate (managed certs are immutable on the
  # `managed.domains` field).
  name    = "${var.environment}-lb-cert-${substr(sha256(jsonencode(values(var.domain_names))), 0, 8)}"
  project = var.project_id

  managed {
    domains = values(var.domain_names)
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_compute_target_https_proxy" "https_proxy" {
  name             = "${var.environment}-https-proxy"
  project          = var.project_id
  url_map          = google_compute_url_map.url_map.id
  ssl_certificates = [google_compute_managed_ssl_certificate.cert.id]
}

# Reserved global IP \u2014 the value the operator points DNS at. Stays
# stable across re-applies (the name is fixed and the resource is
# never destroyed by routine config changes).
resource "google_compute_global_address" "lb_ip" {
  name    = "${var.environment}-lb-ip"
  project = var.project_id
}

resource "google_compute_global_forwarding_rule" "https" {
  name                  = "${var.environment}-https-fr"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.https_proxy.id
  port_range            = "443"
  ip_address            = google_compute_global_address.lb_ip.id
}

# ----- HTTP -> HTTPS redirect (optional, default on) ----------------------
#
# Without this, hitting `http://app.example.com` returns a TCP RST or
# the user's browser falls back to whatever it cached. Better UX is
# a 301 to the https:// equivalent.
#
# Implemented as a second URL map whose only rule is a redirect,
# fronted by a target_http_proxy on port 80 sharing the same global
# IP as the HTTPS forwarding rule above.
resource "google_compute_url_map" "https_redirect" {
  count   = var.enable_http_redirect ? 1 : 0
  name    = "${var.environment}-https-redirect"
  project = var.project_id

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "http_proxy" {
  count   = var.enable_http_redirect ? 1 : 0
  name    = "${var.environment}-http-proxy"
  project = var.project_id
  url_map = google_compute_url_map.https_redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count                 = var.enable_http_redirect ? 1 : 0
  name                  = "${var.environment}-http-fr"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_http_proxy.http_proxy[0].id
  port_range            = "80"
  ip_address            = google_compute_global_address.lb_ip.id
}

# ----- DNS records (optional) ---------------------------------------------
#
# When the operator provides a managed_zone name we publish A records
# pointing every domain at the LB IP. When dns_zone_name is empty the
# operator points DNS by hand (typical for zones hosted outside
# Google Cloud or in a different project).
resource "google_dns_record_set" "a_records" {
  for_each = var.dns_zone_name != "" ? var.domain_names : {}

  project      = var.project_id
  managed_zone = var.dns_zone_name
  name         = "${each.value}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.lb_ip.address]
}
