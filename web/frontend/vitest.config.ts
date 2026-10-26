/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

import { defineConfig } from 'vitest/config';

// Minimal vitest config for SPA unit tests. jsdom env is required
// for tests that touch window / localStorage / document (e.g. the
// auth.service.spec.ts coverage of P2 #13 token refresh + App Check).
//
// Angular component tests still run via `ng test` (Karma + Jasmine);
// vitest is reserved for plain TS unit tests that don't need the
// Angular TestBed.
export default defineConfig({
  test: {
    // happy-dom exposes window.localStorage out of the box; jsdom 25
    // requires extra Node flags that are awkward to thread through CI.
    environment: 'happy-dom',
    include: ['src/**/*.{test,spec}.ts'],
    globals: false,
  },
});
