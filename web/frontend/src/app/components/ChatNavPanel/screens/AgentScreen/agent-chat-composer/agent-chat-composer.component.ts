/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
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
 *
 * Changed for The Rehearsal: guard IME Enter composition and disabled send state on Enter, and clean up auto-resize timer on destroy.
 */

import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
  ViewChild,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-agent-chat-composer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule],
  templateUrl: './agent-chat-composer.component.html',
  styleUrls: ['./agent-chat-composer.component.scss'],
})
export class AgentChatComposerComponent implements AfterViewInit, OnChanges, OnDestroy {
  @ViewChild('chatTextArea', { read: ElementRef }) textAreaRef!: ElementRef<HTMLTextAreaElement>;

  @Input({ required: true }) text = '';
  @Output() textChange = new EventEmitter<string>();

  @Input({ required: true }) placeholder = '';
  @Input({ required: true }) isAgentWorking = false;
  @Input({ required: true }) canSend = false;
  @Input({ required: true }) isSecuringAgent = false;

  @Output() send = new EventEmitter<void>();

  private resizeTimer: ReturnType<typeof setTimeout> | null = null;

  ngAfterViewInit(): void {
    this.scheduleAutoResize();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['text']) {
      this.scheduleAutoResize();
    }
  }

  ngOnDestroy(): void {
    if (this.resizeTimer !== null) {
      clearTimeout(this.resizeTimer);
      this.resizeTimer = null;
    }
  }

  private scheduleAutoResize(): void {
    if (this.resizeTimer !== null) {
      clearTimeout(this.resizeTimer);
    }
    this.resizeTimer = setTimeout(() => {
      this.resizeTimer = null;
      this.autoResize();
    }, 0);
  }

  onInput(value: string): void {
    this.textChange.emit(value);
    this.autoResize();
  }

  onChatEnter(event: Event): void {
    const ke = event as KeyboardEvent;
    if (ke.shiftKey || ke.isComposing || ke.keyCode === 229) return;
    ke.preventDefault();
    if (!this.text.trim() || !this.canSend || this.isAgentWorking || this.isSecuringAgent) {
      return;
    }
    this.send.emit();
  }

  autoResize(): void {
    const textarea = this.textAreaRef?.nativeElement;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${textarea.scrollHeight}px`;
  }

  resetHeight(): void {
    const textarea = this.textAreaRef?.nativeElement;
    if (textarea) textarea.style.height = 'auto';
  }
}
