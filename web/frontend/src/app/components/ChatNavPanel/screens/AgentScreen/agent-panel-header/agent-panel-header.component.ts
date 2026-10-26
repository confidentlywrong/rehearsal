/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

import { Component, ChangeDetectionStrategy, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import type { AgentScreenHost } from '../agent-screen-host.model';
import { AgentTabStripComponent } from '../agent-tab-strip/agent-tab-strip.component';

// Gear/unfold buttons were removed on 2026-06-22. AgentSettingsMenuComponent
// is no longer mounted from here; the demo runtime knobs live in the user
// menu at the top-right via AgentRuntimeControlsService.

@Component({
  selector: 'app-agent-panel-header',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, AgentTabStripComponent],
  templateUrl: './agent-panel-header.component.html',
  styleUrls: ['./agent-panel-header.component.scss'],
})
export class AgentPanelHeaderComponent {
  @Input({ required: true }) host!: AgentScreenHost;
  @Input({ required: true }) panelExpanded!: boolean;
  @Input({ required: true }) activeTab!: 'agent' | 'organizer' | 'log';
  @Input({ required: true }) tabBtnWide!: boolean;
  @Input({ required: true }) showTabLabels!: boolean;
  @Input({ required: true }) isAgentWorking!: boolean;
}
