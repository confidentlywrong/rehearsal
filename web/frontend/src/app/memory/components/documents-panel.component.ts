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
 *
 * DocumentsPanelComponent — manages seed documents for one analysis.
 *
 *   1. Upload form (kind dropdown + file picker).
 *   2. List of uploaded documents with filename, kind, size, ingest time.
 */

import {
  ChangeDetectionStrategy,
  Component,
  Input,
  OnChanges,
  OnInit,
  SimpleChanges,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import { MemoryApiError, MemoryService } from '../memory.service';
import { MemoryDocument } from '../memory.types';



@Component({
  selector: 'app-documents-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule],
  template: `
    <section class="docs">
      <header class="docs__header">
        <h3>Seed documents</h3>
        <p class="docs__lead">
          Upload memos, transcripts, post-mortems, or plans. Because the extractor agent
          consumes raw text, <strong>only plain-text files (.txt, .md, .csv) are supported.</strong>
          Each document becomes a citable source for extracted candidates. Extraction is
          operator-triggered — nothing runs on upload.
        </p>
      </header>

      <div class="docs__tabs">
        <button type="button" [class.docs__tab--active]="inputMode() === 'file'" (click)="setTab('file')">Upload File</button>
        <button type="button" [class.docs__tab--active]="inputMode() === 'text'" (click)="setTab('text')">Paste Text</button>
      </div>

      <form *ngIf="inputMode() === 'file'" class="docs__upload" (submit)="onUpload($event)">
        <label class="docs__field">
          <span>Kind</span>
          <select [(ngModel)]="kind" name="kind">
            <option *ngFor="let k of kinds()" [value]="k">{{ k }}</option>
          </select>
        </label>
        <label class="docs__field docs__field--file">
          <span>File</span>
          <input
            type="file"
            #fileInput
            accept=".txt,.md,.markdown,.csv,text/*"
            (change)="onPick($event)"
          />
        </label>
        <button
          *ngIf="!uploading()"
          type="submit"
          class="docs__btn docs__btn--primary"
          [disabled]="!pendingFile() || hasFileError()"
        >
          Upload
        </button>
        <button
          *ngIf="uploading()"
          type="button"
          class="docs__btn"
          (click)="onCancelUpload()"
        >
          Cancel ({{ uploadProgress() !== null ? uploadProgress() + '%' : '...' }})
        </button>
        <p *ngIf="uploadError()" class="docs__error">{{ uploadError() }}</p>
      </form>

      <form *ngIf="inputMode() === 'text'" class="docs__upload docs__paste" (submit)="onPaste($event)">
        <div class="docs__paste-row">
          <label class="docs__field">
            <span>Kind</span>
            <select [(ngModel)]="kind" name="kind">
              <option *ngFor="let k of kinds()" [value]="k">{{ k }}</option>
            </select>
          </label>
          <label class="docs__field docs__field--fill">
            <span>Title</span>
            <input type="text" [(ngModel)]="pasteTitle" name="title" placeholder="e.g. Q3 Roadmap Notes" required />
          </label>
        </div>
        <label class="docs__field docs__field--full">
          <span>Text content</span>
          <textarea [(ngModel)]="pasteContent" name="content" rows="6" placeholder="Paste or type knowledge here..." required></textarea>
        </label>
        <div class="docs__form-actions">
          <button
            *ngIf="!uploading()"
            type="submit"
            class="docs__btn docs__btn--primary"
            [disabled]="!pasteTitle.trim() || !pasteContent.trim()"
          >
            Save as Document
          </button>
          <button
            *ngIf="uploading()"
            type="button"
            class="docs__btn"
            disabled
          >
            Saving...
          </button>
        </div>
        <p *ngIf="uploadError()" class="docs__error">{{ uploadError() }}</p>
      </form>

      <div *ngIf="loadState() === 'loading'" class="docs__loading">
        Loading documents…
      </div>

      <ng-container *ngIf="loadState() === 'ready'">
        <div *ngIf="!documents().length" class="docs__empty">
          No documents uploaded yet.
        </div>

        <table *ngIf="documents().length" class="docs__table">
          <thead>
            <tr>
              <th>Filename</th>
              <th>Kind</th>
              <th>Size</th>
              <th>Ingested</th>
              <th class="docs__col-actions">Delete</th>
            </tr>
          </thead>
          <tbody>
            <tr *ngFor="let d of documents()">
              <td>{{ d.filename }}</td>
              <td>
                <span class="docs__kind">{{ d.kind }}</span>
              </td>
              <td>{{ formatBytes(d.byte_size) }}</td>
              <td>{{ d.ingested_at | date: 'MMM d, HH:mm' }}</td>
              <td class="docs__col-actions">
                <button
                  type="button"
                  class="docs__btn"
                  style="color: #ff6b6b; border-color: #ff6b6b;"
                  (click)="onDeleteDocument(d)"
                >
                  Delete
                </button>
              </td>
            </tr>
          </tbody>
        </table>

      </ng-container>
    </section>
  `,
  styles: [
    `
      .docs__header h3 {
        margin: 0 0 4px;
        font-size: 16px;
      }
      .docs__lead {
        margin: 0 0 18px;
        color: #9fb4c6;
        font-size: 12px;
        max-width: 720px;
      }
      .docs__tabs {
        display: flex;
        gap: 16px;
        margin-bottom: 12px;
      }
      .docs__tabs button {
        background: transparent;
        border: 0;
        border-bottom: 2px solid transparent;
        color: #9fb4c6;
        padding: 4px 0 8px;
        font-size: 13px;
        cursor: pointer;
        font-weight: 500;
      }
      .docs__tabs button:hover {
        color: #e8f8ff;
      }
      .docs__tab--active {
        color: #00ffcc !important;
        border-bottom-color: #00ffcc !important;
      }
      .docs__upload {
        display: flex;
        align-items: flex-end;
        gap: 12px;
        background: rgba(0, 255, 200, 0.04);
        border: 1px solid rgba(0, 255, 200, 0.18);
        border-radius: 8px;
        padding: 14px 16px;
        margin-bottom: 22px;
        flex-wrap: wrap;
      }
      .docs__paste {
        flex-direction: column;
        align-items: stretch;
      }
      .docs__paste-row {
        display: flex;
        gap: 12px;
        align-items: flex-end;
      }
      .docs__field {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 12px;
        color: #c8d8e6;
      }
      .docs__field select,
      .docs__field input,
      .docs__field textarea {
        background: #0d1b26;
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 4px;
        color: #e8f8ff;
        padding: 6px 10px;
        font: inherit;
      }
      .docs__field textarea {
        resize: vertical;
        min-height: 80px;
      }
      .docs__field--file input {
        padding: 6px 0;
      }
      .docs__field--fill {
        flex: 1;
      }
      .docs__field--full {
        width: 100%;
      }
      .docs__form-actions {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 12px;
        margin-top: 4px;
      }
      .docs__btn {
        background: transparent;
        border: 1px solid rgba(0, 255, 200, 0.35);
        color: #00ffcc;
        padding: 7px 14px;
        border-radius: 5px;
        font-size: 12px;
        cursor: pointer;
      }
      .docs__btn:hover {
        background: rgba(0, 255, 200, 0.08);
      }
      .docs__btn--primary {
        background: #00ffcc;
        color: #07111a;
        font-weight: 600;
        border-color: #00ffcc;
      }
      .docs__btn--primary:hover {
        background: #00e6b8;
      }
      .docs__btn:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }
      .docs__loading,
      .docs__empty {
        background: rgba(255, 255, 255, 0.03);
        padding: 16px 18px;
        border-radius: 6px;
        font-size: 12px;
        color: #9fb4c6;
      }
      .docs__table {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
      }
      .docs__table th,
      .docs__table td {
        text-align: left;
        padding: 10px 12px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      }
      .docs__table th {
        color: #9fb4c6;
        font-weight: 500;
        text-transform: uppercase;
        font-size: 10px;
        letter-spacing: 0.08em;
      }
      .docs__col-actions {
        text-align: right;
      }
      .docs__kind {
        background: rgba(0, 255, 200, 0.12);
        color: #00ffcc;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 11px;
      }
      .docs__run {
        margin-left: 10px;
        color: #88e6cc;
        font-size: 11px;
      }
      .docs__dropped {
        color: #ffb35a;
      }
      .docs__not-text {
        color: #6c8294;
        font-size: 11px;
      }
      .docs__error {
        color: #ff6b6b;
        font-size: 12px;
        margin: 8px 0 0;
      }
    `,
  ],
})
export class DocumentsPanelComponent implements OnInit, OnChanges {
  @Input() analysisId!: string;

  private readonly memory = inject(MemoryService);

  readonly loadState = signal<'loading' | 'ready' | 'error'>('loading');
  readonly documents = signal<MemoryDocument[]>([]);

  readonly kinds = signal<string[]>(['memo']);
  kind = 'memo';

  readonly inputMode = signal<'file' | 'text'>('file');
  pasteTitle = '';
  pasteContent = '';

  readonly maxUploadBytes = signal(50 * 1024 * 1024);
  readonly uploading = signal(false);
  readonly uploadProgress = signal<number | null>(null);
  private currentUploadCancel: (() => void) | null = null;
  readonly uploadError = signal<string | null>(null);
  readonly pendingFile = signal<File | null>(null);
  readonly hasFileError = signal(false);

  ngOnInit(): void {
    void this.loadConfig();
  }

  async loadConfig(): Promise<void> {
    try {
      const config = await this.memory.getMemoryConfig();
      this.kinds.set(config.text_readable_kinds);
      if (config.max_upload_bytes) {
        this.maxUploadBytes.set(config.max_upload_bytes);
      }
      if (config.text_readable_kinds.length > 0) {
        this.kind = config.text_readable_kinds[0];
      }
    } catch {
      // Fallback if config fails
      this.kinds.set(['memo', 'text']);
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['analysisId']) {
      void this.refresh();
    }
  }

  async refresh(): Promise<void> {
    this.loadState.set('loading');
    try {
      const docs = await this.memory.listDocuments(this.analysisId);
      this.documents.set(docs);
      this.loadState.set('ready');
    } catch {
      this.loadState.set('error');
    }
  }

  setTab(mode: 'file' | 'text'): void {
    this.inputMode.set(mode);
    this.uploadError.set(null);
    this.hasFileError.set(false);
  }

  onPick(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    const file = input.files?.[0] ?? null;
    this.pendingFile.set(file);
    
    if (file) {
      if (file.size > this.maxUploadBytes()) {
        this.hasFileError.set(true);
        this.uploadError.set(`File is too large. Maximum size is ${this.formatBytes(this.maxUploadBytes())}.`);
        return;
      }

      const lowerName = file.name.toLowerCase();
      const isText = file.type.startsWith('text/') ||
                     lowerName.endsWith('.txt') ||
                     lowerName.endsWith('.md') ||
                     lowerName.endsWith('.markdown') ||
                     lowerName.endsWith('.csv');
                     
      if (!isText) {
        this.hasFileError.set(true);
        this.uploadError.set('Only plain text files (.txt, .md, .csv) are currently supported for extraction.');
      } else {
        this.hasFileError.set(false);
        this.uploadError.set(null);
      }
    } else {
      this.hasFileError.set(false);
      this.uploadError.set(null);
    }
  }

  async onUpload(ev: Event): Promise<void> {
    ev.preventDefault();
    const file = this.pendingFile();
    if (!file) return;
    this.uploading.set(true);
    this.uploadProgress.set(0);
    this.uploadError.set(null);
    this.hasFileError.set(false);
    try {
      const { promise, cancel } = this.memory.uploadDocument(
        this.analysisId,
        this.kind,
        file,
        (pct) => this.uploadProgress.set(pct)
      );
      this.currentUploadCancel = cancel;
      
      const doc = await promise;
      this.documents.update(arr => [...arr, doc]);
      this.pendingFile.set(null);
      // Reset the file input so the same file can be re-picked.
      const form = ev.target as HTMLFormElement;
      form.reset();
      this.kind = 'memo';
    } catch (e) {
      const msg =
        e instanceof MemoryApiError
          ? (e.code === 'aborted' ? 'Upload cancelled.' : `Upload failed: ${e.detail ?? e.code}`)
          : 'Upload failed.';
      this.uploadError.set(msg);
    } finally {
      this.uploading.set(false);
      this.uploadProgress.set(null);
      this.currentUploadCancel = null;
    }
  }

  async onPaste(ev: Event): Promise<void> {
    ev.preventDefault();
    if (!this.pasteTitle.trim() || !this.pasteContent.trim()) return;

    this.uploading.set(true);
    this.uploadError.set(null);
    this.uploadProgress.set(null);

    // Provide a file wrapper so it flows precisely down the identical
    // path as a file picked from the OS dialog.
    const textFile = new File([this.pasteContent], this.pasteTitle.trim() + '.txt', {
      type: 'text/plain',
    });

    try {
      const { promise } = this.memory.uploadDocument(
        this.analysisId,
        this.kind,
        textFile,
        pct => this.uploadProgress.set(pct),
      );
      
      const doc = await promise;
      this.documents.update(arr => [...arr, doc]);
      
      // Reset form
      this.pasteTitle = '';
      this.pasteContent = '';
      this.kind = 'memo';
    } catch (e) {
      const msg = e instanceof MemoryApiError ? `Save failed: ${e.detail ?? e.code}` : 'Save failed.';
      this.uploadError.set(msg);
    } finally {
      this.uploading.set(false);
      this.uploadProgress.set(null);
    }
  }

  onCancelUpload(): void {
    if (this.currentUploadCancel) {
      this.currentUploadCancel();
    }
  }

  formatBytes(n: number): string {
    if (!n) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  async onDeleteDocument(doc: MemoryDocument): Promise<void> {
    if (!window.confirm(`Are you sure you want to delete "${doc.filename}"? This cannot be undone.`)) {
      return;
    }
    try {
      await this.memory.deleteDocument(this.analysisId, doc.id);
      this.documents.update(docs => docs.filter(d => d.id !== doc.id));
    } catch (e) {
      this.uploadError.set('Failed to delete document. Please try again.');
      console.error(e);
    }
  }
}
