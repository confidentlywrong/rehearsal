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

# Provision a Firebase project linkage + Web App on top of the existing
# GCP project, then read the auto-generated SDK config (apiKey,
# authDomain, ...) so the frontend BFF can serve it to the browser SPA.
#
# All resources require the google-beta provider.

# 1. Promote the GCP project to also be a Firebase project. Idempotent:
#    if the project is already linked to Firebase (e.g. via the Console),
#    `terraform apply` adopts it cleanly via `terraform import` and then
#    is a no-op on subsequent runs.
resource "google_firebase_project" "default" {
  provider = google-beta
  project  = var.project_id
}

# 2. Register a Web App under the Firebase project.
#    The display_name shows up in Firebase Console > Project Settings.
resource "google_firebase_web_app" "default" {
  provider     = google-beta
  project      = var.project_id
  display_name = var.web_app_display_name

  # Don't auto-delete the underlying Firebase App when the Terraform
  # resource is removed: that operation is destructive (revokes the
  # apiKey + every existing user's session). Operator can prune by
  # hand if truly needed.
  deletion_policy = "ABANDON"

  depends_on = [google_firebase_project.default]
}

# 3. Fetch the auto-generated SDK config (apiKey, authDomain,
#    messagingSenderId, appId). The apiKey is what the SPA needs to
#    initialize the Firebase JS SDK. It is NOT a secret: it identifies
#    the project, and per-key auth happens through Firebase Auth + the
#    project's authorized-domains list (configured under
#    Authentication > Settings > Authorized domains).
data "google_firebase_web_app_config" "default" {
  provider   = google-beta
  web_app_id = google_firebase_web_app.default.app_id
  project    = var.project_id
}
