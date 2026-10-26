# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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

output "zone_name" {
  value = google_dns_managed_zone.zone.name
}

output "name_servers" {
  value = google_dns_managed_zone.zone.name_servers
}

output "domain_suffix" {
  description = "Domain suffix without trailing dot (e.g., sim.example.com)"
  value       = trimsuffix(var.dns_name, ".")
}
