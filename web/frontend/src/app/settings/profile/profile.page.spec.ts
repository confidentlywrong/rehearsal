/**
 * Copyright 2026 Christian Haeussinger
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

import '@angular/compiler';
import { ChangeDetectorRef } from '@angular/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ProfilePageComponent } from './profile.page';

describe('ProfilePageComponent lifecycle & timer guards', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function createPage(overrides?: {
    updateProfile?: ReturnType<typeof vi.fn>;
    requestPasswordReset?: ReturnType<typeof vi.fn>;
    deleteAccount?: ReturnType<typeof vi.fn>;
  }) {
    const cdr = { markForCheck: vi.fn() } as unknown as ChangeDetectorRef;
    const identity = { fetchMe: vi.fn() } as any;
    const profile = {
      fetchProfile: vi.fn(),
      updateProfile: overrides?.updateProfile ?? vi.fn().mockResolvedValue({ ok: true, status: 200 }),
      requestPasswordReset: overrides?.requestPasswordReset ?? vi.fn().mockResolvedValue({ ok: true, status: 200 }),
      deleteAccount: overrides?.deleteAccount ?? vi.fn().mockResolvedValue({ ok: true, status: 204 }),
    } as any;
    const auth = { signOut: vi.fn() } as any;
    const page = new ProfilePageComponent(identity, profile, auth, cdr);
    page.me = {
      needs_init: false,
      user: {
        id: 'u1',
        email: 'exec@example.com',
        display_name: 'Exec',
        timezone: 'UTC',
        plan_tier: 'pro',
        is_superadmin: false,
      },
    } as any;
    return { page, cdr, profile, auth };
  }

  it('clears saveTimeoutId and resetTimeoutId on ngOnDestroy and suppresses late timer callbacks', async () => {
    const { page, cdr } = createPage();
    await page.save();
    await page.sendPasswordReset();

    expect(page.saveState).toBe('saved');
    expect(page.resetState).toBe('done');
    expect((page as unknown as { saveTimeoutId?: number }).saveTimeoutId).toBeDefined();
    expect((page as unknown as { resetTimeoutId?: number }).resetTimeoutId).toBeDefined();

    (cdr.markForCheck as ReturnType<typeof vi.fn>).mockClear();
    page.ngOnDestroy();

    expect((page as unknown as { saveTimeoutId?: number }).saveTimeoutId).toBeUndefined();
    expect((page as unknown as { resetTimeoutId?: number }).resetTimeoutId).toBeUndefined();

    await vi.advanceTimersByTimeAsync(5000);
    expect(page.saveState).toBe('saved');
    expect(page.resetState).toBe('done');
    expect(cdr.markForCheck).not.toHaveBeenCalled();
  });

  it('guards post-await mutations in save, sendPasswordReset, and deleteAccount when destroyed mid-flight', async () => {
    let resolveSave!: (v: unknown) => void;
    let resolveReset!: (v: unknown) => void;
    let resolveDelete!: (v: unknown) => void;

    const { page, cdr, auth } = createPage({
      updateProfile: vi.fn(() => new Promise((r) => { resolveSave = r; })),
      requestPasswordReset: vi.fn(() => new Promise((r) => { resolveReset = r; })),
      deleteAccount: vi.fn(() => new Promise((r) => { resolveDelete = r; })),
    });

    page.confirmDeleteText = 'delete';
    const saveP = page.save();
    const resetP = page.sendPasswordReset();
    const delP = page.deleteAccount();

    (cdr.markForCheck as ReturnType<typeof vi.fn>).mockClear();
    page.ngOnDestroy();

    resolveSave({ ok: true, status: 200 });
    resolveReset({ ok: true, status: 200 });
    resolveDelete({ ok: true, status: 204 });
    await Promise.all([saveP, resetP, delP]);

    expect(page.saveState).toBe('saving');
    expect(page.resetState).toBe('pending');
    expect(page.deleteState).toBe('pending');
    expect(auth.signOut).not.toHaveBeenCalled();
    expect(cdr.markForCheck).not.toHaveBeenCalled();
  });
});
