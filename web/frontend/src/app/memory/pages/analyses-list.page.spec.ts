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
import { provideRouter } from '@angular/router';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { MemoryService } from '../memory.service';
import { AnalysesListPageComponent } from './analyses-list.page';

describe('AnalysesListPageComponent dialog, accessibility & lifecycle regression suite', () => {
  beforeAll(() => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  it('uses ObsidianConfirmDialog instead of window.confirm, omits title=, and renders semantic link cards', async () => {
    const analysis = {
      id: 'a-1',
      operator_id: 'op-1',
      org_name: 'Acme',
      title: 'Q3 Plan',
      created_at: '2026-06-26T10:00:00Z',
    };
    const deleteAnalysis = vi.fn().mockResolvedValue(undefined);
    const listAnalyses = vi.fn().mockResolvedValue([analysis]);

    TestBed.configureTestingModule({
      imports: [AnalysesListPageComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        {
          provide: MemoryService,
          useValue: { listAnalyses, createAnalysis: vi.fn(), deleteAnalysis },
        },
      ],
    });

    const fixture = TestBed.createComponent(AnalysesListPageComponent);
    await fixture.componentInstance.refresh();
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('[title]')).toBeNull();
    const cardLink = host.querySelector('a.analyses__card-link');
    expect(cardLink).not.toBeNull();

    const winConfirm = vi.fn();
    Object.defineProperty(window, 'confirm', {
      configurable: true,
      writable: true,
      value: winConfirm,
    });
    const dialog = (fixture.componentInstance as unknown as { confirmDialog: { prompt: ReturnType<typeof vi.fn> } }).confirmDialog;
    vi.spyOn(dialog, 'prompt').mockResolvedValue(true);

    await fixture.componentInstance.onDelete(analysis);
    expect(winConfirm).not.toHaveBeenCalled();
    expect(deleteAnalysis).toHaveBeenCalledWith('a-1');
    expect(fixture.componentInstance.analyses()).toEqual([]);
    fixture.destroy();
  });

  it('guards post-await refresh mutations after ngOnDestroy', async () => {
    let resolveList!: (v: unknown) => void;
    const listAnalyses = vi.fn(() => new Promise((r) => { resolveList = r; }));

    TestBed.configureTestingModule({
      imports: [AnalysesListPageComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        {
          provide: MemoryService,
          useValue: { listAnalyses, createAnalysis: vi.fn(), deleteAnalysis: vi.fn() },
        },
      ],
    });

    const fixture = TestBed.createComponent(AnalysesListPageComponent);
    const comp = fixture.componentInstance;
    comp.ngOnDestroy();

    resolveList([
      {
        id: 'a-late',
        operator_id: 'op-1',
        org_name: 'Acme',
        title: 'Late',
        created_at: '2026-06-26T10:00:00Z',
      },
    ]);
    await Promise.resolve();
    expect(comp.analyses()).toEqual([]);
  });
});
