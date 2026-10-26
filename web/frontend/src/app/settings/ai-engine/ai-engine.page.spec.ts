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

import '@angular/compiler';
import { ChangeDetectorRef } from '@angular/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AiEngineService,
  AiEngineStatus,
} from './ai-engine.service';
import { AiEnginePageComponent } from './ai-engine.page';

type FetchSpy = ReturnType<typeof vi.fn>;

function installFetch(spy: FetchSpy): void {
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

function buildStatus(overrides: Partial<AiEngineStatus> = {}): AiEngineStatus {
  return {
    provider: 'cloud_gemini',
    endpoint: 'http://localhost:11434',
    text_model: 'gemma2:9b',
    embed_model: 'embeddinggemma:latest',
    cloud_project_configured: true,
    connected: true,
    protocol: 'ollama',
    latency_ms: 4,
    binary_installed: true,
    binary_path: '/Applications/Ollama.app/Contents/Resources/ollama',
    text_model_ready: true,
    embed_model_ready: true,
    installed_models: ['gemma2:9b', 'embeddinggemma:latest'],
    hardware: {
      platform: 'darwin',
      arch: 'arm64',
      chip: 'Apple M3 Max',
      total_ram_gb: 36,
      recommended_tier: 'expert',
      recommended_model: 'gemma2:27b',
    },
    model_tiers: [
      {
        id: 'compact',
        model: 'gemma2:2b',
        label: 'Gemma 2B · Compact',
        subtitle: 'Fastest response · 8 GB+ RAM',
        size_gb: 1.6,
        min_ram_gb: 8,
        fits_ram: true,
        installed: false,
      },
      {
        id: 'balanced',
        model: 'gemma2:9b',
        label: 'Gemma 9B · Balanced',
        subtitle: 'Recommended for leadership rehearsal · 16 GB+ RAM',
        size_gb: 5.4,
        min_ram_gb: 16,
        fits_ram: true,
        installed: true,
      },
      {
        id: 'expert',
        model: 'gemma2:27b',
        label: 'Gemma 27B · Expert',
        subtitle: 'Maximum analytical depth · 32 GB+ RAM',
        size_gb: 16.0,
        min_ram_gb: 32,
        fits_ram: true,
        installed: false,
      },
    ],
    embed_engine: {
      model: 'embeddinggemma:latest',
      configured_model: 'embeddinggemma:latest',
      label: 'EmbeddingGemma (Local Vector Engine)',
      subtitle: 'Local Google vector embeddings for Organizational Memory RAG',
      size_gb: 0.6,
      native_dim: 768,
      pgvector_dim: 3072,
      installed: true,
    },
    pull_progress: {
      active: false,
      percent: 0,
      completed_bytes: 0,
      total_bytes: 0,
      done: false,
    },
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AiEngineService & AiEnginePageComponent', () => {
  let svc: AiEngineService;
  let cdr: ChangeDetectorRef;

  beforeEach(() => {
    svc = new AiEngineService();
    cdr = { markForCheck: vi.fn() } as unknown as ChangeDetectorRef;
  });

  it('loads installed and missing model states on init', async () => {
    const mockStatus = buildStatus();
    const spy = vi.fn().mockResolvedValue(jsonResponse(mockStatus));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    expect(page.loading).toBe(false);
    expect(page.provider).toBe('cloud_gemini');
    expect(page.textModel).toBe('gemma2:9b');
    expect(page.embedModel).toBe('embeddinggemma:latest');
    expect(page.status?.model_tiers[1].installed).toBe(true); // gemma2:9b installed
    expect(page.status?.model_tiers[0].installed).toBe(false); // gemma2:2b missing
    expect(page.status?.embed_engine.installed).toBe(true); // embeddinggemma:latest installed
    page.ngOnDestroy();
  });

  it('handles missing Ollama binary and missing EmbeddingGemma state cleanly', async () => {
    const missingStatus = buildStatus({
      connected: false,
      binary_installed: false,
      text_model_ready: false,
      embed_model_ready: false,
      installed_models: [],
      embed_engine: {
        model: 'embeddinggemma:latest',
        configured_model: 'embeddinggemma:latest',
        label: 'EmbeddingGemma',
        subtitle: 'Local Google vector embeddings',
        size_gb: 0.6,
        native_dim: 768,
        pgvector_dim: 3072,
        installed: false,
      },
    });
    installFetch(vi.fn().mockResolvedValue(jsonResponse(missingStatus)));

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    expect(page.status?.binary_installed).toBe(false);
    expect(page.status?.connected).toBe(false);
    expect(page.status?.embed_engine.installed).toBe(false);
    page.ngOnDestroy();
  });

  it('switches to local_gemma and saves both Gemma text model and EmbeddingGemma', async () => {
    const initialStatus = buildStatus();
    const savedStatus = buildStatus({ provider: 'local_gemma' });
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(initialStatus))
      .mockResolvedValueOnce(jsonResponse(savedStatus));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    page.selectProvider('local_gemma');
    await page.onSave();

    expect(spy).toHaveBeenCalledTimes(2);
    expect(page.provider).toBe('local_gemma');
    expect(page.toastMessage).toContain('Running locally on Google gemma2:9b + embeddinggemma:latest');
    page.ngOnDestroy();
  });

  it('triggers model pull for uninstalled Gemma or EmbeddingGemma models', async () => {
    const initialStatus = buildStatus();
    const pullResponse = {
      pull_progress: {
        active: false,
        model: 'gemma2:2b',
        status: 'success',
        percent: 100,
        completed_bytes: 1600000000,
        total_bytes: 1600000000,
        done: true,
      },
      status: buildStatus(),
    };
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(initialStatus))
      .mockResolvedValueOnce(jsonResponse(pullResponse));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();
    await page.onPullModel('gemma2:2b');

    expect(page.toastMessage).toContain('Model gemma2:2b is installed and ready.');
    page.ngOnDestroy();
  });

  it('detects custom gemma3 tags and surfaces background pull errors when polling finishes', async () => {
    const activePullStatus = buildStatus({
      installed_models: ['gemma3:latest', 'embeddinggemma:latest'],
      pull_progress: {
        active: true,
        model: 'gemma3:12b',
        status: 'downloading',
        percent: 42,
        completed_bytes: 420,
        total_bytes: 1000,
        done: false,
      },
    });
    const activePullCustomStatus = buildStatus({
      endpoint: 'http://127.0.0.1:11435',
      installed_models: ['gemma3:latest', 'embeddinggemma:latest'],
      pull_progress: {
        active: true,
        model: 'gemma3:12b',
        status: 'downloading',
        percent: 65,
        completed_bytes: 650,
        total_bytes: 1000,
        done: false,
      },
    });
    const failedPullCustomStatus = buildStatus({
      endpoint: 'http://127.0.0.1:11435',
      installed_models: ['gemma3:latest', 'embeddinggemma:latest'],
      pull_progress: {
        active: false,
        model: 'gemma3:12b',
        status: 'error',
        percent: 65,
        completed_bytes: 650,
        total_bytes: 1000,
        done: false,
        error: 'Stream interrupted while pulling gemma3:12b',
      },
    });
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(activePullStatus))
      .mockResolvedValueOnce(jsonResponse(activePullCustomStatus))
      .mockResolvedValueOnce(jsonResponse(failedPullCustomStatus));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    page.textModel = 'gemma3:4b';
    expect(page.isCurrentTextModelInstalled()).toBe(true);
    page.textModel = 'gemma3:12b';
    expect(page.isCurrentTextModelInstalled()).toBe(false);
    page.textModel = 'embeddinggemma';
    expect(page.isCurrentTextModelInstalled()).toBe(false);

    // Simulate two consecutive poll ticks on an unsaved custom endpoint:
    // both ticks must pass ?endpoint=http%3A%2F%2F127.0.0.1%3A11435 (no alternating).
    page.endpoint = 'http://127.0.0.1:11435';
    page.toastMessage = 'Started downloading gemma3:12b in the background…';
    await page.loadStatus(false);
    expect(spy).toHaveBeenNthCalledWith(
      2,
      '/api/v1/settings/ai?endpoint=http%3A%2F%2F127.0.0.1%3A11435',
      expect.any(Object),
    );
    await page.loadStatus(false);
    expect(spy).toHaveBeenNthCalledWith(
      3,
      '/api/v1/settings/ai?endpoint=http%3A%2F%2F127.0.0.1%3A11435',
      expect.any(Object),
    );
    expect(page.errorMessage).toContain('Stream interrupted while pulling gemma3:12b');
    expect(page.toastMessage).toBe('');
    page.ngOnDestroy();
  });
});



