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
  description = "GCP project ID"
  type        = string
}

variable "project_number" {
  description = "GCP project number (used to identify the Compute Engine default SA, which is also the default Cloud Build SA, for Secret Manager access)"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "instance_name" {
  description = "Cloud SQL instance name"
  type        = string
  default     = "race-condition-postgres"
}

variable "database_version" {
  description = "PostgreSQL version"
  type        = string
  default     = "POSTGRES_16"
}

variable "tier" {
  description = "Machine tier for Cloud SQL instance"
  type        = string
  default     = "db-custom-1-3840"
}

variable "disk_size" {
  description = "Disk size in GB"
  type        = number
  default     = 10
}

variable "disk_type" {
  description = "Disk type (PD_SSD or PD_HDD)"
  type        = string
  default     = "PD_SSD"
}

variable "availability_type" {
  description = "Availability type (ZONAL or REGIONAL)"
  type        = string
  default     = "ZONAL"
}

variable "deletion_protection" {
  description = "Whether to enable deletion protection on the instance. Defaults to true \u2014 Cloud SQL holds tenant business data and accidental deletion is unrecoverable without a backup. Operators must explicitly set this to false in tfvars when they want to tear down a deploy."
  type        = bool
  default     = true
}

variable "vpc_id" {
  description = "VPC network ID for private IP"
  type        = string
}

variable "private_vpc_connection_id" {
  description = "Private VPC connection ID (for depends_on)"
  type        = string
}

variable "compute_sa" {
  description = "Compute service account member string (serviceAccount:email)"
  type        = string
}

variable "agent_engine_sa_email" {
  description = "Agent Engine service account email for IAM database access"
  type        = string
}

variable "iam_users" {
  description = "List of IAM users for database access (format: user:email@domain)"
  type        = list(string)
  default     = []
}

variable "initial_password" {
  description = "Initial password for the postgres user"
  type        = string
  sensitive   = true
}

# G9: PITR + backup retention knobs. Previous behavior was the
# google_sql_database_instance defaults (PITR off, 7 daily backups
# retained, 7 days of transaction logs when PITR is on). Defaults
# below preserve that exact behavior so existing deploys are byte-
# identical; operators flip point_in_time_recovery_enabled=true in
# terraform.tfvars to opt into PITR and tune the retention windows.

variable "point_in_time_recovery_enabled" {
  description = "Enable Point-In-Time Recovery (PITR) for the Cloud SQL instance. Required to restore to any second within transaction_log_retention_days. Default false to preserve legacy behavior."
  type        = bool
  default     = false
}

variable "transaction_log_retention_days" {
  description = "Number of days of transaction logs to retain for PITR. Only consulted when point_in_time_recovery_enabled=true. Cloud SQL accepts 1..7. Default 7 (Cloud SQL maximum)."
  type        = number
  default     = 7
  validation {
    condition     = var.transaction_log_retention_days >= 1 && var.transaction_log_retention_days <= 7
    error_message = "transaction_log_retention_days must be between 1 and 7 (Cloud SQL caps it at 7)."
  }
}

variable "backup_retention_count" {
  description = "Number of automated backups to retain. Cloud SQL accepts 1..365. Default 7 to match the legacy implicit default."
  type        = number
  default     = 7
  validation {
    condition     = var.backup_retention_count >= 1 && var.backup_retention_count <= 365
    error_message = "backup_retention_count must be between 1 and 365."
  }
}
