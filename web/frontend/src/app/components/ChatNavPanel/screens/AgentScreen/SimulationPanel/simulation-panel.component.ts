/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: guard NaN/non-finite progress, distance, and finisher metrics, add configurable simulation timing inputs, and add button types/accessibility labels in SimulationPanelComponent.
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
  Component,
  Input,
  Output,
  EventEmitter,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { CountUpDirective } from 'ngx-countup';
import {
  HARDCODED_SIM_MAX_TICKS,
  HARDCODED_SIM_TICK_INTERVAL_SEC,
  HARDCODED_SIM_TOTAL_RACE_HOURS,
} from '../../../../../runner-sim-constants';

@Component({
  selector: 'simulation-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, CountUpDirective],
  styleUrls: ['./simulation-panel.scss'],
  template: `
    <div class="sim-panel-wrapper">
      <div class="simulation-running-panel">
        <div class="top-bar">
          <h1>Simulation progress</h1>
          <button
            type="button"
            class="expand-btn sim"
            aria-label="Expand simulation panel"
            (click)="expand.emit()"
          >
            <span class="material-icons" aria-hidden="true">unfold_more</span>
          </button>
        </div>
        <div class="progress-container" [style.--progress]="safeProgress">
          <p class="time">
            <span class="time-part">
              <span class="time-num" [countUp]="hours"></span>
              <span class="time-unit">{{ hoursUnit }}</span></span
            >
            <span class="time-part">
              @for (hourKey of [hours]; track hourKey) {
                <span class="time-num" [countUp]="mins"></span>
              }
              <span class="time-unit">mins</span></span
            >
          </p>
        </div>

        <div class="distance">
          <div class="stat">
            <p class="value">{{ safeAverageDistance | number : '1.1-1' }}</p>
            <p class="label">AVG. DECISIONS <span>(rounds)</span></p>
          </div>
        </div>
        <div class="statistics">
          <div class="stat">
            <p class="value" [countUp]="safeFinishers"></p>
            <p class="label">ALIGNED STAKEHOLDERS</p>
          </div>
          <div class="stat">
            <p class="value">{{ averagePace }}</p>
            <p class="label">AVG. ALIGNMENT <span>(%)</span></p>
          </div>
        </div>
        <div class="button-container">
          <button
            type="button"
            [disabled]="isFollowingLeader"
            (click)="followLeader.emit()"
            class="followButton"
          >
            Follow the leader
          </button>
          <button
            type="button"
            class="followButton"
            (click)="followRandomRunner.emit()"
          >
            I'm feeling lucky
          </button>
        </div>
      </div>
    </div>
  `,
})
export class SimulationPanelComponent {
  @Input() simulationProgress = 0;
  @Input() averageDistance: number | string = 0;
  @Input() numberOfFinishers = 0;
  @Input() averagePace = '0:00';
  @Input() isFollowingLeader = false;
  @Input() maxTicks = HARDCODED_SIM_MAX_TICKS;
  @Input() tickIntervalSec = HARDCODED_SIM_TICK_INTERVAL_SEC;
  @Input() totalSimHours = HARDCODED_SIM_TOTAL_RACE_HOURS;

  @Output() followLeader = new EventEmitter<void>();
  @Output() followRandomRunner = new EventEmitter<void>();
  @Output() expand = new EventEmitter<void>();

  get safeProgress(): number {
    return typeof this.simulationProgress === 'number' && Number.isFinite(this.simulationProgress)
      ? Math.max(0, Math.min(100, this.simulationProgress))
      : 0;
  }

  get safeAverageDistance(): number {
    const val =
      typeof this.averageDistance === 'number'
        ? this.averageDistance
        : Number(this.averageDistance);
    return Number.isFinite(val) ? Math.max(0, val) : 0;
  }

  get safeFinishers(): number {
    return typeof this.numberOfFinishers === 'number' && Number.isFinite(this.numberOfFinishers)
      ? Math.max(0, Math.floor(this.numberOfFinishers))
      : 0;
  }

  private getSimulationElapsedTimeParts(): { hours: number; mins: number } {
    const ticks = typeof this.maxTicks === 'number' && Number.isFinite(this.maxTicks) && this.maxTicks > 0
      ? this.maxTicks
      : HARDCODED_SIM_MAX_TICKS;
    const interval = typeof this.tickIntervalSec === 'number' && Number.isFinite(this.tickIntervalSec) && this.tickIntervalSec > 0
      ? this.tickIntervalSec
      : HARDCODED_SIM_TICK_INTERVAL_SEC;
    const simHours = typeof this.totalSimHours === 'number' && Number.isFinite(this.totalSimHours) && this.totalSimHours > 0
      ? this.totalSimHours
      : HARDCODED_SIM_TOTAL_RACE_HOURS;
    const wallSec = ticks * interval;
    if (!Number.isFinite(wallSec) || wallSec <= 0) {
      return { hours: 0, mins: 0 };
    }
    const elapsedWallSec = (this.safeProgress / 100) * wallSec;
    const total = Math.max(
      0,
      Math.floor((elapsedWallSec / wallSec) * simHours * 60),
    );
    return {
      hours: Math.floor(total / 60),
      mins: total % 60,
    };
  }

  get hours(): number {
    return this.getSimulationElapsedTimeParts().hours;
  }

  get mins(): number {
    return this.getSimulationElapsedTimeParts().mins;
  }

  get hoursUnit(): string {
    return this.hours === 1 ? 'hr' : 'hrs';
  }

  get simulationRealTimeLabel(): string {
    const { hours, mins } = this.getSimulationElapsedTimeParts();
    return `${hours} hours ${mins} minutes`;
  }
}
