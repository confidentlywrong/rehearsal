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
 * Top-level route table — Phase 9.
 *
 * Single SPA, two top-level shells:
 *   - `/`              — DashboardComponent (the live 3D simulation).
 *                        Pre-Phase 9, this was AppComponent itself.
 *   - `/settings/*`    — SettingsShellComponent (lazy children).
 *                        The shell renders a left-nav and a child
 *                        router-outlet; children are loadComponent()
 *                        so the dashboard bundle stays small.
 *
 * The catch-all redirects to `/` rather than rendering a 404 page —
 * keeps the single-tenant flow intact for anyone navigating to a
 * stale bookmark. SAAS-aware deep-links can be added later if needed.
 */

import { Routes } from '@angular/router';
import { DashboardComponent } from './dashboard.component';
import { approvalGuard } from './auth/approval.guard';

export const APP_ROUTES: Routes = [
  {
    path: '',
    pathMatch: 'full',
    component: DashboardComponent,
    canActivate: [approvalGuard],
  },
  {
    path: 'pending-approval',
    loadComponent: () =>
      import('./auth/pending-approval.page').then(m => m.PendingApprovalPageComponent),
  },
  {
    path: 'settings',
    canActivate: [approvalGuard],
    canActivateChild: [approvalGuard],
    loadComponent: () =>
      import('./settings/settings-shell.component').then(
        m => m.SettingsShellComponent,
      ),
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'memory' },
      {
        path: 'profile',
        loadComponent: () =>
          import('./settings/profile/profile.page').then(m => m.ProfilePageComponent),
      },
      {
        path: 'billing',
        loadComponent: () =>
          import('./settings/billing/billing.page').then(m => m.BillingPageComponent),
      },
      {
        path: 'memory',
        loadChildren: () =>
          import('./memory/memory.routes').then(m => m.MEMORY_ROUTES),
      },
    ],
  },
  { path: '**', redirectTo: '' },
];
