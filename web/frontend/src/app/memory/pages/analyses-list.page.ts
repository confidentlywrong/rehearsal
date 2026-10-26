// Copyright 2026 The Confidently Wrong Authors
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
 * AnalysesListPageComponent — landing surface for /memory.
 *
 * Shows every analysis the operator owns, with a "+ New analysis"
 * trigger that opens an inline create form. Click on a card → routes
 * to /memory/:analysisId for the detail view.
 *
 * "Never invent data" rule: when the analyses list is empty we show
 * an empty-state instead of pre-populating a sample analysis.
 */

import {
  ChangeDetectionStrategy,
  Component,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink, Router } from '@angular/router';

import { MemoryApiError, MemoryService } from '../memory.service';
import { Analysis } from '../memory.types';

type LoadState = 'loading' | 'ready' | 'error';

@Component({
  selector: 'app-analyses-list-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <section class="analyses">
      <div class="analyses__top">
        <div class="analyses__header-group">
          <h2 class="analyses__heading">Your analyses</h2>
          <p class="analyses__subheading">Select or create an analysis to upload documents.</p>
        </div>
        <button
          type="button"
          class="analyses__btn"
          (click)="toggleCreate()"
          [disabled]="creating()"
        >
          {{ showCreate() ? 'Cancel' : '+ New analysis' }}
        </button>
      </div>



      <form
        *ngIf="showCreate()"
        class="analyses__create"
        (submit)="onCreate($event)"
      >
        <div class="analyses__create-row">
          <label>
            <span>Organization name (optional)</span>
            <input
              type="text"
              [(ngModel)]="formOrg"
              name="org_name"
              maxlength="200"
              autocomplete="off"
            />
          </label>
          <label>
            <span>Title *</span>
            <input
              type="text"
              [(ngModel)]="formTitle"
              name="title"
              required
              maxlength="200"
              autocomplete="off"
            />
          </label>
        </div>
        <label class="analyses__create-desc">
          <span>Description (optional)</span>
          <textarea
            [(ngModel)]="formDesc"
            name="description"
            rows="2"
            maxlength="2000"
          ></textarea>
        </label>
        <div class="analyses__create-actions">
          <button
            type="submit"
            class="analyses__btn analyses__btn--primary"
            [disabled]="creating() || !formTitle.trim()"
          >
            {{ creating() ? 'Creating…' : 'Create' }}
          </button>
        </div>
        <p *ngIf="createError()" class="analyses__error">{{ createError() }}</p>
      </form>

      <p *ngIf="deleteError()" class="analyses__error" style="margin-bottom: 16px;">{{ deleteError() }}</p>

      <div *ngIf="loadState() === 'loading'" class="analyses__loading">
        Loading analyses…
      </div>

      <div
        *ngIf="loadState() === 'error'"
        class="analyses__error analyses__error--block"
      >
        Unable to load analyses. Try refreshing.
      </div>

      <ng-container *ngIf="loadState() === 'ready'">
        <div *ngIf="!analyses().length" class="analyses__empty">
          No analyses yet. Create one above to begin uploading documents and
          extracting memory.
        </div>
        <ul *ngIf="analyses().length" class="analyses__list">
          <li
            *ngFor="let a of analyses()"
            class="analyses__card"
            [routerLink]="['/settings/memory', a.id]"
          >
            <div class="analyses__card-header">
              <div class="analyses__card-title">{{ a.title }}</div>
              <button
                class="analyses__card-delete"
                (click)="$event.stopPropagation(); onDelete(a)"
                title="Delete analysis"
              >
                ✕
              </button>
            </div>
            <div *ngIf="a.org_name" class="analyses__card-org">{{ a.org_name }}</div>
            <div class="analyses__card-meta">
              Created {{ a.created_at | date: 'MMM d, y' }}

            </div>
            <p *ngIf="a.description" class="analyses__card-desc">
              {{ a.description }}
            </p>
          </li>
        </ul>
      </ng-container>
    </section>
  `,
  styles: [
    `
      .analyses__top {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 18px;
      }
      .analyses__heading {
        margin: 0;
        font-size: 18px;
        font-weight: 600;
        letter-spacing: 0.02em;
      }
      .analyses__subheading {
        margin: 4px 0 0 0;
        font-size: 13px;
        color: #89a3b8;
      }
      .analyses__btn {
        background: transparent;
        border: 1px solid rgba(0, 255, 200, 0.35);
        color: #00ffcc;
        padding: 8px 14px;
        border-radius: 6px;
        font-size: 13px;
        cursor: pointer;
      }
      .analyses__btn:hover {
        background: rgba(0, 255, 200, 0.08);
      }
      .analyses__btn--primary {
        background: #00ffcc;
        color: #07111a;
        font-weight: 600;
      }
      .analyses__btn--primary:hover {
        background: #00e6b8;
      }
      .analyses__btn:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }
      .analyses__create {
        background: rgba(0, 255, 200, 0.04);
        border: 1px solid rgba(0, 255, 200, 0.18);
        border-radius: 8px;
        padding: 18px;
        margin-bottom: 24px;
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .analyses__create-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 14px;
      }
      .analyses__create label,
      .analyses__create-desc {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 12px;
        color: #c8d8e6;
      }
      .analyses__create input,
      .analyses__create textarea {
        background: #0d1b26;
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 4px;
        color: #e8f8ff;
        padding: 8px 10px;
        font: inherit;
      }
      .analyses__create input:focus,
      .analyses__create textarea:focus {
        outline: none;
        border-color: #00ffcc;
      }
      .analyses__create-actions {
        display: flex;
        justify-content: flex-end;
      }
      .analyses__error {
        color: #ff6b6b;
        font-size: 12px;
        margin: 4px 0 0;
      }
      .analyses__error--block {
        background: rgba(255, 80, 80, 0.08);
        border-left: 3px solid #ff6b6b;
        padding: 12px 16px;
        border-radius: 0 4px 4px 0;
      }
      .analyses__loading,
      .analyses__empty {
        background: rgba(255, 255, 255, 0.03);
        padding: 20px 24px;
        border-radius: 6px;
        font-size: 13px;
        color: #9fb4c6;
      }
      .analyses__list {
        list-style: none;
        padding: 0;
        margin: 0;
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 14px;
      }
      .analyses__card {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(0, 255, 200, 0.1);
        border-radius: 8px;
        padding: 16px 18px;
        cursor: pointer;
        transition: border-color 120ms, background 120ms, transform 120ms;
      }
      .analyses__card:hover {
        border-color: rgba(0, 255, 200, 0.4);
        background: rgba(0, 255, 200, 0.05);
        transform: translateY(-1px);
      }
      .analyses__card-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
      }
      .analyses__card-title {
        font-weight: 600;
        font-size: 14px;
        letter-spacing: 0.01em;
      }
      .analyses__card-delete {
        background: transparent;
        border: none;
        color: #ff6b6b;
        font-size: 14px;
        cursor: pointer;
        padding: 4px;
        line-height: 1;
        opacity: 0.6;
        transition: opacity 120ms;
        margin-top: -4px;
        margin-right: -4px;
      }
      .analyses__card-delete:hover {
        opacity: 1;
      }
      .analyses__card-org {
        color: #9fb4c6;
        font-size: 12px;
        margin-top: 2px;
      }
      .analyses__card-meta {
        color: #6c8294;
        font-size: 11px;
        margin-top: 10px;
        display: flex;
        gap: 10px;
        align-items: center;
      }
      .analyses__badge {
        background: rgba(0, 255, 200, 0.18);
        color: #00ffcc;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .analyses__card-desc {
        margin: 10px 0 0;
        color: #c8d8e6;
        font-size: 12px;
        line-height: 1.45;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
      }

    `,
  ],
})
export class AnalysesListPageComponent {
  private memory = inject(MemoryService);
  private router = inject(Router);

  readonly loadState = signal<LoadState>('loading');
  readonly analyses = signal<Analysis[]>([]);
  readonly showCreate = signal(false);
  readonly creating = signal(false);
  readonly createError = signal<string | null>(null);
  readonly deleteError = signal<string | null>(null);

  formOrg = '';
  formTitle = '';
  formDesc = '';

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.loadState.set('loading');
    try {
      const list = await this.memory.listAnalyses();
      this.analyses.set(list);
      this.loadState.set('ready');
    } catch {
      this.loadState.set('error');
    }
  }

  toggleCreate(): void {
    this.showCreate.update(v => !v);
    if (!this.showCreate()) {
      this.formOrg = '';
      this.formTitle = '';
      this.formDesc = '';
      this.createError.set(null);
    }
  }

  async onCreate(ev: Event): Promise<void> {
    ev.preventDefault();
    if (!this.formTitle.trim()) return;
    this.creating.set(true);
    this.createError.set(null);
    try {
      const created = await this.memory.createAnalysis({
        org_name: this.formOrg.trim() || undefined,
        title: this.formTitle.trim(),
        description: this.formDesc.trim() || undefined,
      });
      this.analyses.update(arr => [created, ...arr]);
      this.formOrg = '';
      this.formTitle = '';
      this.formDesc = '';
      this.showCreate.set(false);
      void this.router.navigate(['/settings/memory', created.id]);
    } catch (e) {
      const msg =
        e instanceof MemoryApiError
          ? `Create failed: ${e.detail ?? e.code}`
          : 'Create failed.';
      this.createError.set(msg);
    } finally {
      this.creating.set(false);
    }
  }

  async onDelete(a: Analysis): Promise<void> {
    if (!window.confirm(`Are you sure you want to delete analysis "${a.title}"? This cannot be undone.`)) {
      return;
    }
    this.deleteError.set(null);
    try {
      await this.memory.deleteAnalysis(a.id);
      this.analyses.update(list => list.filter(item => item.id !== a.id));
    } catch (e) {
      this.deleteError.set('Failed to delete analysis. Please try again.');
      console.error(e);
    }
  }
}
