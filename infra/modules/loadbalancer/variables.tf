# Copyright 2026 Christian Haeussinger
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
  type    = string
  default = "us-central1"
}

variable "environment" {
  description = "Name used in resource naming (e.g. oss, prod). Lowercase + hyphens; must be unique per LB in the project (the global address + URL map share the project namespace)."
  type        = string
  default     = "oss"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,28}$", var.environment))
    error_message = "environment must be lowercase alphanumeric/hyphen, 1-29 chars (Cloud LB resource-name limits)."
  }
}

variable "services" {
  description = "Map of service key -> Cloud Run service name. Every key referenced by var.domain_names must exist here. Defaults match the cloud-run-services module's outputs."
  type        = map(string)
}

variable "domain_names" {
  description = "Map of service key -> fully qualified domain name (e.g. {frontend = \"app.example.com\", gateway = \"api.example.com\"}). Each domain becomes a host_rule in the LB URL map, and the managed SSL cert covers every value. Empty map means the root module shouldn't instantiate this module at all (count = 0)."
  type        = map(string)

  validation {
    condition     = alltrue([for d in values(var.domain_names) : can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", d))])
    error_message = "Every domain in var.domain_names must be a lowercase FQDN (e.g. app.example.com). Trailing dots, uppercase, and IP literals are rejected to keep the managed SSL cert provisioning predictable."
  }
}

variable "dns_zone_name" {
  description = "Optional Cloud DNS managed_zone name. When set, the module publishes one A record per entry in var.domain_names pointing at the LB IP. When empty (default), the operator updates DNS by hand \u2014 typical when the zone lives outside this GCP project."
  type        = string
  default     = ""
}

variable "enable_http_redirect" {
  description = "When true (default), a port-80 forwarding rule on the same IP issues a 301 to https://. Disable only when you front the LB with another redirector (e.g. Cloudflare in proxy mode) that already does this."
  type        = bool
  default     = true
}

variable "backend_timeout_seconds" {
  description = "Cloud Run backend response timeout. Cloud Run itself caps a single request at 60 minutes, but the LB-side timeout here is the relevant ceiling for sync requests. 30s matches the Cloud Run default request timeout."
  type        = number
  default     = 30

  validation {
    condition     = var.backend_timeout_seconds >= 1 && var.backend_timeout_seconds <= 86400
    error_message = "backend_timeout_seconds must be in [1, 86400] (Google Cloud LB limit)."
  }
}

variable "enable_cdn_for" {
  description = "Subset of service keys to enable Cloud CDN on. Static frontends benefit; authenticated API backends do not (CDN caching would cross user boundaries). Empty disables CDN everywhere."
  type        = list(string)
  default     = []
}
