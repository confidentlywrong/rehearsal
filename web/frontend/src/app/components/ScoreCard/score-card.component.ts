/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: refactor ScoreCardComponent to bind real route/score inputs and outputs with honest empty states instead of synthetic defaults.
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

import { CommonModule } from '@angular/common';
import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  OnInit,
  Output,
  SimpleChanges,
} from '@angular/core';

@Component({
  selector: 'app-score-card',
  standalone: true,
  imports: [CommonModule],
  styleUrls: ['./ScoreCard.scss'],
  template: `
    <div class="scorecard" [class.simulation]="!plan" [class.current]="current">
      <div class="preview">
        <div class="information">
          <div class="top-row" *ngIf="type || version || date || timestamp">
            <span class="type" *ngIf="type">{{ type }}</span>
            <span *ngIf="version">{{ version }}</span>
            <span *ngIf="date">{{ date }}</span>
            <span *ngIf="timestamp">{{ timestamp }}</span>
          </div>
          <h2>{{ title }}</h2>
        </div>
        <div class="score">
          <span class="value">{{ displayScore }}</span>
          <span class="label">Score</span>
        </div>
      </div>
      <div class="actions-container">
        <button type="button" (click)="onShowScoreCard()">
          <span class="material-icons unfold" aria-hidden="true">unfold_more</span>
          <p>{{ isOpen ? 'Hide score card' : 'Show score card' }}</p>
        </button>
        <ng-container *ngIf="!plan; else play">
          <button type="button" (click)="onRunSimulation($event)">
            <span class="material-icons" aria-hidden="true">play_arrow</span>
            <p>Run simulation</p>
          </button>
        </ng-container>
        <ng-template #play>
          <button type="button" (click)="onShowRoute()">
            <span class="material-icons" aria-hidden="true">arrow_forward</span>
            <p>Show route</p>
          </button>
        </ng-template>
      </div>
    </div>
  `,
})
export class ScoreCardComponent implements OnInit, OnChanges {
  @Input() route: any;
  @Input() score: number | string | null = null;
  @Input() titleText?: string;
  @Input() cardType?: 'PLAN' | 'SIMULATED';
  @Input() versionText?: string;
  @Input() dateText?: string;
  @Input() timestampText?: string;
  @Input() isCurrent?: boolean;
  @Input() isPlan?: boolean;

  @Output() showScoreCard = new EventEmitter<unknown>();
  @Output() runSimulation = new EventEmitter<unknown>();
  @Output() historyClick = new EventEmitter<unknown>();

  protected isOpen = false;
  protected timestamp = '';
  protected date = '';
  protected version = '';
  protected type: 'PLAN' | 'SIMULATED' | null = null;
  protected title = '—';
  protected displayScore = '—';

  protected current = false;
  protected plan = false;

  ngOnInit(): void {
    this.syncFromInputs();
  }

  ngOnChanges(_changes: SimpleChanges): void {
    this.syncFromInputs();
  }

  private syncFromInputs(): void {
    const rawName = this.titleText ?? this.route?.name ?? this.route?.title;
    this.title = typeof rawName === 'string' && rawName.trim() ? rawName.trim() : '—';

    const rawScore = this.score ?? this.route?.score ?? this.route?.eval_score;
    if (typeof rawScore === 'number' && Number.isFinite(rawScore)) {
      this.displayScore = String(rawScore);
    } else if (typeof rawScore === 'string' && rawScore.trim()) {
      this.displayScore = rawScore.trim();
    } else {
      this.displayScore = '—';
    }

    const rawType = this.cardType ?? this.route?.type;
    this.type = rawType === 'PLAN' || rawType === 'SIMULATED' ? rawType : null;

    const rawVersion = this.versionText ?? this.route?.version;
    this.version = typeof rawVersion === 'string' ? rawVersion.trim() : '';

    const rawDate = this.dateText ?? this.route?.date;
    this.date = typeof rawDate === 'string' ? rawDate.trim() : '';

    const rawTimestamp = this.timestampText ?? this.route?.timestamp;
    this.timestamp = typeof rawTimestamp === 'string' ? rawTimestamp.trim() : '';

    this.current = Boolean(this.isCurrent ?? this.route?.current ?? false);
    this.plan = Boolean(this.isPlan ?? (this.type ? this.type === 'PLAN' : this.route?.is_plan ?? true));
  }

  onShowScoreCard(): void {
    this.isOpen = !this.isOpen;
    this.showScoreCard.emit(this.route);
  }

  onRunSimulation(e: any): void {
    e?.stopPropagation?.();
    this.runSimulation.emit(this.route);
  }

  onShowRoute(): void {
    if (!this.route?.route_data) return;
    window.dispatchEvent(
      new CustomEvent('gateway:routeGeojson', { detail: { geojson: this.route.route_data } }),
    );
  }

  onHistoryClick(): void {
    this.historyClick.emit(this.route);
  }
}
