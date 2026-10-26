// Copyright 2026 The Confidently Wrong Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * Copyright 2026 Google LLC. Apache-2.0.
 *
 * MemoryService unit tests — exercises the request shape and the
 * error-mapping contract for /api/v1/memory/*. We mock window.fetch
 * directly because that is the only network primitive the service
 * uses.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';


import { AuthService } from '../auth/auth.service';
import { MemoryApiError, MemoryService } from './memory.service';

type FetchSpy = ReturnType<typeof vi.fn>;

function installFetch(spy: FetchSpy): void {
  Object.defineProperty(window, 'fetch', {
    configurable: true,
    value: spy,
  });
  // Also patch globalThis.fetch so the service can resolve it via
  // either lexical reference.
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

function noBodyResponse(status = 204): Response {
  return new Response(null, { status });
}

beforeEach(() => {
  

  // Re-installed per-test in each `it` block.
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('MemoryService.listAnalyses', () => {
  it('GETs /api/v1/memory/analyses with same-origin credentials and returns the array', async () => {
    const spy = vi.fn().mockResolvedValue(
      jsonResponse({
        analyses: [
          {
            id: 'a1',
            operator_id: 'op1',
            org_name: 'Acme',
            title: 'Q1 transformation',
            created_at: '2026-06-26T10:00:00Z',
          },
        ],
      }),
    );
    installFetch(spy);

    const svc = new MemoryService({ getAppCheckToken: async () => 'test-token' } as any);
    const out = await svc.listAnalyses();

    expect(spy).toHaveBeenCalledTimes(1);
    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/memory/analyses');
    expect(init.method).toBe('GET');
    expect(init.credentials).toBe('same-origin');
    expect(out).toHaveLength(1);
    expect(out[0].title).toBe('Q1 transformation');
  });

  it('returns [] when the gateway returns a 200 without an `analyses` field', async () => {
    installFetch(vi.fn().mockResolvedValue(jsonResponse({})));
    const out = await new MemoryService({ getAppCheckToken: async () => 'test-token' } as any).listAnalyses();
    expect(out).toEqual([]);
  });
});

describe('MemoryService.createAnalysis', () => {
  it('POSTs JSON with snake_case fields and returns the created analysis', async () => {
    const created = {
      id: 'a2',
      operator_id: 'op1',
      org_name: 'Acme',
      title: 'Phase 2 kickoff',
      created_at: '2026-06-26T10:00:00Z',
    };
    const spy = vi.fn().mockResolvedValue(jsonResponse(created, 201));
    installFetch(spy);

    const out = await new MemoryService({ getAppCheckToken: async () => 'test-token' } as any).createAnalysis({
      org_name: 'Acme',
      title: 'Phase 2 kickoff',
      description: 'Notes',
    });

    const [, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      org_name: 'Acme',
      title: 'Phase 2 kickoff',
      description: 'Notes',
    });
    expect(out.id).toBe('a2');
  });
});

describe('MemoryService error mapping', () => {
  it('throws MemoryApiError carrying the gateway-supplied error code on 4xx', async () => {
    installFetch(
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ error: 'invalid_label', detail: 'unknown label' }, 422),
        ),
    );
    await expect(
      new MemoryService({ getAppCheckToken: async () => 'test-token' } as any).createAnalysis({ org_name: 'a', title: 'b' }),
    ).rejects.toMatchObject({
      name: 'MemoryApiError',
      status: 422,
      code: 'invalid_label',
      detail: 'unknown label',
    });
  });

  it('falls back to "http_error" when the body is not JSON', async () => {
    installFetch(
      vi.fn().mockResolvedValue(
        new Response('boom', {
          status: 500,
          headers: { 'Content-Type': 'text/plain' },
        }),
      ),
    );
    try {
      await new MemoryService({ getAppCheckToken: async () => 'test-token' } as any).listAnalyses();
      throw new Error('expected throw');
    } catch (e) {
      expect(e).toBeInstanceOf(MemoryApiError);
      const err = e as MemoryApiError;
      expect(err.status).toBe(500);
      expect(err.code).toBe('http_error');
    }
  });
});

describe('MemoryService.uploadDocument', () => {
  // Minimal XHR mock that records calls and lets the test trigger events.
  class MockXHR {
    openArgs: unknown[] = [];
    sendArgs: unknown[] = [];
    headerCalls: Array<[string, string]> = [];
    status = 0;
    responseText = '';
    withCredentials = false;

    private _listeners: Record<string, Array<(ev?: unknown) => void>> = {};
    upload = {
      addEventListener: (_type: string, _cb: (ev: unknown) => void) => {
        /* no-op for this test */
      },
    };

    open(...args: unknown[]) {
      this.openArgs = args;
    }
    send(...args: unknown[]) {
      this.sendArgs = args;
    }
    setRequestHeader(name: string, value: string) {
      this.headerCalls.push([name, value]);
    }
    addEventListener(type: string, cb: (ev?: unknown) => void) {
      (this._listeners[type] ??= []).push(cb);
    }
    abort() {}

    /** Test helper: fire the 'load' event after setting status/responseText. */
    _fireLoad(status: number, body: unknown) {
      this.status = status;
      this.responseText = JSON.stringify(body);
      for (const cb of this._listeners['load'] ?? []) cb();
    }
  }

  it('POSTs a multipart body via XHR with kind and file fields, without setting Content-Type manually', async () => {
    const doc = {
      id: 'd1',
      analysis_id: 'a1',
      kind: 'memo',
      filename: 'plan.txt',
      sha256: 'aa',
      storage_uri: 'file:///tmp/x',
      byte_size: 3,
      ingested_at: '2026-06-26T10:00:00Z',
    };

    let mockXhr!: MockXHR;
    const origXHR = globalThis.XMLHttpRequest;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).XMLHttpRequest = class extends MockXHR {
      constructor() {
        super();
        mockXhr = this;
      }
    };

    try {
      const file = new File(['hi!'], 'plan.txt', { type: 'text/plain' });
      const result = new MemoryService({ getAppCheckToken: async () => 'test-token' } as any).uploadDocument('a1', 'memo', file);

      // 4. Return value is { promise, cancel }, not a bare Promise
      expect(result).toHaveProperty('promise');
      expect(result).toHaveProperty('cancel');
      expect(result.promise).toBeInstanceOf(Promise);
      expect(typeof result.cancel).toBe('function');

      // Wait for the async IIFE to fetch the token and call xhr.open
      await Promise.resolve();
      await Promise.resolve(); // two microtasks just in case

      // 1. xhr.open called correctly
      expect(mockXhr.openArgs).toEqual(['POST', '/api/v1/memory/analyses/a1/documents']);

      // 2. xhr.send called with FormData containing kind and file
      expect(mockXhr.sendArgs).toHaveLength(1);
      const form = mockXhr.sendArgs[0] as FormData;
      expect(form).toBeInstanceOf(FormData);
      expect(form.get('kind')).toBe('memo');
      expect((form.get('file') as File).name).toBe('plan.txt');

      // 3. Critical invariant: Content-Type must NOT be set manually
      const ctHeaders = mockXhr.headerCalls.filter(
        ([name]) => name.toLowerCase() === 'content-type',
      );
      expect(ctHeaders).toHaveLength(0);

      // 5. When XHR fires 'load' with status 201, promise resolves with the document
      mockXhr._fireLoad(201, doc);
      const out = await result.promise;
      expect(out).toEqual(doc);
    } finally {
      globalThis.XMLHttpRequest = origXHR;
    }
  });
});

