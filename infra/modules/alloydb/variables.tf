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

variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "vpc_id" {
  description = "VPC network ID for AlloyDB network config"
  type        = string
}

variable "private_vpc_connection_id" {
  description = "Private VPC connection ID (for depends_on)"
  type        = string
}

variable "cluster_id" {
  type    = string
  default = "am-cluster"
}

variable "instance_id" {
  type    = string
  default = "agent-memory"
}

variable "cpu_count" {
  type    = number
  default = 4
}

variable "initial_password" {
  description = "Initial password for the postgres user"
  type        = string
  sensitive   = true
}

variable "compute_sa" {
  description = "Compute SA member string for secret accessor IAM"
  type        = string
}

variable "agent_engine_sa_email" {
  description = "Agent Engine SA email for secret accessor IAM"
  type        = string
}

variable "project_number" {
  description = "GCP project number (for AlloyDB service agent IAM)"
  type        = string
}

variable "iam_users" {
  description = "List of user members for IAM database access (format: user:email@domain)"
  type        = list(string)
  default     = []
}

variable "deletion_policy" {
  description = "AlloyDB cluster deletion policy. \"DEFAULT\" (safer) refuses deletion if dependent instances exist \u2014 operator must explicitly destroy the primary first. \"FORCE\" cascades and wipes everything in one shot. We default to DEFAULT to prevent accidental data loss from a stray `tofu destroy`; flip to FORCE only when you genuinely want to tear the cluster down."
  type        = string
  default     = "DEFAULT"

  validation {
    condition     = contains(["DEFAULT", "FORCE"], var.deletion_policy)
    error_message = "deletion_policy must be one of: DEFAULT, FORCE."
  }
}
