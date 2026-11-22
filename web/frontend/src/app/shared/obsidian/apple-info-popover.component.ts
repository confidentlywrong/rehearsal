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

import { CommonModule, DOCUMENT } from '@angular/common';
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  EmbeddedViewRef,
  inject,
  Input,
  OnDestroy,
  TemplateRef,
  ViewChild,
  ViewContainerRef,
} from '@angular/core';

export interface AppleInfoPopoverItem {
  label: string;
  value: string;
}

export interface PopoverRect {
  top: number;
  left: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

export interface PopoverSize {
  width: number;
  height: number;
}

export interface PopoverViewport {
  width: number;
  height: number;
}

export type PopoverPlacement = 'auto' | 'top' | 'bottom';

export interface PopoverCoordinates {
  top: number;
  left: number;
  maxHeight: number;
  placement: 'top' | 'bottom';
}

const OPEN_EVENT = 'apple-info-popover-open';
const VIEWPORT_MARGIN = 12;
const POPOVER_GAP = 8;

let nextPopoverUid = 0;

/**
 * Pure 4-edge viewport clamping & vertical flip calculator for AppleInfoPopover.
 */
export function computePopoverPosition(
  triggerRect: PopoverRect,
  popSize: PopoverSize,
  viewport: PopoverViewport,
  placement: PopoverPlacement = 'auto',
  margin = VIEWPORT_MARGIN,
  gap = POPOVER_GAP,
): PopoverCoordinates {
  const maxAvailW = Math.max(0, viewport.width - 2 * margin);
  const maxAvailH = Math.max(0, viewport.height - 2 * margin);
  const popW = Math.min(Math.ceil(Math.max(0, popSize.width)), maxAvailW);
  const popH = Math.min(Math.ceil(Math.max(0, popSize.height)), maxAvailH);

  const maxLeft = Math.max(margin, viewport.width - popW - margin);
  const left = Math.floor(Math.max(margin, Math.min(triggerRect.left, maxLeft)));

  const fitsBelow = triggerRect.bottom + gap + popH <= viewport.height - margin;
  const fitsAbove = triggerRect.top - gap - popH >= margin;

  let resolvedPlacement: 'top' | 'bottom';
  let rawTop: number;

  if (placement === 'top') {
    resolvedPlacement = fitsAbove || !fitsBelow ? 'top' : 'bottom';
    rawTop =
      resolvedPlacement === 'top'
        ? triggerRect.top - popH - gap
        : triggerRect.bottom + gap;
  } else if (placement === 'bottom') {
    resolvedPlacement = fitsBelow || !fitsAbove ? 'bottom' : 'top';
    rawTop =
      resolvedPlacement === 'bottom'
        ? triggerRect.bottom + gap
        : triggerRect.top - popH - gap;
  } else if (fitsBelow) {
    resolvedPlacement = 'bottom';
    rawTop = triggerRect.bottom + gap;
  } else if (fitsAbove) {
    resolvedPlacement = 'top';
    rawTop = triggerRect.top - popH - gap;
  } else {
    const spaceBelow = viewport.height - triggerRect.bottom - gap - margin;
    const spaceAbove = triggerRect.top - gap - margin;
    resolvedPlacement = spaceAbove > spaceBelow ? 'top' : 'bottom';
    rawTop =
      resolvedPlacement === 'top'
        ? triggerRect.top - popH - gap
        : triggerRect.bottom + gap;
  }

  const maxTop = Math.max(margin, viewport.height - popH - margin);
  const top = Math.floor(Math.max(margin, Math.min(rawTop, maxTop)));
  const maxHeight = Math.max(0, Math.floor(viewport.height - top - margin));

  return { top, left, maxHeight, placement: resolvedPlacement };
}

@Component({
  selector: 'app-apple-info-popover',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      #triggerBtn
      type="button"
      [id]="popoverId + '-trigger'"
      class="apple-popover-trigger"
      [class.apple-popover-trigger--labeled]="!!label"
      [class.apple-popover-trigger--open]="isOpen"
      [attr.aria-label]="resolvedAriaLabel"
      aria-haspopup="dialog"
      [attr.aria-expanded]="isOpen"
      [attr.aria-controls]="isOpen ? popoverId : null"
      (click)="onTriggerClick($event)"
      (mousedown)="onTriggerMouseDown($event)"
      (mouseenter)="onMouseEnter()"
      (mouseleave)="onMouseLeave()"
    >
      <span *ngIf="label" class="apple-popover-trigger__label">{{ label }}</span>
      <svg
        class="apple-popover-trigger__icon"
        viewBox="0 0 16 16"
        width="14"
        height="14"
        fill="none"
        aria-hidden="true"
      >
        <circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.3" />
        <path
          d="M8 7.2v4M8 4.8h.01"
          stroke="currentColor"
          stroke-width="1.4"
          stroke-linecap="round"
        />
      </svg>
    </button>

    <ng-template #popoverTemplate>
      <div
        #popoverEl
        [id]="popoverId"
        class="apple-liquid-glass-card animate-apple-popover-in apple-popover-panel"
        role="dialog"
        aria-modal="false"
        [attr.aria-label]="resolvedHeading"
        [attr.aria-labelledby]="popoverId + '-title'"
        tabindex="-1"
        [style.top.px]="coords.top"
        [style.left.px]="coords.left"
        [style.width.px]="clampedWidthPx"
        [style.maxHeight.px]="coords.maxHeight"
        (mouseenter)="onMouseEnter()"
        (mouseleave)="onMouseLeave()"
        (click)="onPopoverClick($event)"
        (mousedown)="onPopoverMouseDown($event)"
      >
        <div class="mirror-topline apple-popover-panel__header">
          <span [id]="popoverId + '-title'" class="apple-popover-panel__title">
            {{ resolvedHeading }}
          </span>
          <span *ngIf="subtitle" class="apple-popover-panel__subtitle">
            {{ subtitle }}
          </span>
        </div>

        <div class="apple-popover-panel__body">
          <p *ngIf="lead" class="apple-popover-panel__lead">
            <strong>{{ lead }}</strong>
          </p>

          <p *ngIf="!lead && text" class="apple-popover-panel__text">
            {{ text }}
          </p>

          <div
            *ngIf="(lead || text) && (items.length > 0 || note)"
            class="mirror-hairline apple-popover-panel__divider"
          ></div>

          <div *ngIf="items.length > 0" class="apple-popover-panel__grid">
            <ng-container *ngFor="let item of items">
              <span class="apple-popover-panel__item-label">{{ item.label }}</span>
              <span class="apple-popover-panel__item-value">{{ item.value }}</span>
            </ng-container>
          </div>

          <div
            *ngIf="items.length > 0 && note"
            class="mirror-hairline apple-popover-panel__divider"
          ></div>

          <p *ngIf="note" class="apple-popover-panel__note">
            {{ note }}
          </p>

          <ng-content></ng-content>
        </div>
      </div>
    </ng-template>
  `,
  styles: [
    `
      :host {
        display: inline-flex;
        align-items: center;
        vertical-align: middle;
      }

      .apple-popover-trigger {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        min-width: 24px;
        min-height: 24px;
        padding: 3px;
        border: none;
        border-radius: 50%;
        background: transparent;
        color: #6E6E73;
        cursor: pointer;
        font-family: inherit;
        font-size: 12px;
        line-height: 1;
        transition: color 0.15s ease, background-color 0.15s ease;
      }

      .apple-popover-trigger--labeled {
        border-radius: 8px;
        padding: 3px 6px;
      }

      .apple-popover-trigger:hover,
      .apple-popover-trigger--open {
        color: #1D1D1F;
        background: rgba(0, 0, 0, 0.06);
      }

      .apple-popover-trigger:focus-visible {
        outline: none;
        box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.25);
      }

      @media (forced-colors: active) {
        .apple-popover-trigger:focus-visible {
          outline: 2px solid Highlight;
          outline-offset: 2px;
        }
      }

      .apple-popover-trigger__label {
        font-weight: 500;
        color: inherit;
      }

      .apple-popover-trigger__icon {
        flex-shrink: 0;
        display: block;
      }
    `,
    `
      .apple-popover-panel {
        position: fixed;
        z-index: 100010;
        max-width: calc(100vw - 24px);
        overflow-y: auto;
        border-radius: 16px;
        background: linear-gradient(
          155deg,
          rgba(255, 255, 255, 0.96) 0%,
          rgba(255, 255, 255, 0.92) 55%,
          rgba(255, 255, 255, 0.95) 100%
        );
        color: #1D1D1F;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", system-ui, sans-serif;
        text-align: left;
      }

      .apple-popover-panel__header {
        position: sticky;
        top: 0;
        z-index: 1;
        padding: 10px 16px;
      }

      .apple-popover-panel__title {
        font-size: 12px;
        font-weight: 600;
        color: #1D1D1F;
        letter-spacing: -0.01em;
      }

      .apple-popover-panel__subtitle {
        font-size: 12px;
        font-weight: 400;
        color: #52525B;
        flex-shrink: 0;
      }

      .apple-popover-panel__body {
        padding: 14px 16px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        font-size: 13px;
        line-height: 1.5;
        color: #1D1D1F;
      }

      .apple-popover-panel__lead {
        margin: 0;
        font-size: 13px;
        color: #1D1D1F;
      }

      .apple-popover-panel__text {
        margin: 0;
        font-size: 13px;
        color: #52525B;
      }

      .apple-popover-panel__divider {
        margin: 2px 0;
      }

      .apple-popover-panel__grid {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr);
        column-gap: 12px;
        row-gap: 6px;
        font-size: 12px;
        font-variant-numeric: tabular-nums;
      }

      .apple-popover-panel__item-label {
        color: #52525B;
        font-weight: 400;
      }

      .apple-popover-panel__item-value {
        color: #1D1D1F;
        font-weight: 600;
        text-align: right;
      }

      .apple-popover-panel__note {
        margin: 0;
        font-size: 12px;
        color: #52525B;
      }
    `,
  ],
})
export class AppleInfoPopoverComponent implements OnDestroy {
  @Input() text = '';
  @Input() title = '';
  @Input() subtitle = '';
  @Input() lead = '';
  @Input() items: AppleInfoPopoverItem[] = [];
  @Input() note = '';
  @Input() label = '';
  @Input() ariaLabel = '';
  @Input() placement: PopoverPlacement = 'auto';
  @Input() widthPx = 340;

  @ViewChild('triggerBtn', { static: true })
  private triggerBtn!: ElementRef<HTMLButtonElement>;

  @ViewChild('popoverTemplate', { static: true })
  private popoverTemplate!: TemplateRef<unknown>;

  @ViewChild('popoverEl')
  private popoverEl?: ElementRef<HTMLDivElement>;

  readonly instanceId = `apple-popover-${++nextPopoverUid}`;
  readonly popoverId = this.instanceId;

  isOpen = false;
  isPinned = false;
  coords: PopoverCoordinates = {
    top: 12,
    left: 12,
    maxHeight: 400,
    placement: 'bottom',
  };

  private portalView: EmbeddedViewRef<unknown> | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private hoverOpenTimer: ReturnType<typeof setTimeout> | null = null;
  private hoverCloseTimer: ReturnType<typeof setTimeout> | null = null;
  private rafId: number | null = null;
  private globalListenersAttached = false;
  private destroyed = false;

  private readonly onOtherPopoverOpen = (ev: Event): void => {
    const custom = ev as CustomEvent<string>;
    if (custom.detail !== this.instanceId && this.isOpen) {
      this.closePopover(false);
    }
  };

  private readonly onWindowScroll = (ev: Event): void => {
    if (!this.isOpen) return;
    const popEl = this.getPopoverElement();
    const target = ev.target as Node | null;
    if (popEl && target && (target === popEl || popEl.contains(target))) {
      return;
    }
    this.closePopover(false);
  };

  private readonly onWindowResize = (): void => {
    if (!this.isOpen) return;
    this.schedulePositionUpdate();
  };

  private readonly onWindowKeyDown = (ev: KeyboardEvent): void => {
    if (ev.key === 'Escape' && this.isOpen) {
      ev.preventDefault();
      this.closePopover(true);
    }
  };

  private readonly onDocumentPointerDown = (ev: Event): void => {
    if (!this.isOpen) return;
    const target = ev.target as Node | null;
    if (!target) return;
    const triggerEl = this.triggerBtn?.nativeElement;
    const popEl = this.getPopoverElement();
    if (triggerEl?.contains(target) || popEl?.contains(target)) {
      return;
    }
    this.closePopover(false);
  };

  private readonly viewContainerRef = inject(ViewContainerRef);
  private readonly cdr = inject(ChangeDetectorRef);
  private readonly doc = inject(DOCUMENT);

  constructor() {
    if (typeof window !== 'undefined') {
      window.addEventListener(OPEN_EVENT, this.onOtherPopoverOpen);
    }
  }

  get resolvedHeading(): string {
    return this.title || this.label || 'Information';
  }

  get resolvedAriaLabel(): string {
    if (this.ariaLabel) return this.ariaLabel;
    if (this.label && this.title) return `${this.label} - Info: ${this.title}`;
    if (this.title) return `Info: ${this.title}`;
    if (this.label) return `Info: ${this.label}`;
    return 'More information';
  }

  get clampedWidthPx(): number {
    if (typeof window === 'undefined') return this.widthPx;
    return Math.min(this.widthPx, Math.max(0, window.innerWidth - 2 * VIEWPORT_MARGIN));
  }

  onTriggerClick(ev: MouseEvent): void {
    ev.preventDefault();
    ev.stopPropagation();
    this.clearTimers();
    if (this.isOpen && this.isPinned) {
      this.closePopover(false);
    } else {
      this.openPopover(true);
    }
  }

  onTriggerMouseDown(ev: MouseEvent): void {
    ev.stopPropagation();
  }

  onPopoverClick(ev: MouseEvent): void {
    ev.stopPropagation();
    this.isPinned = true;
  }

  onPopoverMouseDown(ev: MouseEvent): void {
    ev.stopPropagation();
  }

  onMouseEnter(): void {
    if (this.hoverCloseTimer) {
      clearTimeout(this.hoverCloseTimer);
      this.hoverCloseTimer = null;
    }
    if (this.isOpen) return;
    this.hoverOpenTimer = setTimeout(() => {
      this.hoverOpenTimer = null;
      if (!this.destroyed) {
        this.openPopover(false);
      }
    }, 140);
  }

  onMouseLeave(): void {
    if (this.hoverOpenTimer) {
      clearTimeout(this.hoverOpenTimer);
      this.hoverOpenTimer = null;
    }
    if (this.isPinned || !this.isOpen) return;
    this.hoverCloseTimer = setTimeout(() => {
      this.hoverCloseTimer = null;
      if (!this.destroyed && !this.isPinned) {
        this.closePopover(false);
      }
    }, 220);
  }

  openPopover(pinned: boolean): void {
    if (this.destroyed) return;
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: this.instanceId }));
    }
    this.isOpen = true;
    this.isPinned = pinned;
    this.mountPortal();
    this.updatePosition();
    this.attachGlobalListeners();
    this.attachResizeObserver();
    this.cdr.markForCheck();
  }

  closePopover(restoreFocus: boolean): void {
    this.clearTimers();
    this.isOpen = false;
    this.isPinned = false;
    this.detachGlobalListeners();
    this.detachResizeObserver();
    this.destroyPortal();
    if (restoreFocus) {
      this.triggerBtn?.nativeElement?.focus();
    }
    if (!this.destroyed) {
      this.cdr.markForCheck();
    }
  }

  updatePosition(): void {
    if (!this.isOpen || typeof window === 'undefined') return;
    const triggerEl = this.triggerBtn?.nativeElement;
    if (!triggerEl || !triggerEl.isConnected) {
      this.closePopover(false);
      return;
    }

    const triggerRect = triggerEl.getBoundingClientRect();
    if (
      triggerRect.bottom < 0 ||
      triggerRect.top > window.innerHeight ||
      triggerRect.right < 0 ||
      triggerRect.left > window.innerWidth
    ) {
      this.closePopover(false);
      return;
    }

    const popEl = this.getPopoverElement();
    const popRect = popEl?.getBoundingClientRect();
    const rawW = Math.max(popEl?.offsetWidth || 0, popRect?.width || 0, this.widthPx);
    const rawH = Math.max(popEl?.offsetHeight || 0, popRect?.height || 0, 180);

    this.coords = computePopoverPosition(
      triggerRect,
      { width: rawW, height: rawH },
      { width: window.innerWidth, height: window.innerHeight },
      this.placement,
      VIEWPORT_MARGIN,
      POPOVER_GAP,
    );

    if (popEl) {
      popEl.style.top = `${this.coords.top}px`;
      popEl.style.left = `${this.coords.left}px`;
      popEl.style.maxHeight = `${this.coords.maxHeight}px`;
    }
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.clearTimers();
    this.detachGlobalListeners();
    this.detachResizeObserver();
    this.destroyPortal();
    if (typeof window !== 'undefined') {
      window.removeEventListener(OPEN_EVENT, this.onOtherPopoverOpen);
    }
  }

  private schedulePositionUpdate(): void {
    if (this.rafId !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    if (typeof requestAnimationFrame === 'function') {
      this.rafId = requestAnimationFrame(() => {
        this.rafId = null;
        this.updatePosition();
      });
    } else {
      this.updatePosition();
    }
  }

  private getPopoverElement(): HTMLDivElement | null {
    if (this.popoverEl?.nativeElement) {
      return this.popoverEl.nativeElement;
    }
    const rootNode = this.portalView?.rootNodes?.[0];
    return rootNode instanceof HTMLDivElement ? rootNode : null;
  }

  private mountPortal(): void {
    if (this.portalView || !this.doc?.body) return;
    this.portalView = this.viewContainerRef.createEmbeddedView(this.popoverTemplate);
    this.portalView.detectChanges();
    for (const node of this.portalView.rootNodes) {
      this.doc.body.appendChild(node);
    }
  }

  private destroyPortal(): void {
    if (!this.portalView) return;
    for (const node of this.portalView.rootNodes) {
      if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    }
    this.portalView.destroy();
    this.portalView = null;
  }

  private attachResizeObserver(): void {
    this.detachResizeObserver();
    const popEl = this.getPopoverElement();
    if (!popEl || typeof ResizeObserver === 'undefined') return;
    this.resizeObserver = new ResizeObserver(() => {
      this.schedulePositionUpdate();
    });
    this.resizeObserver.observe(popEl);
  }

  private detachResizeObserver(): void {
    if (this.resizeObserver) {
      this.resizeObserver.disconnect();
      this.resizeObserver = null;
    }
  }

  private attachGlobalListeners(): void {
    if (this.globalListenersAttached || typeof window === 'undefined') return;
    this.globalListenersAttached = true;
    window.addEventListener('scroll', this.onWindowScroll, true);
    window.addEventListener('resize', this.onWindowResize);
    window.addEventListener('keydown', this.onWindowKeyDown);
    this.doc.addEventListener('pointerdown', this.onDocumentPointerDown);
  }

  private detachGlobalListeners(): void {
    if (!this.globalListenersAttached || typeof window === 'undefined') return;
    this.globalListenersAttached = false;
    window.removeEventListener('scroll', this.onWindowScroll, true);
    window.removeEventListener('resize', this.onWindowResize);
    window.removeEventListener('keydown', this.onWindowKeyDown);
    this.doc.removeEventListener('pointerdown', this.onDocumentPointerDown);
  }

  private clearTimers(): void {
    if (this.hoverOpenTimer) {
      clearTimeout(this.hoverOpenTimer);
      this.hoverOpenTimer = null;
    }
    if (this.hoverCloseTimer) {
      clearTimeout(this.hoverCloseTimer);
      this.hoverCloseTimer = null;
    }
    if (this.rafId !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
  }
}
