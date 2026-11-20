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
import { ChangeDetectorRef, provideZonelessChangeDetection } from '@angular/core';
import { getTestBed, TestBed } from '@angular/core/testing';
import {
  BrowserTestingModule,
  platformBrowserTesting,
} from '@angular/platform-browser/testing';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
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
    expect(page.savedSuccess).toBe(true);
    expect(page.saveNotice).toContain('Running locally on Google gemma2:9b + embeddinggemma:latest');
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

    expect(page.pullNotice).toContain('Model gemma2:2b is installed and ready.');
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
    page.pullNotice = 'Started downloading gemma3:12b in the background…';
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
    expect(page.pullNotice).toBe('');
    page.ngOnDestroy();
  });

  it('starts unconfigured with empty fields and surfaces explicit error without fallback defaults on initial failure', async () => {
    installFetch(vi.fn().mockResolvedValue(jsonResponse({ error: 'backend offline' }, 503)));

    const page = new AiEnginePageComponent(svc, cdr);
    expect(page.endpoint).toBe('');
    expect(page.textModel).toBe('');
    expect(page.embedModel).toBe('');

    await page.ngOnInit();
    expect(page.status).toBeNull();
    expect(page.endpoint).toBe('');
    expect(page.textModel).toBe('');
    expect(page.embedModel).toBe('');
    expect(page.errorMessage).toContain('backend offline');
    page.ngOnDestroy();
  });

  it('does not populate hardcoded fallbacks when backend returns empty endpoint/model strings', async () => {
    installFetch(
      vi.fn().mockResolvedValue(
        jsonResponse(buildStatus({ endpoint: '', text_model: '', embed_model: '' })),
      ),
    );
    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    expect(page.endpoint).toBe('');
    expect(page.textModel).toBe('');
    expect(page.embedModel).toBe('');
    page.ngOnDestroy();
  });

  it('cancels savedSuccessTimer and pollTimer in ngOnDestroy', async () => {
    vi.useFakeTimers();
    try {
      const activeStatus = buildStatus({
        pull_progress: {
          active: true,
          model: 'gemma2:9b',
          status: 'downloading',
          percent: 10,
          completed_bytes: 10,
          total_bytes: 100,
          done: false,
        },
      });
      const spy = vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(activeStatus))
        .mockResolvedValueOnce(jsonResponse(buildStatus()));
      installFetch(spy);

      const page = new AiEnginePageComponent(svc, cdr);
      await page.ngOnInit();
      await page.onSave();
      expect(page.savedSuccess).toBe(true);

      (cdr.markForCheck as ReturnType<typeof vi.fn>).mockClear();
      page.ngOnDestroy();
      await vi.advanceTimersByTimeAsync(5000);

      expect(page.savedSuccess).toBe(true);
      expect(spy).toHaveBeenCalledTimes(2);
      expect(cdr.markForCheck).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('guards post-await mutations on onSave, onTestConnection, onStartDaemon, and onPullModel after ngOnDestroy', async () => {
    let resolveTest!: (r: Response) => void;
    let resolveStart!: (r: Response) => void;
    let resolvePull!: (r: Response) => void;
    let resolveSave!: (r: Response) => void;

    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(buildStatus()))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolveTest = r; }))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolveStart = r; }))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolvePull = r; }))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolveSave = r; }));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    const pTest = page.onTestConnection();
    const pStart = page.onStartDaemon();
    const pPull = page.onPullModel('gemma2:2b');
    const pSave = page.onSave();

    (cdr.markForCheck as ReturnType<typeof vi.fn>).mockClear();
    page.ngOnDestroy();

    resolveTest(jsonResponse({ reachable: true, endpoint: 'http://x', installed_models: null, error: '', status: buildStatus() }));
    resolveStart(jsonResponse(buildStatus()));
    resolvePull(jsonResponse({ pull_progress: { active: false, done: true, percent: 100, completed_bytes: 1, total_bytes: 1 }, status: buildStatus() }));
    resolveSave(jsonResponse(buildStatus({ provider: 'local_gemma' })));
    await Promise.all([pTest, pStart, pPull, pSave]);

    expect(page.connectionNotice).toBe('');
    expect(page.pullNotice).toBe('');
    expect(page.saveNotice).toBe('');
    expect(page.savedSuccess).toBe(false);
    expect(cdr.markForCheck).not.toHaveBeenCalled();
  });

  it('supports gemma2:9b <-> gemma2:latest alias matching and null installed_models on testConnection', async () => {
    const st = buildStatus({
      installed_models: ['gemma2:latest'],
      model_tiers: [],
    });
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(st))
      .mockResolvedValueOnce(
        jsonResponse({
          reachable: true,
          endpoint: 'http://localhost:11434',
          installed_models: null,
          error: '',
          status: st,
        }),
      );
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();
    page.textModel = 'gemma2:9b';
    expect(page.isCurrentTextModelInstalled()).toBe(true);

    await page.onTestConnection();
    expect(page.connectionNotice).toContain('0 model(s) available');
    page.ngOnDestroy();
  });

  it('ignores re-entrant onSave() calls while a save is already in flight', async () => {
    let resolveSave!: (r: Response) => void;
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(buildStatus()))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolveSave = r; }));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    const firstSave = page.onSave();
    expect(page.saving).toBe(true);
    await page.onSave();

    expect(spy).toHaveBeenCalledTimes(2);
    resolveSave(jsonResponse(buildStatus({ provider: 'local_gemma' })));
    await firstSave;
    expect(page.saving).toBe(false);
    page.ngOnDestroy();
  });

  it('ignores re-entrant onTestConnection() and onStartDaemon() calls while in flight', async () => {
    let resolveTest!: (r: Response) => void;
    let resolveStart!: (r: Response) => void;
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(buildStatus()))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolveTest = r; }))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolveStart = r; }));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    const firstTest = page.onTestConnection();
    expect(page.testingConnection).toBe(true);
    await page.onTestConnection();
    expect(spy).toHaveBeenCalledTimes(2);

    resolveTest(jsonResponse({
      reachable: true,
      endpoint: 'http://localhost:11434',
      installed_models: ['gemma2:9b'],
      error: '',
      status: buildStatus(),
    }));
    await firstTest;
    expect(page.testingConnection).toBe(false);

    const firstStart = page.onStartDaemon();
    expect(page.startingDaemon).toBe(true);
    await page.onStartDaemon();
    expect(spy).toHaveBeenCalledTimes(3);

    resolveStart(jsonResponse(buildStatus()));
    await firstStart;
    expect(page.startingDaemon).toBe(false);
    page.ngOnDestroy();
  });

  it('ignores re-entrant onPullModel() calls while pullingModel or status.pull_progress.active is active', async () => {
    let resolvePull!: (r: Response) => void;
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(buildStatus()))
      .mockImplementationOnce(() => new Promise<Response>((r) => { resolvePull = r; }));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    const firstPull = page.onPullModel('gemma2:2b');
    expect(page.pullingModel).toBe('gemma2:2b');
    // Pulling same or different tier while pullingModel is non-null must return immediately
    await page.onPullModel('gemma2:27b');
    await page.onPullModel('gemma2:2b');
    expect(spy).toHaveBeenCalledTimes(2);

    resolvePull(jsonResponse({
      pull_progress: {
        active: false,
        done: true,
        percent: 100,
        completed_bytes: 100,
        total_bytes: 100,
      },
      status: buildStatus(),
    }));
    await firstPull;
    // Also guard when status.pull_progress.active is already true
    page.pullingModel = null;
    page.status = buildStatus({
      pull_progress: {
        active: true,
        model: 'gemma2:2b',
        percent: 42,
        completed_bytes: 42,
        total_bytes: 100,
        done: false,
      },
    });
    await page.onPullModel('gemma2:27b');
    expect(spy).toHaveBeenCalledTimes(2);
    expect(page.pullingModel).toBeNull();
    page.ngOnDestroy();
  });
});

describe('AiEnginePageComponent cross-method pullInFlight race & download button DOM regression', () => {
  beforeAll(() => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  it('keeps pullingModel and sibling download buttons disabled when onTestConnection() or loadStatus() resolves mid-pull', async () => {
    const baseStatus = buildStatus({
      text_model: 'gemma3:12b',
      embed_engine: {
        model: 'embeddinggemma:latest',
        configured_model: 'embeddinggemma:latest',
        label: 'EmbeddingGemma (Local Vector Engine)',
        subtitle: 'Local Google vector embeddings for Organizational Memory RAG',
        size_gb: 0.6,
        native_dim: 768,
        pgvector_dim: 3072,
        installed: false,
      },
    });

    let resolvePull!: (r: Response) => void;
    const fetchSpy = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/v1/settings/ai') {
        return Promise.resolve(jsonResponse(baseStatus));
      }
      if (url === '/api/v1/settings/ai/test') {
        return Promise.resolve(
          jsonResponse({
            reachable: true,
            endpoint: 'http://localhost:11434',
            installed_models: ['gemma2:9b'],
            error: '',
            status: baseStatus,
          }),
        );
      }
      if (url === '/api/v1/settings/ai/pull') {
        return new Promise<Response>((resolve) => {
          resolvePull = resolve;
        });
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    installFetch(fetchSpy);

    await TestBed.configureTestingModule({
      imports: [AiEnginePageComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();

    const fixture = TestBed.createComponent(AiEnginePageComponent);
    const page = fixture.componentInstance;
    await page.ngOnInit();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const getBtn = (testId: string): HTMLButtonElement => {
      const btn = fixture.nativeElement.querySelector(
        `[data-testid="${testId}"]`,
      ) as HTMLButtonElement | null;
      if (!btn) {
        throw new Error(`Missing button [data-testid="${testId}"]`);
      }
      return btn;
    };

    const pullCallCount = () =>
      fetchSpy.mock.calls.filter(([u]) => String(u) === '/api/v1/settings/ai/pull').length;

    // 1. Start pullModel('gemma2:2b') while promise remains pending
    const firstPull = page.onPullModel('gemma2:2b');
    fixture.detectChanges();

    expect(page.pullingModel).toBe('gemma2:2b');
    expect(pullCallCount()).toBe(1);
    expect(getBtn('pull-tier-compact').disabled).toBe(true);
    expect(getBtn('pull-tier-expert').disabled).toBe(true);
    expect(getBtn('pull-custom-model-btn').disabled).toBe(true);
    expect(getBtn('pull-bundle-btn').disabled).toBe(true);
    expect(getBtn('pull-embed-model-btn').disabled).toBe(true);

    // 2. Concurrent onTestConnection() and loadStatus() resolve with pull_progress.active === false
    await page.onTestConnection();
    await page.loadStatus(false);
    fixture.detectChanges();

    expect(page.pullingModel).toBe('gemma2:2b');
    expect(getBtn('pull-tier-compact').disabled).toBe(true);
    expect(getBtn('pull-tier-expert').disabled).toBe(true);
    expect(getBtn('pull-custom-model-btn').disabled).toBe(true);
    expect(getBtn('pull-bundle-btn').disabled).toBe(true);
    expect(getBtn('pull-embed-model-btn').disabled).toBe(true);

    // 3. Concurrent onPullModel('other-model') must be ignored
    await page.onPullModel('other-model');
    expect(pullCallCount()).toBe(1);

    // 4. First pull resolves with pull_progress.active === false -> pullingModel clears to null
    resolvePull(
      jsonResponse({
        pull_progress: {
          active: false,
          model: 'gemma2:2b',
          status: 'success',
          percent: 100,
          completed_bytes: 1600,
          total_bytes: 1600,
          done: true,
        },
        status: baseStatus,
      }),
    );
    await firstPull;
    fixture.detectChanges();

    expect(page.pullingModel).toBeNull();
    expect(getBtn('pull-tier-compact').disabled).toBe(false);
    expect(getBtn('pull-tier-expert').disabled).toBe(false);
    expect(getBtn('pull-custom-model-btn').disabled).toBe(false);
    expect(getBtn('pull-bundle-btn').disabled).toBe(false);
    expect(getBtn('pull-embed-model-btn').disabled).toBe(false);

    fixture.destroy();
  });

  it('renders read-only notice, disables all mutating controls, and blocks mutation calls when can_configure is false', async () => {
    const readOnlyStatus = buildStatus({
      can_configure: false,
      connected: false,
      binary_installed: true,
      text_model: 'gemma3:12b',
      embed_engine: {
        model: 'embeddinggemma:latest',
        configured_model: 'embeddinggemma:latest',
        label: 'EmbeddingGemma (Local Vector Engine)',
        subtitle: 'Local Google vector embeddings for Organizational Memory RAG',
        size_gb: 0.6,
        native_dim: 768,
        pgvector_dim: 3072,
        installed: false,
      },
    });
    const fetchSpy = vi.fn().mockResolvedValue(jsonResponse(readOnlyStatus));
    installFetch(fetchSpy);

    await TestBed.configureTestingModule({
      imports: [AiEnginePageComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();

    const fixture = TestBed.createComponent(AiEnginePageComponent);
    const page = fixture.componentInstance;
    await page.ngOnInit();
    fixture.detectChanges();
    await Promise.resolve();
    fixture.detectChanges();

    expect(page.canConfigure).toBe(false);
    const notice = fixture.nativeElement.querySelector('[data-testid="ai-readonly-notice"]') as HTMLElement | null;
    expect(notice).not.toBeNull();
    expect(notice?.textContent).toContain('Managed by workspace administrator');

    const disabledTestIds = [
      'provider-cloud-gemini',
      'provider-local-gemma',
      'start-daemon-btn',
      'endpoint-input',
      'test-connection-btn',
      'select-tier-balanced',
      'pull-tier-compact',
      'text-model-input',
      'pull-custom-model-btn',
      'pull-bundle-btn',
      'pull-embed-model-btn',
      'save-ai-settings-btn',
    ];
    for (const testId of disabledTestIds) {
      const el = fixture.nativeElement.querySelector(`[data-testid="${testId}"]`) as
        | HTMLButtonElement
        | HTMLInputElement
        | null;
      expect(el, `Expected element [data-testid="${testId}"] to exist`).not.toBeNull();
      expect(el?.disabled, `Expected [data-testid="${testId}"] to be disabled`).toBe(true);
    }

    const callsAfterInit = fetchSpy.mock.calls.length;
    expect(fetchSpy.mock.calls.every(([u]) => String(u) === '/api/v1/settings/ai')).toBe(true);
    page.selectProvider('local_gemma');
    page.selectTierModel(readOnlyStatus.model_tiers[1]);
    await page.onSave();
    await page.onTestConnection();
    await page.onStartDaemon();
    await page.onPullModel('gemma2:2b');

    expect(fetchSpy).toHaveBeenCalledTimes(callsAfterInit);
    expect(page.provider).toBe('cloud_gemini');
    expect(page.textModel).toBe('gemma3:12b');

    fixture.destroy();
  });

  it('maps 403 superadmin_required to a user-friendly administrator error message', async () => {
    const svc = new AiEngineService();
    const cdr = { markForCheck: vi.fn() } as unknown as ChangeDetectorRef;
    const spy = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(buildStatus()))
      .mockResolvedValueOnce(jsonResponse({ error: 'superadmin_required' }, 403))
      .mockResolvedValueOnce(jsonResponse({ error: 'superadmin_required' }, 403))
      .mockResolvedValueOnce(jsonResponse({ error: 'superadmin_required' }, 403))
      .mockResolvedValueOnce(jsonResponse({ error: 'superadmin_required' }, 403));
    installFetch(spy);

    const page = new AiEnginePageComponent(svc, cdr);
    await page.ngOnInit();

    await page.onSave();
    expect(page.errorMessage).toBe('Only workspace administrators can modify AI Engine settings.');
    expect(page.errorMessage).not.toContain('superadmin_required');

    await expect(svc.testConnection('http://localhost:11434')).rejects.toThrow(
      'Only workspace administrators can modify AI Engine settings.',
    );
    await expect(svc.startDaemon('http://localhost:11434')).rejects.toThrow(
      'Only workspace administrators can modify AI Engine settings.',
    );
    await expect(svc.pullModel('gemma2:2b', 'http://localhost:11434')).rejects.toThrow(
      'Only workspace administrators can modify AI Engine settings.',
    );

    page.ngOnDestroy();
  });
});



