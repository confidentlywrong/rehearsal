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
import { Component, provideZonelessChangeDetection } from '@angular/core';
import { getTestBed, TestBed } from '@angular/core/testing';
import {
  BrowserTestingModule,
  platformBrowserTesting,
} from '@angular/platform-browser/testing';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { ObsidianStatusTone, StatusLabelComponent } from './status-label.component';

@Component({
  standalone: true,
  imports: [StatusLabelComponent],
  template: `<app-status-label [status]="tone">Projected Status</app-status-label>`,
})
class ProjectedHostComponent {
  tone: ObsidianStatusTone = 'warning';
}

describe('StatusLabelComponent', () => {
  beforeAll(() => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
  });

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [StatusLabelComponent, ProjectedHostComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('maps every canonical tone and alias to the exact Obsidian hex dot', () => {
    const cases: Array<[ObsidianStatusTone, string]> = [
      ['ok', '#34C759'],
      ['green', '#34C759'],
      ['warning', '#FF9500'],
      ['amber', '#FF9500'],
      ['error', '#FF3B30'],
      ['red', '#FF3B30'],
      ['neutral', '#1D1D1F'],
      ['muted', '#C7C7CC'],
    ];

    for (const [tone, expectedHex] of cases) {
      const fixture = TestBed.createComponent(StatusLabelComponent);
      fixture.componentRef.setInput('status', tone);
      fixture.componentRef.setInput('label', `Label ${tone}`);
      fixture.detectChanges();

      const el = fixture.nativeElement as HTMLElement;
      const gutter = el.querySelector('.status-label-gutter');
      const dot = el.querySelector('.status-label__dot') as HTMLElement | null;
      const text = el.querySelector('.status-label__text');

      expect(gutter).not.toBeNull();
      expect(gutter?.classList.contains('status-label-gutter--inline')).toBe(false);
      expect(dot).not.toBeNull();
      expect(dot?.style.backgroundColor.toUpperCase()).toBe(expectedHex);
      expect(dot?.style.opacity).toBe('1');
      expect(text?.textContent?.trim()).toBe(`Label ${tone}`);
      fixture.destroy();
    }
  });

  it('hides the dot via opacity 0 while preserving gutter layout when showDot=false', () => {
    const fixture = TestBed.createComponent(StatusLabelComponent);
    fixture.componentRef.setInput('status', 'ok');
    fixture.componentRef.setInput('label', 'Ready');
    fixture.componentRef.setInput('showDot', false);
    fixture.componentRef.setInput('inlineColumn', true);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const gutter = el.querySelector('.status-label-gutter');
    const dot = el.querySelector('.status-label__dot') as HTMLElement | null;

    expect(gutter?.classList.contains('status-label-gutter--inline')).toBe(true);
    expect(dot?.style.opacity).toBe('0');
    expect(dot?.style.backgroundColor).toBe('transparent');
    fixture.destroy();
  });

  it('renders projected content when label input is omitted', () => {
    const fixture = TestBed.createComponent(ProjectedHostComponent);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const text = el.querySelector('.status-label__text');
    const dot = el.querySelector('.status-label__dot') as HTMLElement | null;

    expect(text?.textContent?.trim()).toBe('Projected Status');
    expect(dot?.style.backgroundColor.toUpperCase()).toBe('#FF9500');
    fixture.destroy();
  });
});
