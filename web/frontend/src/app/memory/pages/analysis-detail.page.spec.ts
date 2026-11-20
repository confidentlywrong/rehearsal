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
import { provideZonelessChangeDetection } from '@angular/core';
import { getTestBed, TestBed } from '@angular/core/testing';
import {
  BrowserTestingModule,
  platformBrowserTesting,
} from '@angular/platform-browser/testing';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { MemoryService } from '../memory.service';
import { AnalysisDetailPageComponent } from './analysis-detail.page';

describe('AnalysisDetailPageComponent dialog, layout & lifecycle regression suite', () => {
  beforeAll(() => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  it('uses ObsidianConfirmDialog instead of window.confirm, omits horizontal tabs, and navigates on confirm', async () => {
    const analysis = {
      id: 'a-1',
      operator_id: 'op-1',
      org_name: 'Acme',
      title: 'Q3 Plan',
      created_at: '2026-06-26T10:00:00Z',
    };
    const deleteAnalysis = vi.fn().mockResolvedValue(undefined);
    const getAnalysis = vi.fn().mockResolvedValue(analysis);

    TestBed.configureTestingModule({
      imports: [AnalysisDetailPageComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: of(convertToParamMap({ analysisId: 'a-1' })),
            snapshot: { paramMap: convertToParamMap({ analysisId: 'a-1' }) },
          },
        },
        {
          provide: MemoryService,
          useValue: {
            getAnalysis,
            deleteAnalysis,
            getMemoryConfig: vi.fn().mockResolvedValue({
              text_readable_kinds: ['memo'],
              max_upload_bytes: 1024,
            }),
            listDocuments: vi.fn().mockResolvedValue([]),
          },
        },
      ],
    });

    const router = TestBed.inject(Router);
    const navSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    const fixture = TestBed.createComponent(AnalysisDetailPageComponent);
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.detail__tabs')).toBeNull();

    const winConfirm = vi.fn();
    Object.defineProperty(window, 'confirm', {
      configurable: true,
      writable: true,
      value: winConfirm,
    });
    const dialog = (fixture.componentInstance as unknown as { confirmDialog: { prompt: ReturnType<typeof vi.fn> } }).confirmDialog;
    vi.spyOn(dialog, 'prompt').mockResolvedValue(true);

    await fixture.componentInstance.onDeleteAnalysis(analysis);
    expect(winConfirm).not.toHaveBeenCalled();
    expect(deleteAnalysis).toHaveBeenCalledWith('a-1');
    expect(navSpy).toHaveBeenCalledWith(['/settings/memory']);
    fixture.destroy();
  });

  it('guards post-await refresh and delete navigation after ngOnDestroy', async () => {
    let resolveGet!: (v: unknown) => void;
    let resolveDel!: () => void;
    const getAnalysis = vi.fn(() => new Promise((r) => { resolveGet = r; }));
    const deleteAnalysis = vi.fn(() => new Promise<void>((r) => { resolveDel = r; }));

    TestBed.configureTestingModule({
      imports: [AnalysisDetailPageComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: of(convertToParamMap({ analysisId: 'a-1' })),
            snapshot: { paramMap: convertToParamMap({ analysisId: 'a-1' }) },
          },
        },
        {
          provide: MemoryService,
          useValue: {
            getAnalysis,
            deleteAnalysis,
            getMemoryConfig: vi.fn().mockResolvedValue({
              text_readable_kinds: ['memo'],
              max_upload_bytes: 1024,
            }),
            listDocuments: vi.fn().mockResolvedValue([]),
          },
        },
      ],
    });

    const router = TestBed.inject(Router);
    const navSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    const fixture = TestBed.createComponent(AnalysisDetailPageComponent);
    const comp = fixture.componentInstance;
    fixture.detectChanges();

    const dialog = (comp as unknown as { confirmDialog: { prompt: ReturnType<typeof vi.fn> } }).confirmDialog;
    vi.spyOn(dialog, 'prompt').mockResolvedValue(true);

    const delPromise = comp.onDeleteAnalysis({
      id: 'a-1',
      operator_id: 'op-1',
      org_name: 'Acme',
      title: 'Q3 Plan',
      created_at: '2026-06-26T10:00:00Z',
    });
    await Promise.resolve();
    comp.ngOnDestroy();

    resolveGet({
      id: 'a-1',
      operator_id: 'op-1',
      org_name: 'Acme',
      title: 'Q3 Plan',
      created_at: '2026-06-26T10:00:00Z',
    });
    resolveDel();
    await delPromise;
    expect(comp.analysis()).toBeNull();
    expect(navSpy).not.toHaveBeenCalled();
  });
});
