/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

import { Component, Input, ViewChild, ElementRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SanitizedMarkdownPipe } from '../../../../../pipes/sanitized-markdown.pipe';
import { A2uiControllerComponent } from '../../../../a2ui/a2-ui-controller.component';
import { StakeholderGraphCardComponent } from '../../Log/stakeholder-graph-card.component';
import { StakeholderPersonasCardComponent } from '../../Log/stakeholder-personas-card.component';
import { StakeholderReactionsCardComponent } from '../../Log/stakeholder-reactions-card.component';
import { SimulationTimelineCardComponent } from '../../Log/simulation-timeline-card.component';
import { SimulationReportCardComponent } from '../../Log/simulation-report-card.component';
import { StakeholderInterviewCardComponent } from '../../Log/stakeholder-interview-card.component';
import { TranslationDiagnosticCardComponent } from '../../Log/translation-diagnostic-card.component';
import { StakeholderPsychologyCardComponent } from '../../Log/stakeholder-psychology-card.component';

import type { DisplayItem } from '../agent-screen.types';
import type { AgentScreenHost } from '../agent-screen-host.model';

@Component({
  selector: 'app-agent-chat-thread',
  standalone: true,
  imports: [
    CommonModule,
    SanitizedMarkdownPipe,
    A2uiControllerComponent,
    StakeholderGraphCardComponent,
    StakeholderPersonasCardComponent,
    StakeholderReactionsCardComponent,
    SimulationTimelineCardComponent,
    SimulationReportCardComponent,
    StakeholderInterviewCardComponent,
    TranslationDiagnosticCardComponent,
    StakeholderPsychologyCardComponent,
  ],
  templateUrl: './agent-chat-thread.component.html',
  styleUrls: ['./agent-chat-thread.component.scss'],
})
export class AgentChatThreadComponent {
  @Input({ required: true }) host!: AgentScreenHost;

  @ViewChild('chatScroll', { read: ElementRef }) chatScrollRef!: ElementRef<HTMLDivElement>;

  trackByDisplayItem = (i: number, item: DisplayItem): string =>
    this.host.chatDisplay.trackByDisplayItem(i, item);

  getScrollNative(): HTMLDivElement {
    return this.chatScrollRef.nativeElement;
  }

  /** Returns resource delta entries as an array for *ngFor iteration */
  getResourceEntries(deltas: Record<string, { name: string; current: number; delta: number; is_critical: boolean; is_warning: boolean }>): Array<{ key: string; name: string; current: number; delta: number; is_critical: boolean; is_warning: boolean }> {
    if (!deltas) return [];
    return Object.entries(deltas).map(([key, v]) => ({
      key,
      name: v.name,
      current: v.current,
      delta: v.delta,
      is_critical: v.is_critical,
      is_warning: v.is_warning,
    }));
  }

  /** Returns entries where delta != 0 (something actually changed) */
  getActiveResourceDeltas(deltas: Record<string, { name: string; current: number; delta: number; is_critical: boolean; is_warning: boolean }>): Array<{ key: string; name: string; current: number; delta: number; is_critical: boolean; is_warning: boolean }> {
    return this.getResourceEntries(deltas).filter(e => e.delta !== 0 || e.is_critical);
  }

  /** Check if there are any infrastructure events worth showing */
  hasInfraEvents(item: Extract<DisplayItem, { kind: 'tick_feed' }>): boolean {
    const infra = item.infrastructure;
    if (!infra) return false;
    return infra.medical_saves_this_tick > 0 || infra.comfort_warning || infra.friction_level !== 'none';
  }

  /** Returns transition entries as an array for *ngFor, sorted by severity (deadlocked first) */
  getTransitionEntries(transitions: Record<string, { total: number; by_persona: Record<string, number> }>): Array<{ type: string; total: number; by_persona: Record<string, number> }> {
    if (!transitions) return [];
    const order = ['deadlocked', 'stalled', 'aligned'];
    return Object.entries(transitions)
      .map(([type, data]) => ({ type, total: data.total, by_persona: data.by_persona }))
      .sort((a, b) => {
        const ai = order.indexOf(a.type);
        const bi = order.indexOf(b.type);
        return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
      });
  }

  /** Format persona breakdown as a compact string: "5 Disruptors, 4 Visionaries" */
  formatPersonaBreakdown(byPersona: Record<string, number>): string {
    if (!byPersona) return '';
    const labels: Record<string, string> = {
      pragmatist: 'Pragmatists',
      visionary: 'Visionaries',
      diplomat: 'Diplomats',
      disruptor: 'Disruptors',
    };
    return Object.entries(byPersona)
      .sort((a, b) => b[1] - a[1])
      .map(([persona, count]) => `${count} ${labels[persona] || persona}`)
      .join(', ');
  }

  /** Check if there are any transitions worth showing */
  hasEventsSummary(item: Extract<DisplayItem, { kind: 'tick_feed' }>): boolean {
    const s = item.eventsSummary;
    if (!s || !s.transitions) return false;
    return Object.keys(s.transitions).length > 0;
  }

  /** Return severity CSS class for a transition type */
  transitionSeverityClass(type: string): string {
    switch (type) {
      case 'deadlocked': return 'tick-feed__transition--critical';
      case 'stalled': return 'tick-feed__transition--warning';
      case 'aligned': return 'tick-feed__transition--positive';
      default: return '';
    }
  }

  /** Return severity arrow for a transition type */
  transitionArrow(type: string): string {
    switch (type) {
      case 'deadlocked': return '▼▼';
      case 'stalled': return '▼';
      case 'aligned': return '▲';
      default: return '—';
    }
  }
}
