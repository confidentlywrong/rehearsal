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
import { Subject } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { IdentityService } from './identity.service';
import { UserMenuComponent } from './user-menu.component';

describe('UserMenuComponent & IdentityService regression suite', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function createMenu(ensureInitialized = vi.fn()) {
    const controls$ = new Subject<null>();
    const unsubscribeSpy = vi.fn();
    const fakeObservable = {
      subscribe: (fn: (v: null) => void) => {
        const sub = controls$.subscribe(fn);
        return {
          unsubscribe: () => {
            unsubscribeSpy();
            sub.unsubscribe();
          },
        };
      },
    };
    const cdr = { markForCheck: vi.fn() };
    const comp = new UserMenuComponent(
      { ensureInitialized } as unknown as IdentityService,
      { signOut: vi.fn() } as any,
      cdr as any,
      { current: null, controls$: fakeObservable } as any,
    );
    return { comp, cdr, controls$, unsubscribeSpy };
  }

  it('bounds pollInitMe() retries at MAX_INIT_ATTEMPTS on repeated 503 responses', async () => {
    const ensureInitialized = vi.fn().mockResolvedValue({ available: false, status: 503 });
    const { comp } = createMenu(ensureInitialized);

    await comp.ngOnInit();
    await vi.runAllTimersAsync();

    expect(ensureInitialized).toHaveBeenCalledTimes(5);
    expect(comp.me).toBeNull();
    expect((comp as unknown as { retryTimer: unknown }).retryTimer).toBeNull();
    comp.ngOnDestroy();
  });

  it('short-circuits pollInitMe() immediately on terminal 401, 403, and 410 statuses', async () => {
    for (const status of [401, 403, 410]) {
      const ensureInitialized = vi.fn().mockResolvedValue({ available: false, status });
      const { comp } = createMenu(ensureInitialized);

      await comp.ngOnInit();
      await Promise.resolve();

      expect(ensureInitialized).toHaveBeenCalledTimes(1);
      expect((comp as unknown as { retryTimer: unknown }).retryTimer).toBeNull();
      comp.ngOnDestroy();
    }
  });

  it('resolves pending sleep promise, clears retryTimer, and unsubscribes agentControlsSub on ngOnDestroy', async () => {
    const ensureInitialized = vi.fn().mockResolvedValue({ available: false, status: 503 });
    const { comp, controls$, cdr, unsubscribeSpy } = createMenu(ensureInitialized);

    await comp.ngOnInit();
    await Promise.resolve();
    expect((comp as unknown as { retryTimer: unknown }).retryTimer).not.toBeNull();
    expect((comp as unknown as { retryResolve: unknown }).retryResolve).not.toBeNull();

    comp.ngOnDestroy();

    expect((comp as unknown as { retryTimer: unknown }).retryTimer).toBeNull();
    expect((comp as unknown as { retryResolve: unknown }).retryResolve).toBeNull();
    expect(unsubscribeSpy).toHaveBeenCalledTimes(1);

    // Late emission after destroy must not trigger markForCheck
    const callsBefore = cdr.markForCheck.mock.calls.length;
    controls$.next(null);
    expect(cdr.markForCheck.mock.calls.length).toBe(callsBefore);
    expect(ensureInitialized).toHaveBeenCalledTimes(1);
  });

  it('guards post-await state mutation when destroyed while ensureInitialized() is in flight', async () => {
    let resolveInit!: (val: unknown) => void;
    const ensureInitialized = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveInit = resolve;
        }),
    );
    const { comp, cdr } = createMenu(ensureInitialized);

    await comp.ngOnInit();
    comp.ngOnDestroy();

    resolveInit({
      available: true,
      status: 200,
      data: {
        needs_init: false,
        user: {
          id: 'u-1',
          email: 'exec@example.com',
          display_name: 'Exec',
          timezone: 'UTC',
          plan_tier: 'pro',
          is_superadmin: false,
        },
      },
    });
    await Promise.resolve();

    expect(comp.me).toBeNull();
    expect(cdr.markForCheck).not.toHaveBeenCalled();
  });

  it('populates me immediately on 200 without scheduling retryTimer', async () => {
    const payload = {
      needs_init: false,
      user: {
        id: 'u-2',
        email: 'leader@example.com',
        display_name: 'Leader',
        timezone: 'UTC',
        plan_tier: 'starter',
        is_superadmin: false,
      },
    };
    const ensureInitialized = vi.fn().mockResolvedValue({
      available: true,
      status: 200,
      data: payload,
    });
    const { comp, cdr } = createMenu(ensureInitialized);

    await comp.ngOnInit();
    await Promise.resolve();

    expect(comp.me).toEqual(payload);
    expect(cdr.markForCheck).toHaveBeenCalledTimes(1);
    expect((comp as unknown as { retryTimer: unknown }).retryTimer).toBeNull();
    comp.ngOnDestroy();
  });

  it('IdentityService.ensureInitialized() returns failure snapshot when /me/init fails or throws', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_init: true }), { status: 200 }),
      )
      .mockResolvedValueOnce(new Response('Service Unavailable', { status: 503 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_init: true }), { status: 200 }),
      )
      .mockRejectedValueOnce(new Error('network down'));

    vi.stubGlobal('fetch', fetchMock);
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const svc = new IdentityService();
    const resHttpErr = await svc.ensureInitialized();
    expect(resHttpErr).toEqual({ available: false, status: 503 });

    const resNetErr = await svc.ensureInitialized();
    expect(resNetErr).toEqual({ available: false, status: 0 });
    expect(warnSpy).toHaveBeenCalledTimes(2);
  });
});
