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

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PortalService } from './portal.service';

function installFetch(spy: ReturnType<typeof vi.fn>): void {
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: spy,
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('PortalService', () => {
  let svc: PortalService;

  beforeEach(() => {
    svc = new PortalService();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('POSTs /api/v1/billing/portal with return_url and returns redirect on valid HTTPS URL', async () => {
    const spy = vi.fn().mockResolvedValue(
      jsonResponse({ url: 'https://billing.stripe.com/p/session_123', provider: 'stripe' }),
    );
    installFetch(spy);

    const outcome = await svc.openPortal();

    expect(spy).toHaveBeenCalledTimes(1);
    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/billing/portal');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(String(body['return_url'])).toContain('/#/settings/billing');
    expect(outcome).toEqual({
      outcome: 'redirect',
      url: 'https://billing.stripe.com/p/session_123',
      provider: 'stripe',
    });
  });

  it('maps 503, 409, 4xx, unsafe URL, and network errors accurately', async () => {
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ provider: 'stub', error: 'portal off' }, 503))
      .mockResolvedValueOnce(jsonResponse({ error: 'no customer yet' }, 409))
      .mockResolvedValueOnce(jsonResponse({ error: 'bad return_url' }, 400))
      .mockResolvedValueOnce(jsonResponse({ url: '//evil.example/portal' }, 200))
      .mockRejectedValueOnce(new TypeError('offline'));
    installFetch(spy);

    expect(await svc.openPortal()).toEqual({
      outcome: 'unavailable',
      provider: 'stub',
      message: 'portal off',
    });
    expect(await svc.openPortal()).toEqual({
      outcome: 'no_customer',
      message: 'no customer yet',
    });
    expect(await svc.openPortal()).toEqual({
      outcome: 'error',
      message: 'bad return_url',
      status: 400,
    });
    expect(await svc.openPortal()).toEqual({
      outcome: 'error',
      message: 'Invalid billing portal redirect URL.',
      status: 200,
    });
    expect(await svc.openPortal()).toEqual({
      outcome: 'error',
      message: 'Network error.',
      status: 0,
    });
  });
});
