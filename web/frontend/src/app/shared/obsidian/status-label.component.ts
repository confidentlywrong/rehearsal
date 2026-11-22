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

import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

export type ObsidianStatusTone =
  | 'ok'
  | 'warning'
  | 'error'
  | 'neutral'
  | 'muted'
  | 'green'
  | 'amber'
  | 'red';

const TONE_HEX_MAP: Record<ObsidianStatusTone, string> = {
  ok: '#34C759',
  green: '#34C759',
  warning: '#FF9500',
  amber: '#FF9500',
  error: '#FF3B30',
  red: '#FF3B30',
  neutral: '#1D1D1F',
  muted: '#C7C7CC',
};

/**
 * Apple HIG Zero-Shift Hanging StatusLabel.
 *
 * Places a 6px status dot inside a 14px hanging left gutter
 * (`grid-template-columns: 6px minmax(0, 1fr); gap: 8px; margin-left: -14px;`)
 * so adjacent text labels align to an identical vertical X-axis (0.0000px shift)
 * inside parent containers with >= 16px left padding.
 */
@Component({
  selector: 'app-status-label',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span
      class="status-label-gutter"
      [class.status-label-gutter--inline]="inlineColumn"
    >
      <span
        class="status-label__dot"
        aria-hidden="true"
        [style.backgroundColor]="dotColor"
        [style.opacity]="showDot && status ? 1 : 0"
      ></span>
      <span class="status-label__text">
        <ng-container *ngIf="label; else projected">{{ label }}</ng-container>
        <ng-template #projected><ng-content></ng-content></ng-template>
      </span>
    </span>
  `,
  styles: [
    `
      :host {
        display: inline-block;
        max-width: 100%;
      }

      .status-label__dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        align-self: center;
        flex-shrink: 0;
      }

      .status-label__text {
        min-width: 0;
        color: #1D1D1F;
      }
    `,
  ],
})
export class StatusLabelComponent {
  @Input() status: ObsidianStatusTone | null | undefined = 'neutral';
  @Input() label = '';
  @Input() showDot = true;
  @Input() inlineColumn = false;

  get dotColor(): string {
    if (!this.showDot || !this.status) {
      return 'transparent';
    }
    return TONE_HEX_MAP[this.status] ?? '#1D1D1F';
  }
}
