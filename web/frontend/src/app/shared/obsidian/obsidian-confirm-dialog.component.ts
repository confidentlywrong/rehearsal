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
import {
  AfterViewInit,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  EventEmitter,
  inject,
  Input,
  OnDestroy,
  Output,
  ViewChild,
} from '@angular/core';

export interface ObsidianConfirmOptions {
  title?: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  closedBy?: 'any' | 'closerequest' | 'none';
}

let nextDialogUid = 0;

/**
 * Native `<dialog>` modal confirmation sheet styled with Obsidian Light
 * 5-Layer Liquid Glass. Uses `showModal()`, omits redundant ARIA dialog roles
 * per modern-web-guidance, resets `returnValue` immediately on close, and
 * provides a `pointerdown` + `pointerup` bounding-rect fallback when `closedBy`
 * is unsupported by the host browser.
 */
@Component({
  selector: 'app-obsidian-confirm-dialog',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <dialog
      #dialogEl
      class="obsidian-dialog apple-liquid-glass-card"
      [attr.aria-labelledby]="titleId"
      [attr.aria-describedby]="message ? messageId : null"
      [attr.closedby]="closedBy"
      (close)="handleNativeClose()"
      (pointerdown)="handlePointerDown($event)"
      (pointerup)="handlePointerUp($event)"
    >
      <div class="mirror-topline obsidian-dialog__header">
        <h2 [id]="titleId" class="obsidian-dialog__title">{{ title }}</h2>
      </div>

      <div class="obsidian-dialog__body">
        <p *ngIf="message" [id]="messageId" class="obsidian-dialog__message">
          {{ message }}
        </p>
        <ng-content></ng-content>
      </div>

      <div class="mirror-hairline"></div>

      <div class="macos-group-actions obsidian-dialog__actions">
        <button
          #cancelBtn
          type="button"
          class="apple-btn-secondary"
          [attr.autofocus]="destructive ? '' : null"
          (click)="cancelAction()"
        >
          {{ cancelLabel }}
        </button>
        <button
          #confirmBtn
          type="button"
          [class]="destructive ? 'apple-btn-destructive' : 'apple-btn-primary'"
          [attr.autofocus]="!destructive ? '' : null"
          (click)="confirmAction()"
        >
          {{ confirmLabel }}
        </button>
      </div>
    </dialog>
  `,
  styles: [
    `
      dialog.obsidian-dialog {
        display: none;
        padding: 0;
        border: 1px solid rgba(255, 255, 255, 0.82);
        border-radius: 18px;
        max-width: min(440px, calc(100vw - 24px));
        width: 100%;
        color: #1D1D1F;
        background: #FFFFFF;
      }

      dialog.obsidian-dialog[open] {
        display: block;
      }

      dialog.obsidian-dialog:modal {
        overscroll-behavior: contain;
      }

      dialog.obsidian-dialog::backdrop {
        overflow: hidden;
        overscroll-behavior: contain;
        background: rgba(0, 0, 0, 0.28);
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
      }

      .obsidian-dialog__header {
        padding: 12px 20px;
      }

      .obsidian-dialog__title {
        margin: 0;
        font-size: 14px;
        font-weight: 600;
        line-height: 1.3;
        color: #1D1D1F;
      }

      .obsidian-dialog__body {
        padding: 16px 20px;
      }

      .obsidian-dialog__message {
        margin: 0;
        font-size: 13px;
        line-height: 1.5;
        color: #52525B;
      }

      .obsidian-dialog__actions {
        padding: 12px 20px;
      }
    `,
  ],
})
export class ObsidianConfirmDialogComponent implements AfterViewInit, OnDestroy {
  @Input() title = 'Confirm action';
  @Input() message = '';
  @Input() confirmLabel = 'Confirm';
  @Input() cancelLabel = 'Cancel';
  @Input() destructive = false;
  @Input() closedBy: 'any' | 'closerequest' | 'none' = 'any';

  @Input()
  get open(): boolean {
    return this._open;
  }
  set open(value: boolean) {
    const next = Boolean(value);
    if (next === this._open) return;
    this._open = next;
    if (this.viewReady) {
      if (next) {
        this.openDialog();
      } else {
        this.closeDialog('cancel');
      }
    }
  }

  @Output() confirmed = new EventEmitter<void>();
  @Output() cancelled = new EventEmitter<void>();
  @Output() openChange = new EventEmitter<boolean>();

  @ViewChild('dialogEl', { static: true })
  private dialogEl!: ElementRef<HTMLDialogElement>;

  @ViewChild('cancelBtn', { static: true })
  private cancelBtn!: ElementRef<HTMLButtonElement>;

  @ViewChild('confirmBtn', { static: true })
  private confirmBtn!: ElementRef<HTMLButtonElement>;

  private readonly uid = ++nextDialogUid;
  readonly titleId = `obsidian-dialog-title-${this.uid}`;
  readonly messageId = `obsidian-dialog-message-${this.uid}`;

  private _open = false;
  private viewReady = false;
  private pressedOutside = false;
  private pendingResolve: ((accepted: boolean) => void) | null = null;

  private readonly cdr = inject(ChangeDetectorRef);

  ngAfterViewInit(): void {
    this.viewReady = true;
    if (this._open) {
      this.openDialog();
    }
  }

  ngOnDestroy(): void {
    if (this.pendingResolve) {
      const resolve = this.pendingResolve;
      this.pendingResolve = null;
      resolve(false);
    }
    const dialog = this.dialogEl?.nativeElement;
    if (dialog?.open && typeof dialog.close === 'function') {
      dialog.close('cancel');
    }
  }

  prompt(options: ObsidianConfirmOptions = {}): Promise<boolean> {
    if (this.pendingResolve) {
      const prevResolve = this.pendingResolve;
      this.pendingResolve = null;
      prevResolve(false);
    }
    if (options.title !== undefined) this.title = options.title;
    if (options.message !== undefined) this.message = options.message;
    if (options.confirmLabel !== undefined) this.confirmLabel = options.confirmLabel;
    if (options.cancelLabel !== undefined) this.cancelLabel = options.cancelLabel;
    if (options.destructive !== undefined) this.destructive = options.destructive;
    if (options.closedBy !== undefined) this.closedBy = options.closedBy;

    this.cdr.markForCheck();

    return new Promise<boolean>((resolve) => {
      this.pendingResolve = resolve;
      this.openDialog();
    });
  }

  openDialog(): void {
    const dialog = this.dialogEl?.nativeElement;
    if (!dialog) return;
    this._open = true;
    this.pressedOutside = false;
    if (!dialog.open) {
      dialog.returnValue = '';
      dialog.showModal();
    }
    this.openChange.emit(true);
    if (this.destructive) {
      this.cancelBtn?.nativeElement?.focus?.();
    } else {
      this.confirmBtn?.nativeElement?.focus?.();
    }
    this.cdr.markForCheck();
  }

  confirmAction(): void {
    this.closeDialog('confirm');
  }

  cancelAction(): void {
    this.closeDialog('cancel');
  }

  closeDialog(returnValue: 'confirm' | 'cancel' = 'cancel'): void {
    const dialog = this.dialogEl?.nativeElement;
    if (!dialog) return;
    if (dialog.open) {
      dialog.close(returnValue);
    } else {
      this.finalizeClose(returnValue === 'confirm');
    }
  }

  handleNativeClose(): void {
    const dialog = this.dialogEl?.nativeElement;
    if (!dialog) return;
    const didConfirm = dialog.returnValue === 'confirm';
    dialog.returnValue = '';
    this.finalizeClose(didConfirm);
  }

  handlePointerDown(ev: PointerEvent): void {
    if (this.closedBy !== 'any' || !this.needsClosedByFallback()) {
      this.pressedOutside = false;
      return;
    }
    this.pressedOutside = this.isPointerOutsideDialog(ev);
  }

  handlePointerUp(ev: PointerEvent): void {
    if (this.closedBy !== 'any' || !this.needsClosedByFallback()) {
      this.pressedOutside = false;
      return;
    }
    const wasPressedOutside = this.pressedOutside;
    this.pressedOutside = false;
    if (wasPressedOutside && this.isPointerOutsideDialog(ev)) {
      this.closeDialog('cancel');
    }
  }

  private needsClosedByFallback(): boolean {
    return (
      typeof HTMLDialogElement === 'undefined' ||
      !('closedBy' in HTMLDialogElement.prototype)
    );
  }

  private isPointerOutsideDialog(ev: PointerEvent): boolean {
    const dialog = this.dialogEl?.nativeElement;
    if (!dialog) return false;
    const rect = dialog.getBoundingClientRect();
    if (rect.width > 0 || rect.height > 0) {
      return (
        ev.clientX < rect.left ||
        ev.clientX > rect.right ||
        ev.clientY < rect.top ||
        ev.clientY > rect.bottom
      );
    }
    return ev.target === dialog;
  }

  private finalizeClose(didConfirm: boolean): void {
    this._open = false;
    this.pressedOutside = false;
    this.openChange.emit(false);
    if (didConfirm) {
      this.confirmed.emit();
    } else {
      this.cancelled.emit();
    }
    if (this.pendingResolve) {
      const resolve = this.pendingResolve;
      this.pendingResolve = null;
      resolve(didConfirm);
    }
    this.cdr.markForCheck();
  }
}
