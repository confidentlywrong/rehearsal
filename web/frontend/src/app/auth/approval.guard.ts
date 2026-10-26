/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * approvalGuard — manual-approval flow (2026-06-26).
 *
 * Mounted as `canActivate` on every protected route. Calls
 * `IdentityService.ensureInitialized()` (the same idempotent
 * /me + /me/init handshake the rest of the SPA uses) and inspects
 * the returned status.
 *
 * Outcomes:
 *   - status === "approved" or the row is not yet provisioned (the
 *     gateway returns needs_init=true on the first call, then the
 *     init endpoint inserts the row in StatusPending) → allow.
 *
 *   - status === "pending" or "rejected"  → redirect to
 *     /pending-approval so the user sees the friendly holding
 *     screen instead of the dashboard's failed XHRs.
 *
 *   - /me unreachable (gateway down / 5xx)  → fail open. Better to
 *     show the dashboard with broken panels than to lock the user
 *     out behind a screen that can't ever recover.
 *
 * The guard does NOT run on /pending-approval itself — that route
 * mounts its own check that does the reverse direction (redirect
 * away once status flips to "approved").
 */

import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';

import { IdentityService } from './identity.service';

export const approvalGuard: CanActivateFn = async () => {
  const identity = inject(IdentityService);
  const router = inject(Router);

  // ensureInitialized runs the idempotent /me + /me/init handshake.
  // For a brand-new signup it creates the users row server-side
  // (which lands in StatusPending), then re-fetches /me so the
  // status field is populated here.
  const snap = await identity.ensureInitialized();

  // Gateway unreachable or returned a non-2xx (other than needs_init
  // which the service resolves internally): fail open so a backend
  // outage does not turn into "every user blocked at the gate".
  if (!snap.available || !snap.data?.user) {
    return true;
  }

  const status = snap.data.user.status || '';
  if (status === '' || status === 'approved') {
    return true;
  }
  // Pending or rejected → bounce to the holding screen.
  return router.createUrlTree(['/pending-approval']);
};
