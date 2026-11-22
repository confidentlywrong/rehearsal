/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: migrate floating viewport labels to Obsidian Light and remove static decorative dot.
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
import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

export interface LabelEntry {
  id: number;
  x: number;
  y: number;
  progress: number;
  selected: boolean;
  hovered: boolean;
  visible: boolean;
  status: 'Moving' | 'Paused';
  name?: string; // set for landmark labels; omitted for runner labels
}

@Component({
  selector: 'app-label',
  standalone: true,
  imports: [CommonModule],
  template: `
    <ng-container *ngFor="let label of labels">
      <div
        *ngIf="label.name"
        class="label landmark"
        [class.selected]="label.selected"
        [class.hovered]="label.hovered"
        [class.show]="label.visible"
        [style.left.px]="label.x"
        [style.top.px]="label.y"
      >
        <span class="lm-name">{{ label.name }}</span>
      </div>
      <button
        *ngIf="!label.name"
        type="button"
        class="label"
        [class.selected]="label.selected"
        [class.hovered]="label.hovered"
        [class.show]="label.visible"
        [style.left.px]="label.x"
        [style.top.px]="label.y"
        (click)="onLabelClick(label.id)"
      >
        <span class="r-name">Runner {{ label.id }}</span>
        <span class="r-prog">{{ label.progress.toFixed(1) }}%</span>
      </button>
    </ng-container>
  `,
  styles: [`
    /* ── Base label (runner) ─────────────────────────────────────── */
    .label {
      appearance: none;
      position: fixed;
      transform: translate(-50%, -100%);
      display: flex;
      align-items: center;
      gap: 6px;
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(18px) saturate(1.8);
      -webkit-backdrop-filter: blur(18px) saturate(1.8);
      border: 1px solid #E5E5EA;
      border-radius: 9px;
      padding: 5px 11px;
      font-family: 'SF Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
      color: #1D1D1F;
      white-space: nowrap;
      pointer-events: none;
      opacity: 0;
      cursor: pointer;
      transition: opacity 0.18s ease, border-color 0.15s ease;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
    }
    .label.show { opacity: 1; pointer-events: auto; }
    .label.selected {
      border-color: #34C759;
      box-shadow: 0 4px 16px rgba(52, 199, 89, 0.18);
    }
    .label.hovered {
      border-color: #0071E3;
    }
    .label:focus-visible {
      outline: 2px solid #0071E3;
      outline-offset: 2px;
    }

    /* ── Landmark chip ─────────────────────────────────────────────── */
    .label.landmark {
      transform: translate(-50%, calc(-100% - 22px));
      background: rgba(255, 255, 255, 0.92);
      border-radius: 10px;
      padding: 5px 11px;
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'SF Pro Display', system-ui, sans-serif;
      pointer-events: none;
      cursor: default;
      box-shadow: 0 4px 18px rgba(0, 0, 0, 0.1);
    }
    .label.landmark.show { pointer-events: none; }

    .lm-name {
      color: #1D1D1F;
      font-size: 11.5px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }
    .r-name { color: #1D1D1F; font-weight: 500; }
    .r-prog { color: #6E6E73; font-size: 10px; }
  `]
})
export class LabelComponent {
  @Input() labels: LabelEntry[] = [];
  onLabelClick(id: number) {
    window.dispatchEvent(new CustomEvent('runner:select', { detail: { id } }));
  }
}
