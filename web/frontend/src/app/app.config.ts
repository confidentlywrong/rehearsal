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

import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideAnimations } from '@angular/platform-browser/animations';
import { provideRouter, withHashLocation } from '@angular/router';
import { provideMarkdown } from 'ngx-markdown';
import { provideAuth } from './auth/auth.service';
import { APP_ROUTES } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideAnimations(),
    provideMarkdown(),
    // SaaS Phase 1: additive auth layer. No-op when SAAS_MODE=off, so
    // single-tenant `make start` behavior is byte-identical.
    // See concepts/multi_tenant_saas_plan.md §4.2.
    ...provideAuth(),
    // SaaS Phase 9: introduce Angular Router so /settings/* lives
    // under the same SPA as the dashboard. Hash-based location so
    // the BFF's catch-all SPA fallback keeps working without a
    // server-side route rewrite (every URL inside the SPA renders
    // index.html; the router does the in-page navigation).
    provideRouter(APP_ROUTES, withHashLocation()),
  ],
};
