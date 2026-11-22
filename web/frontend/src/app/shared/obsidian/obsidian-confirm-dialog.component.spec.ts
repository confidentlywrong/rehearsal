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
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { ObsidianConfirmDialogComponent } from './obsidian-confirm-dialog.component';

describe('ObsidianConfirmDialogComponent', () => {
  beforeAll(() => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
  });

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ObsidianConfirmDialogComponent],
      providers: [provideZonelessChangeDetection()],
    }).compileComponents();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  it('uses native <dialog> without redundant role/aria-modal, calls showModal(), and places autofocus on Cancel when destructive', () => {
    const fixture = TestBed.createComponent(ObsidianConfirmDialogComponent);
    fixture.componentRef.setInput('title', 'Delete analysis');
    fixture.componentRef.setInput('message', 'This cannot be undone.');
    fixture.componentRef.setInput('destructive', true);
    fixture.detectChanges();

    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;
    expect(dialog).not.toBeNull();
    expect(dialog.hasAttribute('role')).toBe(false);
    expect(dialog.hasAttribute('aria-modal')).toBe(false);
    expect(dialog.getAttribute('closedby')).toBe('any');

    const buttons = Array.from(
      fixture.nativeElement.querySelectorAll('.obsidian-dialog__actions button'),
    ) as HTMLButtonElement[];
    expect(buttons).toHaveLength(2);
    expect(buttons[0].hasAttribute('autofocus')).toBe(true); // Cancel button
    expect(buttons[1].hasAttribute('autofocus')).toBe(false);
    expect(buttons[1].classList.contains('apple-btn-destructive')).toBe(true);

    const showModalSpy = vi.spyOn(dialog, 'showModal');
    fixture.componentInstance.open = true;
    fixture.detectChanges();
    expect(showModalSpy).toHaveBeenCalledTimes(1);
    expect(dialog.open).toBe(true);

    fixture.destroy();
  });

  it('resolves prompt() true on confirm, false on cancel/Escape, and resets dialog.returnValue to empty string', async () => {
    const fixture = TestBed.createComponent(ObsidianConfirmDialogComponent);
    fixture.detectChanges();
    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;

    const confirmedSpy = vi.fn();
    const cancelledSpy = vi.fn();
    fixture.componentInstance.confirmed.subscribe(confirmedSpy);
    fixture.componentInstance.cancelled.subscribe(cancelledSpy);

    const firstPromise = fixture.componentInstance.prompt({
      title: 'Delete document',
      message: 'Remove memo.txt?',
      confirmLabel: 'Delete',
      destructive: true,
    });
    fixture.detectChanges();
    expect(dialog.open).toBe(true);

    const confirmBtn = fixture.nativeElement.querySelector(
      '.obsidian-dialog__actions button:last-child',
    ) as HTMLButtonElement;
    confirmBtn.click();
    expect(await firstPromise).toBe(true);
    expect(confirmedSpy).toHaveBeenCalledTimes(1);
    expect(dialog.returnValue).toBe('');
    expect(dialog.open).toBe(false);

    // Simulate Escape / close event with empty returnValue
    const secondPromise = fixture.componentInstance.prompt({ title: 'Second check' });
    fixture.detectChanges();
    dialog.returnValue = '';
    dialog.close();
    expect(await secondPromise).toBe(false);
    expect(cancelledSpy).toHaveBeenCalledTimes(1);
    expect(dialog.returnValue).toBe('');

    fixture.destroy();
  });

  it('light-dismisses via pointerdown+pointerup outside dialog rect when closedBy is unsupported', async () => {
    const fixture = TestBed.createComponent(ObsidianConfirmDialogComponent);
    fixture.detectChanges();
    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;

    vi.spyOn(dialog, 'getBoundingClientRect').mockReturnValue({
      top: 100,
      left: 100,
      right: 400,
      bottom: 300,
      width: 300,
      height: 200,
      x: 100,
      y: 100,
      toJSON: () => ({}),
    });

    const p = fixture.componentInstance.prompt({ title: 'Backdrop test' });
    fixture.detectChanges();
    expect(dialog.open).toBe(true);

    // Drag starting inside and releasing outside should NOT dismiss
    fixture.componentInstance.handlePointerDown({ clientX: 150, clientY: 150 } as PointerEvent);
    fixture.componentInstance.handlePointerUp({ clientX: 10, clientY: 10 } as PointerEvent);
    expect(dialog.open).toBe(true);

    // Press and release outside DOES dismiss
    fixture.componentInstance.handlePointerDown({ clientX: 10, clientY: 10 } as PointerEvent);
    fixture.componentInstance.handlePointerUp({ clientX: 10, clientY: 10 } as PointerEvent);
    expect(await p).toBe(false);
    expect(dialog.open).toBe(false);

    fixture.destroy();
  });
});
