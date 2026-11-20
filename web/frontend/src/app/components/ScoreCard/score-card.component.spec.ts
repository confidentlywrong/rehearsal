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
import { provideZonelessChangeDetection, ɵresolveComponentResources as resolveComponentResources } from '@angular/core';
import { getTestBed, TestBed } from '@angular/core/testing';
import {
  BrowserTestingModule,
  platformBrowserTesting,
} from '@angular/platform-browser/testing';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { ScoreCardComponent } from './score-card.component';

describe('ScoreCardComponent (SSOT & Outputs)', () => {
  beforeAll(async () => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
    await resolveComponentResources(() => Promise.resolve(''));
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('defaults to honest empty states instead of hardcoded mock scores or titles', async () => {
    await TestBed.configureTestingModule({
      imports: [ScoreCardComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();

    const fixture = TestBed.createComponent(ScoreCardComponent);
    const card = fixture.componentInstance;
    fixture.detectChanges();

    expect(card.score).toBeNull();
    expect(card.titleText).toBeUndefined();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('—');
    expect(text).not.toContain('75');
    expect(text).not.toContain('EL CLASSICO');
    expect(text).not.toContain('14:38:11 AM');
    expect(text).not.toContain('25/03/26');
    expect(text).not.toContain('#1234');
  });

  it('renders bound @Input() values and emits showScoreCard / runSimulation / historyClick outputs', async () => {
    await TestBed.configureTestingModule({
      imports: [ScoreCardComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();

    const fixture = TestBed.createComponent(ScoreCardComponent);
    const comp = fixture.componentInstance;
    comp.score = 88;
    comp.titleText = 'Board Alignment Plan';
    comp.cardType = 'PLAN';
    comp.versionText = 'v2';
    comp.dateText = '2026-11-18';
    comp.timestampText = '12:00:00';
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('88');
    expect(text).toContain('Board Alignment Plan');
    expect(text).toContain('v2');
    expect(text).not.toContain('EL CLASSICO');
    expect(text).not.toContain('#1234');
    expect(text).not.toContain('14:38:11 AM');

    const showSpy = vi.fn();
    const runSpy = vi.fn();
    const historySpy = vi.fn();
    comp.showScoreCard.subscribe(showSpy);
    comp.runSimulation.subscribe(runSpy);
    comp.historyClick.subscribe(historySpy);

    comp.onShowScoreCard();
    comp.onRunSimulation({ stopPropagation: vi.fn() });
    comp.onHistoryClick();

    expect(showSpy).toHaveBeenCalledTimes(1);
    expect(runSpy).toHaveBeenCalledTimes(1);
    expect(historySpy).toHaveBeenCalledTimes(1);
  });
});
