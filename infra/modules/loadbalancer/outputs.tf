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

output "lb_ip" {
  description = "Reserved global IP for the LB. Operator points DNS A records at this value (or sets var.dns_zone_name to have the module do it)."
  value       = google_compute_global_address.lb_ip.address
}

output "service_urls" {
  description = "Map of service key -> https://<fqdn>. Only keys with a configured domain appear; *.run.app URLs for the rest come from the cloud_run_services module."
  value       = { for k, fqdn in var.domain_names : k => "https://${fqdn}" }
}

output "ssl_certificate_name" {
  description = "Managed SSL certificate resource name. Use this to watch provisioning status: `gcloud compute ssl-certificates describe <name> --global`. PROVISIONING is normal until DNS resolves."
  value       = google_compute_managed_ssl_certificate.cert.name
}

output "cors_allowed_origins" {
  description = "Comma-separated list of https://<fqdn> origins suitable for CORS_ALLOWED_ORIGINS. The root module passes this straight to cloud_run_services so the gateway/admin/frontend CORS middleware locks down to the brand domains automatically once the LB is in place."
  value       = join(",", [for fqdn in values(var.domain_names) : "https://${fqdn}"])
}
