// Copyright 2026 Christian Haeussinger
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
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: URL-encode memory path parameters, unwrap upload document wrapper, and guard malformed 2xx XHR JSON responses.
 *
 * MemoryService — typed HTTP client for /api/v1/memory/*.
 *
 * Every method targets the gateway routes from cmd/gateway/memory.go.
 * The AuthService's APP_INITIALIZER patches window.fetch so we don't
 * need to handle Bearer tokens here; we just keep credentials:
 * 'same-origin' for parity with IdentityService.
 *
 * Errors:
 *   - All methods throw `MemoryApiError` with the gateway's
 *     {error, detail} payload preserved when available. Callers
 *     catch and surface the .code (e.g. 'extractor_unconfigured').
 */

import { Injectable } from '@angular/core';

import { AuthService } from '../auth/auth.service';

import {
  Analysis,
  MemoryDocument,
} from './memory.types';

export class MemoryApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly detail?: string,
  ) {
    super(`memory api ${status}: ${code}${detail ? ` (${detail})` : ''}`);
    this.name = 'MemoryApiError';
  }
}

const BASE = '/api/v1/memory';

async function parseOrThrow<T>(resp: Response): Promise<T> {
  if (resp.ok) {
    if (resp.status === 204) {
      return undefined as unknown as T;
    }
    return (await resp.json()) as T;
  }
  let code = 'http_error';
  let detail: string | undefined;
  try {
    const body = await resp.json();
    if (typeof body?.error === 'string') {
      code = body.error;
    }
    if (typeof body?.detail === 'string') {
      detail = body.detail;
    }
  } catch {
    // Body was not JSON; fall through with default code.
  }
  throw new MemoryApiError(resp.status, code, detail);
}

@Injectable({ providedIn: 'root' })
export class MemoryService {
  constructor(private auth: AuthService) {}
  // --- config ------------------------------------------------------
  
  async getMemoryConfig(): Promise<{ text_readable_kinds: string[], max_upload_bytes: number }> {
    const resp = await fetch(`${BASE}/config`, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    return parseOrThrow<{ text_readable_kinds: string[], max_upload_bytes: number }>(resp);
  }

  // --- analyses ----------------------------------------------------

  async listAnalyses(): Promise<Analysis[]> {
    const resp = await fetch(`${BASE}/analyses`, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const body = await parseOrThrow<{ analyses: Analysis[] }>(resp);
    return body?.analyses ?? [];
  }

  async createAnalysis(input: {
    org_name?: string;
    title: string;
    description?: string;
  }): Promise<Analysis> {
    const resp = await fetch(`${BASE}/analyses`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(input),
    });
    return parseOrThrow<Analysis>(resp);
  }

  async getAnalysis(analysisId: string): Promise<Analysis> {
    const resp = await fetch(`${BASE}/analyses/${encodeURIComponent(analysisId)}`, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    return parseOrThrow<Analysis>(resp);
  }

  async deleteAnalysis(analysisId: string): Promise<void> {
    const resp = await fetch(`${BASE}/analyses/${encodeURIComponent(analysisId)}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    await parseOrThrow<void>(resp);
  }


  // --- documents ---------------------------------------------------

  async listDocuments(analysisId: string): Promise<MemoryDocument[]> {
    const resp = await fetch(`${BASE}/analyses/${encodeURIComponent(analysisId)}/documents`, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const body = await parseOrThrow<{ documents: MemoryDocument[] }>(resp);
    return body?.documents ?? [];
  }

  async deleteDocument(analysisId: string, documentId: string): Promise<void> {
    const resp = await fetch(
      `${BASE}/analyses/${encodeURIComponent(analysisId)}/documents/${encodeURIComponent(documentId)}`,
      {
        method: 'DELETE',
        credentials: 'same-origin',
      },
    );
    await parseOrThrow<void>(resp);
  }

  /**
   * Uploads one file via multipart/form-data. Returns the persisted
   * document. The browser sets the Content-Type boundary automatically
   * when `body` is a FormData — never set it manually.
   */
  uploadDocument(
    analysisId: string,
    kind: string,
    file: File,
    onProgress?: (pct: number) => void,
  ): { promise: Promise<MemoryDocument>; cancel: () => void } {
    const form = new FormData();
    form.append('kind', kind);
    form.append('file', file, file.name);

    const xhr = new XMLHttpRequest();
    let aborted = false;
    let opened = false;
    let settled = false;
    let rejectPromise!: (reason?: unknown) => void;

    const settleResolve = (resolve: (value: MemoryDocument) => void, doc: MemoryDocument): void => {
      if (settled) return;
      settled = true;
      resolve(doc);
    };
    const settleReject = (err: unknown): void => {
      if (settled) return;
      settled = true;
      rejectPromise(err);
    };

    const promise = new Promise<MemoryDocument>((resolve, reject) => {
      rejectPromise = reject;
      (async () => {
        try {
          const token = await this.auth.getAppCheckToken();
          if (aborted || settled) return;

          if (onProgress) {
            xhr.upload.addEventListener('progress', (ev) => {
              if (ev.lengthComputable) {
                onProgress(Math.round((ev.loaded * 100) / ev.total));
              }
            });
          }
          xhr.addEventListener('load', () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              try {
                const parsed = JSON.parse(xhr.responseText);
                const baseDoc = (
                  parsed &&
                  typeof parsed === 'object' &&
                  'document' in parsed &&
                  parsed.document
                    ? parsed.document
                    : parsed
                ) as MemoryDocument;
                const warning =
                  parsed &&
                  typeof parsed === 'object' &&
                  typeof parsed.warning === 'string' &&
                  parsed.warning.trim()
                    ? parsed.warning.trim()
                    : undefined;
                const doc: MemoryDocument = warning ? { ...baseDoc, warning } : baseDoc;
                settleResolve(resolve, doc);
              } catch {
                settleReject(new MemoryApiError(xhr.status, 'invalid_json', 'Malformed response JSON'));
              }
            } else {
              let code = 'http_error';
              let detail;
              try {
                const body = JSON.parse(xhr.responseText);
                if (body.error) code = body.error;
                if (body.detail) detail = body.detail;
              } catch { /* non-JSON error body — fall through with default code */ }
              settleReject(new MemoryApiError(xhr.status, code, detail));
            }
          });
          xhr.addEventListener('error', () => {
            settleReject(new MemoryApiError(0, 'network_error'));
          });
          xhr.addEventListener('abort', () => {
            aborted = true;
            settleReject(new MemoryApiError(0, 'aborted', 'Upload cancelled.'));
          });
          xhr.open('POST', `${BASE}/analyses/${encodeURIComponent(analysisId)}/documents`);
          opened = true;
          xhr.withCredentials = true;
          if (token) {
            xhr.setRequestHeader('X-Firebase-AppCheck', token);
          }
          xhr.send(form);
        } catch (err) {
          settleReject(err);
        }
      })();
    });

    return {
      promise,
      cancel: () => {
        if (aborted || settled) return;
        aborted = true;
        if (opened) {
          xhr.abort();
        } else {
          settleReject(new MemoryApiError(0, 'aborted', 'Upload cancelled.'));
        }
      },
    };
  }


}
