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
# One-shot import blocks
# =============================================================================
# Identity Platform is enabled implicitly the first time anything
# touches the Firebase Auth API (Firebase Console first-time setup,
# Identity Toolkit API enable, etc.). When that happens out-of-band,
# `google_identity_platform_config.default` cannot CREATE -- the
# server returns "Identity Platform has already been enabled for this
# project". This import block adopts the existing config into TF
# state on the next apply, after which it can be managed normally
# (authorized_domains, sign-in providers, etc).
#
# `for_each` mirrors the resource's `count` predicate so the address
# `google_identity_platform_config.default[0]` is always reachable
# when the block fires. During `tf-apply-base` (enable_services=false)
# the for_each is empty so the block is a no-op.
#
# Safe to leave in place forever -- Terraform skips import blocks
# whose target is already in state. Safe to delete after the first
# successful apply, but harmless to keep.
import {
  for_each = var.identity_provider == "firebase" && var.enable_services ? toset(["this"]) : toset([])
  to       = google_identity_platform_config.default[0]
  id       = "projects/${var.project_id}/config"
}
