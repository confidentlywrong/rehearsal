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
 * Changed for The Rehearsal: align nullable scroll element return type with AgentChatThreadComponent.
 */

import { Component, Input, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import type { AgentScreenHost } from '../agent-screen-host.model';
import { AgentChatThreadComponent } from '../agent-chat-thread/agent-chat-thread.component';
import { AgentChatComposerComponent } from '../agent-chat-composer/agent-chat-composer.component';

@Component({
  selector: 'app-agent-chat-panel',
  standalone: true,
  imports: [CommonModule, AgentChatThreadComponent, AgentChatComposerComponent],
  templateUrl: './agent-chat-panel.component.html',
  styleUrls: ['./agent-chat-panel.component.scss'],
})
export class AgentChatPanelComponent {
  @Input({ required: true }) host!: AgentScreenHost;

  @ViewChild(AgentChatThreadComponent) private chatThread?: AgentChatThreadComponent;

  getChatScrollNative(): HTMLDivElement | null | undefined {
    return this.chatThread?.getScrollNative();
  }

  scrollChatToBottom(): void {
    const el = this.chatThread?.getScrollNative();
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }

  onTextChange(value: string): void {
    this.host.chatInput = value;
    this.host.cdr.markForCheck();
  }
}
