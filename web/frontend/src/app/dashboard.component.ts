/**
 * Copyright 2026 Google LLC
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

/**
 * DashboardComponent — Phase 9.
 *
 * Hosts the live 3D viewport + HUD overlays + token-usage tile that
 * make up the simulation dashboard. Was previously inlined into
 * AppComponent; lifted out so the router can mount alternative
 * top-level views (e.g. /settings/*) without dragging the Three.js
 * scene with them.
 *
 * Visual contract: when SAAS_MODE=off, this component renders
 * byte-identically to the pre-Phase 9 AppComponent — the same
 * children are mounted in the same order, no router-outlet wrapping.
 */

import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnDestroy } from '@angular/core';
import { ViewportComponent } from './viewport/viewport-lookdev.component';
import { HudComponent } from './hud/hud.component';
import { LabelComponent, LabelEntry } from './label/label.component';
import { FilterMenuComponent } from './components/FilterMenu/filter-menu.component';
import { CountdownOverlayComponent } from './components/CountdownOverlay/countdown-overlay.component';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [
    ViewportComponent,
    HudComponent,
    LabelComponent,
    FilterMenuComponent,
    CountdownOverlayComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-viewport-lookdev></app-viewport-lookdev>
    <app-hud></app-hud>
    <app-filter-menu></app-filter-menu>
    <app-countdown-overlay></app-countdown-overlay>
    <app-label [labels]="labels"></app-label>
  `,
  styles: [
    `
      :host {
        display: block;
        width: 100vw;
        height: 100vh;
        overflow: hidden;
      }
    `,
  ],
})
export class DashboardComponent implements OnDestroy {
  labels: LabelEntry[] = [];

  // Stored as a bound reference so removeEventListener in ngOnDestroy
  // can target the same function instance the constructor registered.
  // Without this the listener would leak on every route change away
  // from / and back (B-14, 2026-06-22 audit).
  private readonly onLabelsUpdate = (e: Event): void => {
    this.labels = (e as CustomEvent).detail;
    this.cdr.markForCheck();
  };

  constructor(private cdr: ChangeDetectorRef) {
    window.addEventListener('labels:update', this.onLabelsUpdate);
  }

  ngOnDestroy(): void {
    window.removeEventListener('labels:update', this.onLabelsUpdate);
  }
}
