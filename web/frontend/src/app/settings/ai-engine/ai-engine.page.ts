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

import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import {
  AiEngineService,
  AiEngineStatus,
  AiProvider,
  ModelTier,
} from './ai-engine.service';

@Component({
  selector: 'app-ai-engine-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule],
  template: `
    <section class="ai-page">
      <header class="ai-page__header">
        <div>
          <h2 class="ai-page__title">AI Engine &amp; Local Google Models</h2>
          <p class="ai-page__subtitle">
            Switch between Cloud Google Gemini (Vertex AI) and 100% local execution
            powered by Google Gemma text models and Google EmbeddingGemma vector embeddings.
          </p>
        </div>
      </header>

      <div *ngIf="loading" class="ai-card ai-card--muted">
        Detecting local hardware profile, Ollama daemon, and installed Google model manifests…
      </div>

      <div *ngIf="errorMessage" class="ai-banner ai-banner--error" role="alert">
        {{ errorMessage }}
      </div>

      <div *ngIf="toastMessage" class="ai-banner ai-banner--ok" role="status">
        {{ toastMessage }}
      </div>

      <ng-container *ngIf="status as st">
        <!-- 1. Provider Selector -->
        <div class="ai-card">
          <div class="ai-card__label">Active AI Provider</div>
          <div class="ai-provider-grid" role="radiogroup" aria-label="AI Provider">
            <button
              type="button"
              class="ai-provider-option"
              [class.ai-provider-option--active]="provider === 'cloud_gemini'"
              (click)="selectProvider('cloud_gemini')"
              data-testid="provider-cloud-gemini"
            >
              <div class="ai-provider-option__top">
                <span class="ai-provider-option__title">Cloud Google Gemini</span>
                <span
                  class="ai-badge"
                  [class.ai-badge--ok]="provider === 'cloud_gemini'"
                >
                  {{ provider === 'cloud_gemini' ? '✓ Active' : 'Cloud' }}
                </span>
              </div>
              <p class="ai-provider-option__desc">
                Uses Vertex AI Gemini 3 Flash / Pro and <code>gemini-embedding-001</code> (3072-d).
              </p>
            </button>

            <button
              type="button"
              class="ai-provider-option"
              [class.ai-provider-option--active]="provider === 'local_gemma'"
              (click)="selectProvider('local_gemma')"
              data-testid="provider-local-gemma"
            >
              <div class="ai-provider-option__top">
                <span class="ai-provider-option__title">Local Google LLM (Gemma)</span>
                <span
                  class="ai-badge"
                  [class.ai-badge--ok]="provider === 'local_gemma'"
                >
                  {{ provider === 'local_gemma' ? '✓ Active' : '100% Local' }}
                </span>
              </div>
              <p class="ai-provider-option__desc">
                Runs Google <code>{{ textModel || 'gemma2:9b' }}</code> + <code>{{ embedModel || 'embeddinggemma:latest' }}</code> locally via Ollama on your Mac/PC.
              </p>
            </button>
          </div>
        </div>

        <!-- 2. Hardware & Local Daemon Status Card -->
        <div class="ai-card">
          <div class="ai-card__header-row">
            <div>
              <div class="ai-card__label">Local Runtime &amp; Hardware Calibration</div>
              <div class="ai-card__meta">
                {{ st.hardware.chip }} · {{ st.hardware.total_ram_gb }} GB Unified Memory ·
                Recommended: <code>{{ st.hardware.recommended_model }}</code>
              </div>
            </div>

            <div class="ai-status-cluster">
              <span
                *ngIf="st.connected"
                class="ai-badge ai-badge--ok"
                data-testid="daemon-connected-badge"
              >
                ● Engine Online ({{ st.latency_ms ?? 1 }} ms)
              </span>
              <span
                *ngIf="!st.connected && st.binary_installed"
                class="ai-badge ai-badge--warn"
                data-testid="daemon-stopped-badge"
              >
                ○ Daemon Stopped (Binary Installed)
              </span>
              <span
                *ngIf="!st.connected && !st.binary_installed"
                class="ai-badge ai-badge--missing"
                data-testid="daemon-missing-badge"
              >
                ✕ Ollama Not Installed
              </span>
            </div>
          </div>

          <!-- Missing Binary Guidance -->
          <div
            *ngIf="!st.connected && !st.binary_installed"
            class="ai-callout ai-callout--warn"
            data-testid="ollama-install-callout"
          >
            <div class="ai-callout__title">Ollama runtime not detected on this machine</div>
            <p class="ai-callout__text">
              Install Ollama via <code>brew install --cask ollama</code> or from
              <a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com/download</a>,
              then click <strong>Check Connection</strong>.
            </p>
          </div>

          <!-- Stopped Daemon Callout -->
          <div
            *ngIf="!st.connected && st.binary_installed"
            class="ai-callout ai-callout--info"
            data-testid="ollama-start-callout"
          >
            <div class="ai-callout__row">
              <div>
                <div class="ai-callout__title">Local Ollama binary found ({{ st.binary_path }})</div>
                <p class="ai-callout__text">
                  On-disk models were detected automatically. Start the local engine to run simulations offline.
                </p>
              </div>
              <button
                type="button"
                class="ai-btn ai-btn--primary"
                [disabled]="startingDaemon"
                (click)="onStartDaemon()"
                data-testid="start-daemon-btn"
              >
                {{ startingDaemon ? 'Starting…' : 'Start Local Engine' }}
              </button>
            </div>
          </div>

          <!-- Endpoint Input + Test Button -->
          <div class="ai-endpoint-row">
            <label class="ai-field">
              <span class="ai-field__label">Local Loopback Endpoint</span>
              <input
                type="text"
                class="ai-input"
                [(ngModel)]="endpoint"
                placeholder="http://localhost:11434"
                data-testid="endpoint-input"
              />
            </label>
            <button
              type="button"
              class="ai-btn ai-btn--ghost"
              [disabled]="testingConnection"
              (click)="onTestConnection()"
              data-testid="test-connection-btn"
            >
              {{ testingConnection ? 'Checking…' : 'Check Connection' }}
            </button>
          </div>
        </div>

        <!-- Live Pull Progress Banner -->
        <div
          *ngIf="st.pull_progress.active"
          class="ai-card ai-card--progress"
          data-testid="pull-progress-card"
        >
          <div class="ai-progress__header">
            <span>
              Downloading <code>{{ st.pull_progress.model }}</code> —
              {{ st.pull_progress.status || 'pulling layers' }}
            </span>
            <strong>{{ st.pull_progress.percent | number: '1.0-1' }}%</strong>
          </div>
          <div class="ai-progress__track">
            <div
              class="ai-progress__fill"
              [style.width.%]="st.pull_progress.percent"
            ></div>
          </div>
        </div>

        <!-- 3. Google Gemma Text & Agent Model Tiers -->
        <div class="ai-card">
          <div class="ai-card__label">1. Google Gemma Reasoning &amp; Simulation Model</div>
          <p class="ai-card__hint">
            Powers the Planner, Stakeholder Simulator, Evaluation Judge, and Coaching agents.
            Models already present in <code>~/.ollama/models</code> are marked <strong>Installed</strong>.
          </p>

          <div class="ai-tier-list">
            <div
              *ngFor="let tier of st.model_tiers"
              class="ai-tier"
              [class.ai-tier--selected]="textModel === tier.model"
              [attr.data-testid]="'tier-' + tier.id"
            >
              <div class="ai-tier__info">
                <div class="ai-tier__title-row">
                  <span class="ai-tier__name">{{ tier.label }}</span>
                  <code class="ai-tier__code">{{ tier.model }}</code>
                  <span
                    *ngIf="tier.installed && textModel === tier.model && provider === 'local_gemma'"
                    class="ai-badge ai-badge--ok"
                  >
                    ✓ Active
                  </span>
                  <span
                    *ngIf="tier.installed && !(textModel === tier.model && provider === 'local_gemma')"
                    class="ai-badge ai-badge--installed"
                  >
                    ✓ Installed ({{ tier.size_gb }} GB)
                  </span>
                  <span
                    *ngIf="!tier.installed"
                    class="ai-badge ai-badge--missing"
                  >
                    Not Installed · {{ tier.size_gb }} GB
                  </span>
                  <span *ngIf="!tier.fits_ram" class="ai-badge ai-badge--warn">
                    Requires {{ tier.min_ram_gb }} GB RAM
                  </span>
                </div>
                <div class="ai-tier__subtitle">{{ tier.subtitle }}</div>
              </div>

              <div class="ai-tier__actions">
                <button
                  *ngIf="tier.installed"
                  type="button"
                  class="ai-btn"
                  [class.ai-btn--primary]="textModel === tier.model"
                  [class.ai-btn--ghost]="textModel !== tier.model"
                  (click)="selectTierModel(tier)"
                  [attr.data-testid]="'select-tier-' + tier.id"
                >
                  {{ textModel === tier.model ? 'Selected' : 'Select Model' }}
                </button>
                <button
                  *ngIf="!tier.installed"
                  type="button"
                  class="ai-btn ai-btn--ghost"
                  [disabled]="pullingModel === tier.model || st.pull_progress.active"
                  (click)="onPullModel(tier.model)"
                  [attr.data-testid]="'pull-tier-' + tier.id"
                >
                  {{ pullingModel === tier.model ? 'Starting Download…' : 'Download (' + tier.size_gb + ' GB)' }}
                </button>
              </div>
            </div>
          </div>

          <div class="ai-custom-row">
            <label class="ai-field">
              <span class="ai-field__label">Selected / Custom Gemma Tag (e.g. gemma2:9b, gemma3:4b, gemma3:12b)</span>
              <input
                type="text"
                class="ai-input"
                [(ngModel)]="textModel"
                placeholder="gemma2:9b"
                data-testid="text-model-input"
              />
            </label>
            <div class="ai-custom-actions">
              <span
                *ngIf="isCurrentTextModelInstalled()"
                class="ai-badge ai-badge--installed"
                data-testid="custom-model-installed-badge"
              >
                ✓ Tag Installed
              </span>
              <button
                *ngIf="!isCurrentTextModelInstalled() && textModel.trim()"
                type="button"
                class="ai-btn ai-btn--ghost"
                [disabled]="pullingModel === textModel.trim() || st.pull_progress.active"
                (click)="onPullModel(textModel.trim())"
                data-testid="pull-custom-model-btn"
              >
                {{ pullingModel === textModel.trim() ? 'Downloading…' : 'Download ' + textModel.trim() }}
              </button>
              <button
                *ngIf="!isCurrentTextModelInstalled() || !st.embed_engine.installed"
                type="button"
                class="ai-btn ai-btn--primary"
                [disabled]="pullingModel === 'bundle' || st.pull_progress.active"
                (click)="onPullModel('bundle')"
                data-testid="pull-bundle-btn"
              >
                {{ pullingModel === 'bundle' ? 'Downloading Bundle…' : 'Download Missing Models' }}
              </button>
            </div>
          </div>
        </div>

        <!-- 4. Local Google Vector Embeddings Engine (embeddinggemma:latest) -->
        <div class="ai-card" data-testid="embed-engine-card">
          <div class="ai-card__header-row">
            <div>
              <div class="ai-card__label">2. Local Google Vector Embeddings Engine</div>
              <div class="ai-card__meta">
                Powers Organizational Memory RAG &amp; document chunk retrieval without Vertex AI calls.
              </div>
            </div>
            <span
              *ngIf="st.embed_engine.installed"
              class="ai-badge ai-badge--ok"
              data-testid="embed-installed-badge"
            >
              ✓ Installed ({{ st.embed_engine.size_gb }} GB)
            </span>
            <span
              *ngIf="!st.embed_engine.installed"
              class="ai-badge ai-badge--missing"
              data-testid="embed-missing-badge"
            >
              Not Installed · {{ st.embed_engine.size_gb }} GB
            </span>
          </div>

          <div class="ai-tier ai-tier--embed">
            <div class="ai-tier__info">
              <div class="ai-tier__title-row">
                <span class="ai-tier__name">{{ st.embed_engine.label }}</span>
                <code class="ai-tier__code">{{ embedModel }}</code>
              </div>
              <div class="ai-tier__subtitle">
                {{ st.embed_engine.subtitle }} ({{ st.embed_engine.native_dim }} native dims →
                L2-normalized &amp; padded to {{ st.embed_engine.pgvector_dim }} dims for pgvector)
              </div>
            </div>

            <div class="ai-tier__actions">
              <button
                *ngIf="!st.embed_engine.installed"
                type="button"
                class="ai-btn ai-btn--primary"
                [disabled]="pullingModel === embedModel || st.pull_progress.active"
                (click)="onPullModel(embedModel)"
                data-testid="pull-embed-model-btn"
              >
                {{ pullingModel === embedModel ? 'Downloading…' : 'Download EmbeddingGemma' }}
              </button>
              <span *ngIf="st.embed_engine.installed" class="ai-ready-pill">
                Ready for Local RAG
              </span>
            </div>
          </div>
        </div>

        <!-- 5. Save Footer -->
        <div class="ai-actions-footer">
          <button
            type="button"
            class="ai-btn ai-btn--primary ai-btn--large"
            [disabled]="saving"
            (click)="onSave()"
            data-testid="save-ai-settings-btn"
          >
            {{ saving ? 'Saving Settings…' : 'Save AI Engine Settings' }}
          </button>
        </div>
      </ng-container>
    </section>
  `,
  styles: [
    `
      .ai-page {
        display: flex;
        flex-direction: column;
        gap: 18px;
        color: #e8f8ff;
      }
      .ai-page__title {
        margin: 0 0 6px 0;
        font-size: 20px;
        font-weight: 600;
      }
      .ai-page__subtitle {
        margin: 0;
        font-size: 13px;
        color: rgba(232, 248, 255, 0.72);
        line-height: 1.5;
      }
      .ai-card {
        background: rgba(13, 25, 38, 0.85);
        border: 1px solid rgba(0, 255, 200, 0.16);
        border-radius: 10px;
        padding: 18px 20px;
        display: flex;
        flex-direction: column;
        gap: 14px;
      }
      .ai-card--muted {
        color: rgba(232, 248, 255, 0.7);
        font-size: 13px;
      }
      .ai-card--progress {
        border-color: rgba(0, 255, 200, 0.45);
      }
      .ai-card__header-row {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .ai-card__label {
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.03em;
        text-transform: uppercase;
        color: #00ffcc;
      }
      .ai-card__meta,
      .ai-card__hint {
        margin: 4px 0 0 0;
        font-size: 12px;
        color: rgba(232, 248, 255, 0.72);
      }
      .ai-provider-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 12px;
      }
      .ai-provider-option {
        text-align: left;
        background: rgba(7, 17, 26, 0.75);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 8px;
        padding: 14px;
        color: #e8f8ff;
        cursor: pointer;
        transition: border-color 0.15s ease, background 0.15s ease;
      }
      .ai-provider-option:hover {
        border-color: rgba(0, 255, 200, 0.4);
      }
      .ai-provider-option--active {
        border-color: #00ffcc;
        background: rgba(0, 255, 200, 0.08);
      }
      .ai-provider-option__top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        margin-bottom: 6px;
      }
      .ai-provider-option__title {
        font-size: 14px;
        font-weight: 600;
      }
      .ai-provider-option__desc {
        margin: 0;
        font-size: 12px;
        color: rgba(232, 248, 255, 0.72);
        line-height: 1.45;
      }
      .ai-badge {
        display: inline-flex;
        align-items: center;
        padding: 3px 9px;
        border-radius: 999px;
        font-size: 11px;
        font-weight: 600;
        background: rgba(255, 255, 255, 0.08);
        color: #c8d8e6;
      }
      .ai-badge--ok {
        background: rgba(0, 255, 200, 0.16);
        color: #00ffcc;
      }
      .ai-badge--installed {
        background: rgba(74, 168, 255, 0.18);
        color: #7cc4ff;
      }
      .ai-badge--warn {
        background: rgba(255, 196, 0, 0.16);
        color: #ffd666;
      }
      .ai-badge--missing {
        background: rgba(255, 107, 107, 0.15);
        color: #ff9b9b;
      }
      .ai-callout {
        padding: 12px 14px;
        border-radius: 8px;
        font-size: 12px;
      }
      .ai-callout--warn {
        background: rgba(255, 196, 0, 0.08);
        border: 1px solid rgba(255, 196, 0, 0.3);
      }
      .ai-callout--info {
        background: rgba(0, 255, 200, 0.06);
        border: 1px solid rgba(0, 255, 200, 0.25);
      }
      .ai-callout__row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }
      .ai-callout__title {
        font-weight: 600;
        margin-bottom: 4px;
      }
      .ai-callout__text {
        margin: 0;
        color: rgba(232, 248, 255, 0.8);
      }
      .ai-callout__text a {
        color: #00ffcc;
      }
      .ai-endpoint-row,
      .ai-custom-row {
        display: flex;
        align-items: flex-end;
        gap: 10px;
        flex-wrap: wrap;
      }
      .ai-custom-actions {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      }
      .ai-field {
        flex: 1 1 260px;
        display: flex;
        flex-direction: column;
        gap: 5px;
      }
      .ai-field__label {
        font-size: 11px;
        color: rgba(232, 248, 255, 0.72);
      }
      .ai-input {
        padding: 8px 11px;
        border-radius: 6px;
        border: 1px solid rgba(255, 255, 255, 0.18);
        background: rgba(5, 12, 20, 0.9);
        color: #e8f8ff;
        font-size: 13px;
      }
      .ai-tier-list {
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .ai-tier {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 14px;
        border-radius: 8px;
        background: rgba(7, 17, 26, 0.7);
        border: 1px solid rgba(255, 255, 255, 0.09);
        flex-wrap: wrap;
      }
      .ai-tier--selected {
        border-color: rgba(0, 255, 200, 0.55);
        background: rgba(0, 255, 200, 0.06);
      }
      .ai-tier__title-row {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      }
      .ai-tier__name {
        font-size: 13px;
        font-weight: 600;
      }
      .ai-tier__code {
        font-size: 11px;
        padding: 2px 6px;
        border-radius: 4px;
        background: rgba(255, 255, 255, 0.07);
        color: #9be7ff;
      }
      .ai-tier__subtitle {
        font-size: 12px;
        color: rgba(232, 248, 255, 0.68);
        margin-top: 4px;
      }
      .ai-btn {
        padding: 7px 14px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
        border: 1px solid transparent;
        transition: opacity 0.15s ease;
      }
      .ai-btn:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }
      .ai-btn--primary {
        background: #00ffcc;
        color: #04121a;
      }
      .ai-btn--ghost {
        background: rgba(255, 255, 255, 0.06);
        border-color: rgba(255, 255, 255, 0.16);
        color: #e8f8ff;
      }
      .ai-btn--large {
        padding: 10px 20px;
        font-size: 13px;
      }
      .ai-ready-pill {
        font-size: 12px;
        color: #00ffcc;
        font-weight: 600;
      }
      .ai-banner {
        padding: 10px 14px;
        border-radius: 8px;
        font-size: 12px;
      }
      .ai-banner--error {
        background: rgba(255, 90, 90, 0.14);
        border: 1px solid rgba(255, 90, 90, 0.4);
        color: #ffb3b3;
      }
      .ai-banner--ok {
        background: rgba(0, 255, 200, 0.12);
        border: 1px solid rgba(0, 255, 200, 0.35);
        color: #00ffcc;
      }
      .ai-progress__header {
        display: flex;
        justify-content: space-between;
        font-size: 12px;
      }
      .ai-progress__track {
        width: 100%;
        height: 8px;
        background: rgba(255, 255, 255, 0.08);
        border-radius: 999px;
        overflow: hidden;
      }
      .ai-progress__fill {
        height: 100%;
        background: linear-gradient(90deg, #00ffcc, #4aa8ff);
        transition: width 0.25s ease;
      }
      .ai-actions-footer {
        display: flex;
        justify-content: flex-end;
      }
    `,
  ],
})
export class AiEnginePageComponent implements OnInit, OnDestroy {
  status: AiEngineStatus | null = null;
  loading = true;
  saving = false;
  testingConnection = false;
  startingDaemon = false;
  pullingModel: string | null = null;

  provider: AiProvider = 'cloud_gemini';
  endpoint = 'http://localhost:11434';
  textModel = 'gemma2:9b';
  embedModel = 'embeddinggemma:latest';

  errorMessage = '';
  toastMessage = '';

  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private savedEndpoint = 'http://localhost:11434';

  constructor(
    private aiService: AiEngineService,
    private cdr: ChangeDetectorRef,
  ) {}

  async ngOnInit(): Promise<void> {
    await this.loadStatus(true);
  }

  ngOnDestroy(): void {
    this.stopPullPolling();
  }

  async loadStatus(syncForm = false): Promise<void> {
    try {
      const trimmedEndpoint = this.endpoint.trim();
      const customEndpoint =
        !syncForm && trimmedEndpoint && trimmedEndpoint !== this.savedEndpoint
          ? trimmedEndpoint
          : undefined;
      const st = await this.aiService.getStatus(customEndpoint);
      this.applyStatus(st, syncForm);
    } catch (err) {
      this.errorMessage =
        err instanceof Error ? err.message : 'Could not load AI Engine status';
    } finally {
      this.loading = false;
      this.cdr.markForCheck();
    }
  }

  private applyStatus(st: AiEngineStatus, syncForm: boolean): void {
    const wasPolling = this.pollTimer !== null || this.pullingModel !== null;
    const prevPullModel = this.pullingModel;
    this.status = st;
    if (syncForm) {
      this.provider = st.provider;
      this.endpoint = st.endpoint || 'http://localhost:11434';
      this.savedEndpoint = this.endpoint;
      this.textModel = st.text_model || 'gemma2:9b';
      this.embedModel = st.embed_model || 'embeddinggemma:latest';
    }
    if (st.pull_progress?.active) {
      this.startPullPolling();
    } else {
      this.pullingModel = null;
      this.stopPullPolling();
      if (wasPolling && st.pull_progress?.error) {
        this.toastMessage = '';
        this.errorMessage = st.pull_progress.error;
      } else if (wasPolling && st.pull_progress?.done) {
        const finishedModel = st.pull_progress.model || prevPullModel || 'Model';
        this.errorMessage = '';
        this.toastMessage = `Model ${finishedModel} is installed and ready.`;
      }
    }
    this.cdr.markForCheck();
  }

  isCurrentTextModelInstalled(): boolean {
    const st = this.status;
    if (!st) {
      return false;
    }
    const raw = (this.textModel || '')
      .trim()
      .toLowerCase()
      .replace(/^ollama_chat\//, '')
      .replace(/^ollama\//, '');
    if (!raw || raw.includes('embed')) {
      return false;
    }
    const matchingTier = st.model_tiers?.find((t) => t.model.toLowerCase() === raw);
    if (matchingTier) {
      return matchingTier.installed;
    }
    const installed = (st.installed_models || [])
      .map((m) =>
        m
          .trim()
          .toLowerCase()
          .replace(/^ollama_chat\//, '')
          .replace(/^ollama\//, ''),
      )
      .filter((m) => !m.includes('embed'));
    if (installed.includes(raw)) {
      return true;
    }
    if (
      (raw === 'gemma2:9b' && installed.includes('gemma2:latest')) ||
      (raw === 'gemma2:latest' && installed.includes('gemma2:9b')) ||
      (raw === 'gemma3:4b' && installed.includes('gemma3:latest')) ||
      (raw === 'gemma3:latest' && installed.includes('gemma3:4b'))
    ) {
      return true;
    }
    if (!raw.includes(':')) {
      return installed.some((m) => m.split(':')[0] === raw);
    }
    return false;
  }

  selectProvider(p: AiProvider): void {
    this.provider = p;
    this.errorMessage = '';
    this.toastMessage = '';
    this.cdr.markForCheck();
  }

  selectTierModel(tier: ModelTier): void {
    this.textModel = tier.model;
    this.provider = 'local_gemma';
    this.cdr.markForCheck();
  }

  async onTestConnection(): Promise<void> {
    this.testingConnection = true;
    this.errorMessage = '';
    this.toastMessage = '';
    this.cdr.markForCheck();
    try {
      const res = await this.aiService.testConnection(this.endpoint, true);
      this.applyStatus(res.status, false);
      if (res.reachable) {
        this.toastMessage = `Connected to local AI engine at ${res.endpoint} (${res.installed_models.length} model(s) available).`;
      } else {
        this.errorMessage =
          res.error || `Local AI engine at ${res.endpoint} is not reachable.`;
      }
    } catch (err) {
      this.errorMessage =
        err instanceof Error ? err.message : 'Connection test failed';
    } finally {
      this.testingConnection = false;
      this.cdr.markForCheck();
    }
  }

  async onStartDaemon(): Promise<void> {
    this.startingDaemon = true;
    this.errorMessage = '';
    this.toastMessage = '';
    this.cdr.markForCheck();
    try {
      const st = await this.aiService.startDaemon(this.endpoint);
      this.applyStatus(st, false);
      this.toastMessage = 'Local Ollama engine started and ready.';
    } catch (err) {
      this.errorMessage =
        err instanceof Error ? err.message : 'Failed to start local engine';
    } finally {
      this.startingDaemon = false;
      this.cdr.markForCheck();
    }
  }

  async onPullModel(model: string): Promise<void> {
    const trimmedText = (this.textModel || '').trim();
    const effectiveModel =
      model === 'bundle' &&
      trimmedText &&
      trimmedText !== (this.status?.text_model || '')
        ? `bundle:${trimmedText}`
        : model;
    this.pullingModel = model;
    this.errorMessage = '';
    this.toastMessage = '';
    this.cdr.markForCheck();
    try {
      const res = await this.aiService.pullModel(effectiveModel, this.endpoint);
      this.applyStatus(res.status, false);
      if (res.pull_progress.error) {
        this.errorMessage = res.pull_progress.error;
      } else if (res.pull_progress.done) {
        this.toastMessage = `Model ${model} is installed and ready.`;
      } else {
        this.toastMessage = `Started downloading ${model}…`;
      }
    } catch (err) {
      this.pullingModel = null;
      this.errorMessage =
        err instanceof Error ? err.message : `Failed to download ${model}`;
    } finally {
      this.cdr.markForCheck();
    }
  }

  async onSave(): Promise<void> {
    this.saving = true;
    this.errorMessage = '';
    this.toastMessage = '';
    this.cdr.markForCheck();
    try {
      const st = await this.aiService.saveConfig({
        provider: this.provider,
        endpoint: this.endpoint,
        text_model: this.textModel,
        embed_model: this.embedModel,
      });
      this.applyStatus(st, true);
      this.toastMessage =
        st.provider === 'local_gemma'
          ? `Saved: Running locally on Google ${st.text_model} + ${st.embed_model}.`
          : 'Saved: Running on Cloud Google Gemini (Vertex AI).';
    } catch (err) {
      this.errorMessage =
        err instanceof Error ? err.message : 'Failed to save AI Engine settings';
    } finally {
      this.saving = false;
      this.cdr.markForCheck();
    }
  }

  private startPullPolling(): void {
    if (this.pollTimer) {
      return;
    }
    this.pollTimer = setInterval(() => {
      void this.loadStatus(false);
    }, 1500);
  }

  private stopPullPolling(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }
}
