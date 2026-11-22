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
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  AppleInfoPopoverComponent,
  computePopoverPosition,
} from './apple-info-popover.component';

describe('computePopoverPosition', () => {
  it('clamps coordinates inside all four viewport edges with 12px safety margin', () => {
    const coords = computePopoverPosition(
      { top: 4, left: 760, right: 780, bottom: 24, width: 20, height: 20 },
      { width: 340, height: 220 },
      { width: 800, height: 600 },
      'auto',
      12,
      8,
    );
    expect(coords.left).toBe(448); // 800 - 340 - 12
    expect(coords.top).toBe(32); // 24 + 8
    expect(coords.placement).toBe('bottom');
  });

  it('flips above the trigger when bottom space overflows', () => {
    const coords = computePopoverPosition(
      { top: 520, left: 2, right: 22, bottom: 540, width: 20, height: 20 },
      { width: 300, height: 200 },
      { width: 800, height: 600 },
      'auto',
      12,
      8,
    );
    expect(coords.left).toBe(12);
    expect(coords.top).toBe(312); // 520 - 200 - 8
    expect(coords.placement).toBe('top');
  });

  it('clamps maxHeight without overflowing on tiny viewports (vh = 90)', () => {
    const coords = computePopoverPosition(
      { top: 10, left: 10, right: 30, bottom: 30, width: 20, height: 20 },
      { width: 340, height: 220 },
      { width: 800, height: 90 },
      'auto',
      12,
      8,
    );
    expect(coords.top).toBeGreaterThanOrEqual(12);
    expect(coords.maxHeight).toBeGreaterThanOrEqual(0);
    expect(coords.top + coords.maxHeight).toBeLessThanOrEqual(90 - 12);
  });
});

@Component({
  standalone: true,
  imports: [AppleInfoPopoverComponent],
  template: `
    <app-apple-info-popover title="First" text="First details"></app-apple-info-popover>
    <app-apple-info-popover title="Second" text="Second details"></app-apple-info-popover>
  `,
})
class TwoPopoversHostComponent {}

describe('AppleInfoPopoverComponent', () => {
  let resizeCallback: (() => void) | null = null;
  let disconnectSpy = vi.fn();

  beforeAll(() => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
  });

  beforeEach(async () => {
    resizeCallback = null;
    disconnectSpy = vi.fn();
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: ResizeObserverCallback) {
          resizeCallback = () => cb([], this as unknown as ResizeObserver);
        }
        observe = vi.fn();
        disconnect = disconnectSpy;
      },
    );
    await TestBed.configureTestingModule({
      imports: [AppleInfoPopoverComponent, TwoPopoversHostComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    document.body.innerHTML = '';
  });

  it('portals to document.body, sets accessible trigger ARIA without title, and cleans up on destroy', () => {
    const fixture = TestBed.createComponent(AppleInfoPopoverComponent);
    fixture.componentRef.setInput('title', 'Decision Rule');
    fixture.componentRef.setInput('subtitle', 'Scale');
    fixture.componentRef.setInput('lead', 'All metrics use live data.');
    fixture.componentRef.setInput('items', [{ label: 'Threshold', value: '0.30' }]);
    fixture.detectChanges();

    const trigger = fixture.nativeElement.querySelector('button') as HTMLButtonElement;
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(trigger.hasAttribute('title')).toBe(false);

    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: 40,
      left: 100,
      right: 120,
      bottom: 60,
      width: 20,
      height: 20,
      x: 100,
      y: 40,
      toJSON: () => ({}),
    });

    trigger.click();
    fixture.detectChanges();

    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    const portalEl = document.body.querySelector('.apple-popover-panel') as HTMLElement | null;
    expect(portalEl).not.toBeNull();
    expect(portalEl?.getAttribute('role')).toBe('dialog');
    expect(portalEl?.parentElement).toBe(document.body);
    expect(portalEl?.textContent).toContain('Decision Rule');
    expect(portalEl?.textContent).toContain('Threshold');

    fixture.destroy();
    expect(document.body.querySelector('.apple-popover-panel')).toBeNull();
    expect(disconnectSpy).toHaveBeenCalled();
  });

  it('closes sibling popovers via single-open bus, restores focus on Escape, and dismisses on outside scroll/pointerdown', () => {
    const hostFixture = TestBed.createComponent(TwoPopoversHostComponent);
    hostFixture.detectChanges();

    const buttons = Array.from(
      hostFixture.nativeElement.querySelectorAll('button'),
    ) as HTMLButtonElement[];
    expect(buttons).toHaveLength(2);
    const [btn1, btn2] = buttons;

    btn1.click();
    hostFixture.detectChanges();
    expect(btn1.getAttribute('aria-expanded')).toBe('true');
    expect(btn2.getAttribute('aria-expanded')).toBe('false');

    btn2.click();
    hostFixture.detectChanges();
    expect(btn1.getAttribute('aria-expanded')).toBe('false');
    expect(btn2.getAttribute('aria-expanded')).toBe('true');

    // Inner scroll inside popover keeps it open; outer scroll closes it
    const popEl = document.body.querySelector('.apple-popover-panel') as HTMLElement;
    expect(popEl).not.toBeNull();
    popEl.dispatchEvent(new Event('scroll', { bubbles: false }));
    hostFixture.detectChanges();
    expect(btn2.getAttribute('aria-expanded')).toBe('true');

    // ResizeObserver triggers position recalculation without closing
    resizeCallback?.();
    hostFixture.detectChanges();
    expect(btn2.getAttribute('aria-expanded')).toBe('true');

    // Escape closes and restores focus to trigger
    const focusSpy = vi.spyOn(btn2, 'focus');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    hostFixture.detectChanges();
    expect(btn2.getAttribute('aria-expanded')).toBe('false');
    expect(focusSpy).toHaveBeenCalledTimes(1);

    // Outside pointerdown closes; outside scroll closes
    btn2.click();
    hostFixture.detectChanges();
    expect(btn2.getAttribute('aria-expanded')).toBe('true');
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    hostFixture.detectChanges();
    expect(btn2.getAttribute('aria-expanded')).toBe('false');

    btn2.click();
    hostFixture.detectChanges();
    expect(btn2.getAttribute('aria-expanded')).toBe('true');
    window.dispatchEvent(new Event('scroll'));
    hostFixture.detectChanges();
    expect(btn2.getAttribute('aria-expanded')).toBe('false');

    hostFixture.destroy();
  });

  it('clamps clampedWidthPx to vw - 24 on tiny viewports (vw = 180) and stacks .apple-popover-panel above HUD overlays', () => {
    const originalInnerWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', {
      configurable: true,
      writable: true,
      value: 180,
    });
    try {
      const fixture = TestBed.createComponent(AppleInfoPopoverComponent);
      fixture.componentRef.setInput('title', 'Tiny Viewport');
      fixture.componentRef.setInput('text', 'Clamping check');
      fixture.detectChanges();

      expect(fixture.componentInstance.clampedWidthPx).toBe(156);

      const trigger = fixture.nativeElement.querySelector('button') as HTMLButtonElement;
      trigger.click();
      fixture.detectChanges();

      const portalEl = document.body.querySelector('.apple-popover-panel') as HTMLElement | null;
      expect(portalEl).not.toBeNull();
      expect(portalEl?.style.width).toBe('156px');

      const zIndex = Number(getComputedStyle(portalEl!).zIndex);
      expect(zIndex).toBeGreaterThan(100001);
      expect(zIndex).toBe(100010);

      fixture.destroy();
    } finally {
      Object.defineProperty(window, 'innerWidth', {
        configurable: true,
        writable: true,
        value: originalInnerWidth,
      });
    }
  });
});
