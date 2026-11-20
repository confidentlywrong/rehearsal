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
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { MemoryService } from '../memory.service';
import { DocumentsPanelComponent } from './documents-panel.component';

describe('DocumentsPanelComponent SSOT, dialog & lifecycle regression suite', () => {
  beforeAll(() => {
    if (!getTestBed().platform) {
      TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
    }
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  function setup(memoryOverrides: Partial<Record<keyof MemoryService, unknown>> = {}) {
    const mockMemory = {
      getMemoryConfig: vi.fn().mockResolvedValue({
        text_readable_kinds: ['memo', 'note', 'plan', 'postmortem', 'transcript'],
        max_upload_bytes: 1024,
      }),
      listDocuments: vi.fn().mockResolvedValue([]),
      uploadDocument: vi.fn(),
      deleteDocument: vi.fn().mockResolvedValue(undefined),
      ...memoryOverrides,
    };

    TestBed.configureTestingModule({
      imports: [DocumentsPanelComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: MemoryService, useValue: mockMemory },
      ],
    });
    const fixture = TestBed.createComponent(DocumentsPanelComponent);
    fixture.componentInstance.analysisId = 'a-1';
    return { fixture, mockMemory };
  }

  it('starts with empty SSOT config and hydrates kinds & maxUploadBytes from API', async () => {
    const { fixture } = setup();
    const comp = fixture.componentInstance;
    expect(comp.kinds()).toEqual([]);
    expect(comp.kind).toBe('');
    expect(comp.maxUploadBytes()).toBe(0);
    expect(comp.configState()).toBe('loading');

    fixture.detectChanges(); // triggers ngOnInit -> loadConfig()
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    expect(comp.configState()).toBe('ready');
    expect(comp.kinds()).toEqual(['memo', 'note', 'plan', 'postmortem', 'transcript']);
    expect(comp.kind).toBe('memo');
    expect(comp.maxUploadBytes()).toBe(1024);
    expect(fixture.nativeElement.querySelector('.docs__tabs')).toBeNull();
    fixture.destroy();
  });

  it('surfaces explicit error alert and keeps submit disabled when config fails or returns empty kinds', async () => {
    const { fixture } = setup({
      getMemoryConfig: vi.fn().mockResolvedValue({
        text_readable_kinds: [],
        max_upload_bytes: 1024,
      }),
    });
    const comp = fixture.componentInstance;
    fixture.detectChanges(); // triggers ngOnInit -> loadConfig()
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    expect(comp.configState()).toBe('error');
    expect(comp.kinds()).toEqual([]);
    expect(comp.configError()).toContain('unavailable');
    const alertEl = fixture.nativeElement.querySelector('[role="alert"]');
    expect(alertEl?.textContent).toContain('unavailable');
    const submitBtn = fixture.nativeElement.querySelector('form.docs__upload button[type="submit"]') as HTMLButtonElement;
    expect(submitBtn.disabled).toBe(true);
    fixture.destroy();
  });

  it('enforces maxUploadBytes and plain-text extension checks in onPick()', async () => {
    const { fixture } = setup();
    const comp = fixture.componentInstance;
    await comp.loadConfig();

    const bigFile = new File(['x'.repeat(2048)], 'big.txt', { type: 'text/plain' });
    comp.onPick({ target: { files: [bigFile] } } as unknown as Event);
    expect(comp.hasFileError()).toBe(true);
    expect(comp.uploadError()).toContain('too large');

    const badExt = new File(['abc'], 'binary.exe', { type: 'application/octet-stream' });
    comp.onPick({ target: { files: [badExt] } } as unknown as Event);
    expect(comp.hasFileError()).toBe(true);
    expect(comp.uploadError()).toContain('plain text');

    const okFile = new File(['abc'], 'notes.md', { type: '' });
    comp.onPick({ target: { files: [okFile] } } as unknown as Event);
    expect(comp.hasFileError()).toBe(false);
    expect(comp.uploadError()).toBeNull();
    fixture.destroy();
  });

  it('uses ObsidianConfirmDialog instead of window.confirm on delete and guards ngOnDestroy upload cancellation', async () => {
    const cancelSpy = vi.fn();
    let resolveUpload!: (doc: unknown) => void;
    const doc = {
      id: 'd-1',
      analysis_id: 'a-1',
      kind: 'memo',
      filename: 'plan.txt',
      sha256: 'aa',
      storage_uri: 'file:///x',
      byte_size: 10,
      ingested_at: '2026-06-26T10:00:00Z',
    };
    const { fixture, mockMemory } = setup({
      listDocuments: vi.fn().mockResolvedValue([doc]),
      uploadDocument: vi.fn(() => ({
        promise: new Promise((r) => { resolveUpload = r; }),
        cancel: cancelSpy,
      })),
    });
    const comp = fixture.componentInstance;
    await comp.loadConfig();
    await comp.refresh();
    fixture.detectChanges();

    const confirmSpy = vi.fn();
    Object.defineProperty(window, 'confirm', {
      configurable: true,
      writable: true,
      value: confirmSpy,
    });
    const dialog = (comp as unknown as { confirmDialog: { prompt: ReturnType<typeof vi.fn> } }).confirmDialog;
    const promptSpy = vi.spyOn(dialog, 'prompt').mockResolvedValueOnce(false).mockResolvedValueOnce(true);

    await comp.onDeleteDocument(doc);
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(promptSpy).toHaveBeenCalledTimes(1);
    expect(mockMemory.deleteDocument).not.toHaveBeenCalled();

    await comp.onDeleteDocument(doc);
    expect(mockMemory.deleteDocument).toHaveBeenCalledWith('a-1', 'd-1');
    expect(comp.documents()).toEqual([]);

    // In-flight upload cancelled on destroy and late resolution suppressed
    const file = new File(['abc'], 'new.txt', { type: 'text/plain' });
    comp.pendingFile.set(file);
    const upPromise = comp.onUpload({ preventDefault: vi.fn(), target: { reset: vi.fn() } } as unknown as Event);
    comp.ngOnDestroy();
    expect(cancelSpy).toHaveBeenCalledTimes(1);
    resolveUpload(doc);
    await upPromise;
    expect(comp.documents()).toEqual([]);
  });

  it('prevents uploading oversized or disallowed files even when form.docs__upload is submitted', async () => {
    const { fixture, mockMemory } = setup();
    const comp = fixture.componentInstance;
    fixture.detectChanges();
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    const bigFile = new File(['x'.repeat(2048)], 'oversized.txt', { type: 'text/plain' });
    comp.onPick({ target: { files: [bigFile] } } as unknown as Event);
    expect(comp.hasFileError()).toBe(true);
    expect(comp.pendingFile()).toBeNull();

    const form = fixture.nativeElement.querySelector('form.docs__upload') as HTMLFormElement;
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await Promise.resolve();
    expect(mockMemory.uploadDocument).not.toHaveBeenCalled();
    expect(comp.hasFileError()).toBe(true);
    expect(comp.uploadError()).toContain('too large');

    const badExt = new File(['abc'], 'malware.exe', { type: 'application/octet-stream' });
    comp.onPick({ target: { files: [badExt] } } as unknown as Event);
    expect(comp.hasFileError()).toBe(true);
    expect(comp.pendingFile()).toBeNull();

    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await Promise.resolve();
    expect(mockMemory.uploadDocument).not.toHaveBeenCalled();
    expect(comp.hasFileError()).toBe(true);
    expect(comp.uploadError()).toContain('plain text');

    // Even if pendingFile is injected directly, onUpload re-validates and rejects
    comp.hasFileError.set(false);
    comp.pendingFile.set(badExt);
    await comp.onUpload({ preventDefault: vi.fn(), target: { reset: vi.fn() } } as unknown as Event);
    expect(mockMemory.uploadDocument).not.toHaveBeenCalled();
    expect(comp.hasFileError()).toBe(true);
    expect(comp.pendingFile()).toBeNull();
    fixture.destroy();
  });

  it('tracks deletingDocIds per document so resolving docA does not clear docB in-flight lock', async () => {
    let resolveDeleteA!: () => void;
    let resolveDeleteB!: () => void;
    const docA = {
      id: 'd-a',
      analysis_id: 'a-1',
      kind: 'memo',
      filename: 'alpha.txt',
      sha256: 'aa',
      storage_uri: 'file:///a',
      byte_size: 10,
      ingested_at: '2026-06-26T10:00:00Z',
    };
    const docB = {
      id: 'd-b',
      analysis_id: 'a-1',
      kind: 'memo',
      filename: 'beta.txt',
      sha256: 'bb',
      storage_uri: 'file:///b',
      byte_size: 12,
      ingested_at: '2026-06-26T10:01:00Z',
    };
    const { fixture } = setup({
      listDocuments: vi.fn().mockResolvedValue([docA, docB]),
      deleteDocument: vi.fn((_aid: string, docId: string) => {
        if (docId === 'd-a') {
          return new Promise<void>((r) => { resolveDeleteA = r; });
        }
        return new Promise<void>((r) => { resolveDeleteB = r; });
      }),
    });
    const comp = fixture.componentInstance;
    fixture.detectChanges();
    await Promise.resolve();
    await Promise.resolve();
    await comp.refresh();
    fixture.detectChanges();

    const dialog = (comp as unknown as { confirmDialog: { prompt: ReturnType<typeof vi.fn> } }).confirmDialog;
    vi.spyOn(dialog, 'prompt').mockResolvedValue(true);

    const pA = comp.onDeleteDocument(docA);
    await Promise.resolve();
    const pB = comp.onDeleteDocument(docB);
    await Promise.resolve();
    fixture.detectChanges();

    expect(comp.isDeletingDoc('d-a')).toBe(true);
    expect(comp.isDeletingDoc('d-b')).toBe(true);

    resolveDeleteA();
    await pA;
    fixture.detectChanges();

    expect(comp.isDeletingDoc('d-a')).toBe(false);
    expect(comp.isDeletingDoc('d-b')).toBe(true);
    const deleteBtns = Array.from(
      fixture.nativeElement.querySelectorAll('button.docs__btn-delete'),
    ) as HTMLButtonElement[];
    expect(deleteBtns).toHaveLength(1);
    expect(deleteBtns[0].disabled).toBe(true);

    resolveDeleteB();
    await pB;
    fixture.detectChanges();

    expect(comp.isDeletingDoc('d-b')).toBe(false);
    expect(comp.documents()).toEqual([]);
    fixture.destroy();
  });

  it('ignores concurrent onUpload() and onPaste() calls while an upload is already in flight', async () => {
    let resolveUpload!: (doc: unknown) => void;
    const cancelSpy = vi.fn();
    const doc = {
      id: 'd-u1',
      analysis_id: 'a-1',
      kind: 'memo',
      filename: 'first.txt',
      sha256: 'cc',
      storage_uri: 'file:///u1',
      byte_size: 8,
      ingested_at: '2026-06-26T10:02:00Z',
    };
    const { fixture, mockMemory } = setup({
      uploadDocument: vi.fn(() => ({
        promise: new Promise((r) => { resolveUpload = r; }),
        cancel: cancelSpy,
      })),
    });
    const comp = fixture.componentInstance;
    await comp.loadConfig();

    const fileA = new File(['alpha'], 'alpha.txt', { type: 'text/plain' });
    const fileB = new File(['beta'], 'beta.txt', { type: 'text/plain' });
    comp.pendingFile.set(fileA);
    comp.pasteTitle = 'Pasted note';
    comp.pasteContent = 'Pasted body';

    const firstUpload = comp.onUpload({ preventDefault: vi.fn(), target: { reset: vi.fn() } } as unknown as Event);
    expect(comp.uploading()).toBe(true);
    expect(mockMemory.uploadDocument).toHaveBeenCalledTimes(1);

    // Concurrent onUpload() and onPaste() while uploading() is true must return early
    comp.pendingFile.set(fileB);
    await comp.onUpload({ preventDefault: vi.fn(), target: { reset: vi.fn() } } as unknown as Event);
    await comp.onPaste({ preventDefault: vi.fn() } as unknown as Event);
    expect(mockMemory.uploadDocument).toHaveBeenCalledTimes(1);

    resolveUpload(doc);
    await firstUpload;
    expect(comp.uploading()).toBe(false);
    expect(comp.documents()).toEqual([doc]);

    // After first upload finishes, onPaste() proceeds normally and blocks re-entrant onPaste()/onUpload()
    let resolvePaste!: (doc: unknown) => void;
    const doc2 = { ...doc, id: 'd-u2', filename: 'Pasted note.txt' };
    (mockMemory.uploadDocument as ReturnType<typeof vi.fn>).mockImplementationOnce(() => ({
      promise: new Promise((r) => { resolvePaste = r; }),
      cancel: vi.fn(),
    }));
    const firstPaste = comp.onPaste({ preventDefault: vi.fn() } as unknown as Event);
    expect(comp.uploading()).toBe(true);
    expect(mockMemory.uploadDocument).toHaveBeenCalledTimes(2);

    await comp.onPaste({ preventDefault: vi.fn() } as unknown as Event);
    comp.pendingFile.set(fileB);
    await comp.onUpload({ preventDefault: vi.fn(), target: { reset: vi.fn() } } as unknown as Event);
    expect(mockMemory.uploadDocument).toHaveBeenCalledTimes(2);

    resolvePaste(doc2);
    await firstPaste;
    expect(comp.uploading()).toBe(false);
    expect(comp.documents()).toEqual([doc, doc2]);
    fixture.destroy();
  });

  it('surfaces HTTP 201 indexing warning explicitly in Obsidian warning state for both upload and paste', async () => {
    const serverWarning =
      'Document saved but indexing failed. It will not appear in search results until re-indexed.';
    const doc1 = {
      id: 'd-w1',
      analysis_id: 'a-1',
      kind: 'memo',
      filename: 'warn1.txt',
      sha256: 'aa',
      storage_uri: 'file:///w1',
      byte_size: 10,
      ingested_at: '2026-06-26T10:00:00Z',
    };
    const doc2 = {
      ...doc1,
      id: 'd-w2',
      filename: 'Pasted warn.txt',
    };
    const { fixture } = setup({
      uploadDocument: vi
        .fn()
        .mockReturnValueOnce({
          promise: Promise.resolve({ ...doc1, warning: serverWarning }),
          cancel: vi.fn(),
        })
        .mockReturnValueOnce({
          promise: Promise.resolve({ ...doc2, warning: serverWarning }),
          cancel: vi.fn(),
        }),
    });
    const comp = fixture.componentInstance;
    fixture.detectChanges();
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    const file = new File(['hello'], 'warn1.txt', { type: 'text/plain' });
    comp.pendingFile.set(file);
    await comp.onUpload({ preventDefault: vi.fn(), target: { reset: vi.fn() } } as unknown as Event);
    fixture.detectChanges();

    expect(comp.uploadWarning()).toBe(serverWarning);
    expect(comp.documents()).toEqual([doc1]);
    const uploadWarnEl = fixture.nativeElement.querySelector('form.docs__upload .docs__warning');
    expect(uploadWarnEl?.textContent).toContain(serverWarning);

    // Picking a new file clears stale uploadWarning
    comp.onPick({ target: { files: [file] } } as unknown as Event);
    expect(comp.uploadWarning()).toBeNull();

    // Paste upload also surfaces server warning explicitly
    comp.pasteTitle = 'Pasted warn';
    comp.pasteContent = 'hello';
    await comp.onPaste({ preventDefault: vi.fn() } as unknown as Event);
    fixture.detectChanges();

    expect(comp.pasteWarning()).toBe(serverWarning);
    expect(comp.documents()).toEqual([doc1, doc2]);
    const pasteWarnEl = fixture.nativeElement.querySelector('form.docs__paste .docs__warning');
    expect(pasteWarnEl?.textContent).toContain(serverWarning);
    fixture.destroy();
  });
});
