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

output "web_api_key" {
  description = "The Firebase Web API key for the registered Web App (used by the SPA, not a secret)."
  value       = data.google_firebase_web_app_config.default.api_key
  sensitive   = true # treat as sensitive in TF output even though it's project-public
}

output "auth_domain" {
  description = "Firebase Auth domain (typically <project>.firebaseapp.com)."
  value       = data.google_firebase_web_app_config.default.auth_domain
}

output "project_id" {
  description = "The Firebase project ID (== the GCP project ID)."
  value       = google_firebase_project.default.project
}

output "app_id" {
  description = "Firebase Web App ID (1:NNNNN:web:NNNN). Identifies the Web App in the Firebase project."
  value       = google_firebase_web_app.default.app_id
}

output "messaging_sender_id" {
  description = "Firebase Cloud Messaging sender ID (numeric project number)."
  value       = data.google_firebase_web_app_config.default.messaging_sender_id
}

output "storage_bucket" {
  description = "Default Firebase Storage bucket (typically <project>.appspot.com)."
  value       = data.google_firebase_web_app_config.default.storage_bucket
}
