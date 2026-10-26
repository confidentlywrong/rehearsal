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
 * AnalysisDetailPageComponent — the per-analysis workspace at
 *   /memory/:analysisId
 *
 * Owns the analysis header (org_name, title) and the Documents tab
 * (DocumentsPanelComponent) for uploading and managing seed documents.
 *
 * Tab state is local component state, not a separate route — keeping
 * the tab in sync with a child route would add navigation noise
 * without giving the operator any deep-link benefit.
 */

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { map } from 'rxjs';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import { MemoryApiError, MemoryService } from '../memory.service';
import { Analysis } from '../memory.types';
import { DocumentsPanelComponent } from '../components/documents-panel.component';

type Tab = 'documents';

@Component({
  selector: 'app-analysis-detail-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    FormsModule,
    RouterLink,
    DocumentsPanelComponent,
  ],
  template: `
    <div *ngIf="analysisId() as id">
      <nav class="detail__breadcrumb">
        <a routerLink="/settings/memory">All analyses</a>
        <span class="detail__crumb-sep">/</span>
        <span>{{ analysis()?.title || 'Analysis' }}</span>
      </nav>

      <div *ngIf="loadState() === 'loading'" class="detail__loading">
        Loading analysis…
      </div>

      <div *ngIf="loadState() === 'not-found'" class="detail__error">
        Analysis not found, or you do not have access.
        <a routerLink="/settings/memory">Back to all analyses.</a>
      </div>

      <div *ngIf="loadState() === 'error'" class="detail__error">
        Unable to load analysis. Try refreshing.
      </div>

      <ng-container *ngIf="loadState() === 'ready' && analysis() as a">
        <header class="detail__header">
          <div>
            <h2 class="detail__title">{{ a.title }}</h2>
            <p *ngIf="a.org_name" class="detail__org">{{ a.org_name }}</p>
            <p *ngIf="a.description" class="detail__desc">
              {{ a.description }}
            </p>
          </div>
          <div class="detail__sharing-block">
            <button
              type="button"
              class="detail__btn-delete"
              (click)="onDeleteAnalysis(a)"
            >
              Delete Analysis
            </button>
          </div>
        </header>

        <nav class="detail__tabs">
          <button
            type="button"
            [class.detail__tab--active]="tab() === 'documents'"
            (click)="tab.set('documents')"
          >
            Documents
          </button>
        </nav>

        <div [hidden]="tab() !== 'documents'">
          <app-documents-panel
            [analysisId]="id"
          ></app-documents-panel>
        </div>
      </ng-container>
    </div>
  `,
  styles: [
    `
      .detail__breadcrumb {
        font-size: 12px;
        color: #9fb4c6;
        margin-bottom: 18px;
      }
      .detail__breadcrumb a {
        color: #00ffcc;
        text-decoration: none;
      }
      .detail__breadcrumb a:hover {
        text-decoration: underline;
      }
      .detail__crumb-sep {
        margin: 0 6px;
        color: #41566a;
      }
      .detail__loading,
      .detail__error {
        background: rgba(255, 255, 255, 0.03);
        padding: 18px 20px;
        border-radius: 6px;
        font-size: 13px;
        color: #9fb4c6;
      }
      .detail__error a {
        color: #00ffcc;
        margin-left: 8px;
      }
      .detail__header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 24px;
        margin-bottom: 20px;
      }
      .detail__title {
        margin: 0;
        font-size: 20px;
        font-weight: 600;
      }
      .detail__org {
        margin: 4px 0 0;
        color: #9fb4c6;
        font-size: 13px;
      }
      .detail__desc {
        margin: 10px 0 0;
        color: #c8d8e6;
        font-size: 12px;
        max-width: 720px;
        line-height: 1.5;
      }
      .detail__sharing {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 12px;
        color: #c8d8e6;
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.06);
        padding: 8px 12px;
        border-radius: 6px;
        cursor: pointer;
        white-space: nowrap;
      }
      .detail__sharing-block {
        display: flex;
        flex-direction: column;
        gap: 6px;
        align-items: flex-end;
      }
      .detail__sharing-since {
        margin: 0;
        font-size: 11px;
        color: #80ffd2;
        text-align: right;
        max-width: 260px;
        line-height: 1.35;
      }
      .detail__tabs {
        display: flex;
        gap: 4px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.06);
        margin: 16px 0 22px;
      }
      .detail__tabs button {
        background: transparent;
        border: 0;
        border-bottom: 2px solid transparent;
        color: #9fb4c6;
        padding: 8px 14px;
        font-size: 13px;
        cursor: pointer;
      }
      .detail__tabs button:hover {
        color: #e8f8ff;
      }
      .detail__tab--active {
        color: #00ffcc !important;
        border-bottom-color: #00ffcc !important;
      }
      .detail__btn-delete {
        background: transparent;
        border: 1px solid #ff6b6b;
        color: #ff6b6b;
        border-radius: 6px;
        padding: 6px 12px;
        font-size: 12px;
        cursor: pointer;
        align-self: flex-end;
      }
      .detail__btn-delete:hover {
        background: rgba(255, 107, 107, 0.1);
      }
    `,
  ],
})
export class AnalysisDetailPageComponent {
  private readonly memory = inject(MemoryService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  readonly analysisId = toSignal(
    this.route.paramMap.pipe(map(p => p.get('analysisId'))),
    { initialValue: this.route.snapshot.paramMap.get('analysisId') }
  );

  readonly analysis = signal<Analysis | null>(null);
  readonly loadState = signal<'loading' | 'ready' | 'not-found' | 'error'>(
    'loading',
  );
  readonly tab = signal<Tab>('documents');

  constructor() {
    effect(() => {
      const id = this.analysisId();
      if (id) void this.refresh();
    });
  }

  async refresh(): Promise<void> {
    const id = this.analysisId();
    if (!id) {
      this.loadState.set('not-found');
      return;
    }
    this.loadState.set('loading');
    try {
      const a = await this.memory.getAnalysis(id);
      this.analysis.set(a);
      this.loadState.set('ready');
    } catch (e) {
      if (e instanceof MemoryApiError && e.status === 404) {
        this.loadState.set('not-found');
      } else {
        this.loadState.set('error');
      }
    }
  }

  async onDeleteAnalysis(a: Analysis): Promise<void> {
    if (!window.confirm(`Are you sure you want to delete analysis "${a.title}"? This cannot be undone.`)) {
      return;
    }
    try {
      await this.memory.deleteAnalysis(a.id);
      void this.router.navigate(['/settings/memory']);
    } catch (e) {
      alert('Failed to delete analysis.');
      console.error(e);
    }
  }
}
