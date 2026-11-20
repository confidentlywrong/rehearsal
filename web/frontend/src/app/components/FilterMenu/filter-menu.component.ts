/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: validate restored localStorage filter indices, cancel pending animation timers on destroy, add ARIA button states, and standardize icon classes.
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
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  OnDestroy,
  OnInit,
  effect,
  inject,
} from '@angular/core';
import { DemoService } from '../DemoOverlay/demo.service';
import { DEMO_CONFIG } from '../../demo-config';
import { DemoId } from '../../../constants';

interface FilterItem {
  id: string;
  icon: string;
  active: boolean;
  alwaysActive?: boolean;
  options: string[];
  optionIndex: number;
  prevLabel: string;
  animating: boolean;
}

const STORAGE_KEY = 'filter-menu-state';

@Component({
  selector: 'app-filter-menu',
  standalone: true,
  imports: [],
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrls: ['./filter-menu.scss'],
  template: `
    <div class="filter-menu" [class.open]="open">
      @for (item of filters; track item.id) {
        @if (item.id === 'camera') {
          <div class="filter-row filter-row--camera">
            <button
              type="button"
              class="filter-item active"
              aria-label="Camera presets"
              [attr.aria-pressed]="true"
            >
              <span class="material-symbols-outlined" aria-hidden="true">{{ item.icon }}</span>
            </button>
            <div class="camera-subnav">
              <div class="camera-blocker"></div>
              @for (option of item.options; track option; let i = $index) {
                <button
                  type="button"
                  class="camera-option"
                  [class.selected]="item.optionIndex === i"
                  [attr.aria-pressed]="item.optionIndex === i"
                  (click)="selectCamera(item, i)"
                >
                  <span class="material-symbols-outlined" aria-hidden="true">videocam</span>
                  <span class="camera-option-letter">{{ option.slice(-1) }}</span>
                </button>
              }
            </div>
          </div>
        } @else {
          <div class="filter-row">
            <button
              type="button"
              class="filter-item"
              [class.active]="item.active"
              [attr.aria-pressed]="item.active"
              [attr.aria-label]="item.options[item.optionIndex]"
              (click)="toggleFilter(item)"
            >
              <span class="material-symbols-outlined" aria-hidden="true">{{ item.icon }}</span>
            </button>
            <span class="filter-label">
              <span class="label-scroll" [class.animating]="item.animating">
                <span class="label-old">{{ item.prevLabel }}</span>
                <span class="label-current">{{ item.options[item.optionIndex] }}</span>
              </span>
            </span>
          </div>
        }
        @if (item.id === 'race-support' || item.id === 'panning') {
          <div class="filter-spacer"></div>
        }
      }

      <button
        type="button"
        class="toggle-btn"
        (click)="open = !open"
        aria-label="Toggle filter menu"
        [attr.aria-expanded]="open"
      >
        <span class="material-icons" aria-hidden="true">{{
          open ? 'unfold_less' : 'unfold_more'
        }}</span>
      </button>
    </div>
  `,
})
export class FilterMenuComponent implements OnInit, OnDestroy {
  open = false;
  demoService = inject(DemoService);
  private pendingRafId: number | null = null;
  private pendingAnimTimer: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;

  filters: FilterItem[] = [
    {
      id: 'entertainment',
      icon: 'favorite',
      active: false,
      options: ['Spectators'],
      optionIndex: 0,
      prevLabel: '',
      animating: false,
    },
    // { id: 'sentiment', icon: 'chat', active: true, options: ['Sentiment'], optionIndex: 0, prevLabel: '', animating: false },
    // { id: 'race-info', icon: 'sports_score', active: true, options: ['Race info'], optionIndex: 0, prevLabel: '', animating: false },
    {
      id: 'race-support',
      icon: 'health_and_safety',
      active: false,
      options: ['Race Support'],
      optionIndex: 0,
      prevLabel: '',
      animating: false,
    },
    {
      id: 'camera',
      icon: 'videocam',
      active: true,
      alwaysActive: true,
      options: ['Camera A', 'Camera B', 'Camera C'],
      optionIndex: 0,
      prevLabel: '',
      animating: false,
    },
    {
      id: 'panning',
      icon: 'autoplay',
      options: ['Orbit On', 'Orbit Off'],
      prevLabel: '',
      animating: false,
      ...this.orbitPanningFromActiveDemo(),
    },
  ];

  constructor(private cdr: ChangeDetectorRef) {
    effect(() => {
      const { active, optionIndex } = this.orbitPanningFromDemoId(this.demoService.activeDemo());
      const panning = this.filters.find((f) => f.id === 'panning');
      if (!panning) return;
      if (panning.optionIndex === optionIndex && panning.active === active) return;
      panning.optionIndex = optionIndex;
      panning.active = active;
      window.dispatchEvent(
        new CustomEvent('filter:changed', {
          detail: { id: 'panning', value: panning.options[optionIndex], index: optionIndex },
        }),
      );
      this.cdr.markForCheck();
    });
  }

  private orbitPanningFromDemoId(demoId: DemoId): { active: boolean; optionIndex: number } {
    const on = !!(DEMO_CONFIG[demoId] as { orbitCamera?: boolean }).orbitCamera;
    return { active: on, optionIndex: on ? 0 : 1 };
  }

  private orbitPanningFromActiveDemo(): { active: boolean; optionIndex: number } {
    return this.orbitPanningFromDemoId(this.demoService.activeDemo());
  }

  ngOnInit(): void {
    this.loadState();
    for (const item of this.filters) {
      if (item.options.length > 1) {
        window.dispatchEvent(
          new CustomEvent('filter:changed', {
            detail: { id: item.id, value: item.options[item.optionIndex], index: item.optionIndex },
          }),
        );
      } else {
        window.dispatchEvent(
          new CustomEvent('filter:changed', {
            detail: { id: item.id, active: item.active },
          }),
        );
      }
    }
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    if (this.pendingRafId !== null) {
      cancelAnimationFrame(this.pendingRafId);
      this.pendingRafId = null;
    }
    if (this.pendingAnimTimer !== null) {
      clearTimeout(this.pendingAnimTimer);
      this.pendingAnimTimer = null;
    }
  }

  private saveState(): void {
    try {
      const state: Record<string, { active: boolean; optionIndex: number }> = {};
      for (const item of this.filters) {
        state[item.id] = { active: item.active, optionIndex: item.optionIndex };
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // ignore storage quota/privacy errors
    }
  }

  private loadState(): void {
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(STORAGE_KEY);
    } catch {
      return;
    }
    if (!raw) return;
    try {
      const state = JSON.parse(raw) as Record<string, { active?: unknown; optionIndex?: unknown }>;
      if (!state || typeof state !== 'object') return;
      for (const item of this.filters) {
        if (item.id === 'panning') continue;
        if (item.id === 'camera') continue;
        const saved = state[item.id];
        if (!saved || typeof saved !== 'object') continue;
        item.active = item.alwaysActive ? true : Boolean(saved.active);
        const rawIdx = Number(saved.optionIndex);
        item.optionIndex =
          Number.isInteger(rawIdx) && rawIdx >= 0
            ? Math.min(rawIdx, Math.max(0, item.options.length - 1))
            : 0;
      }
    } catch {
      // ignore corrupt storage
    }
  }

  selectCamera(item: FilterItem, index: number): void {
    item.optionIndex = index;
    window.dispatchEvent(
      new CustomEvent('filter:changed', {
        detail: { id: item.id, value: item.options[index], index },
      }),
    );
    this.saveState();
    this.cdr.markForCheck();
  }

  toggleFilter(item: FilterItem): void {
    if (item.id === 'camera') return;
    if (item.options.length > 1) {
      item.animating = false;
      this.cdr.markForCheck();

      if (this.pendingRafId !== null) {
        cancelAnimationFrame(this.pendingRafId);
      }
      this.pendingRafId = requestAnimationFrame(() => {
        this.pendingRafId = null;
        if (this.destroyed) return;
        item.prevLabel = item.options[item.optionIndex];
        item.optionIndex = (item.optionIndex + 1) % item.options.length;
        if (!item.alwaysActive) {
          item.active = item.optionIndex === 0;
        }
        item.animating = true;
        this.cdr.markForCheck();

        window.dispatchEvent(
          new CustomEvent('filter:changed', {
            detail: { id: item.id, value: item.options[item.optionIndex], index: item.optionIndex },
          }),
        );
        this.saveState();

        if (this.pendingAnimTimer !== null) {
          clearTimeout(this.pendingAnimTimer);
        }
        this.pendingAnimTimer = setTimeout(() => {
          this.pendingAnimTimer = null;
          if (this.destroyed) return;
          item.animating = false;
          this.cdr.markForCheck();
        }, 300);
      });
      return;
    } else {
      item.active = !item.active;
      window.dispatchEvent(
        new CustomEvent('filter:changed', {
          detail: { id: item.id, active: item.active },
        }),
      );
    }

    this.saveState();
    this.cdr.markForCheck();
  }
}
