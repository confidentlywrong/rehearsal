/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: prevent stale agent init completions across reset/removal and guard optional ChangeDetectorRef.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 */

import { Injectable, inject, ChangeDetectorRef, OnDestroy } from '@angular/core';
import type { Subscription } from 'rxjs';
import { GatewayService, type BackendAgent } from '../../../../../gateway.service';

@Injectable()
export class AgentGatewayAgentsService implements OnDestroy {
  private readonly gateway = inject(GatewayService);
  private cdr!: ChangeDetectorRef;
  private sessionsSub?: Subscription;

  agents: Record<string, string | null> = {};
  initializingAgents: Record<string, boolean> = {};
  private epoch = 0;

  connect(cdr: ChangeDetectorRef): void {
    this.cdr = cdr;
    if (!this.sessionsSub && this.gateway.agents$) {
      this.sessionsSub = this.gateway.agents$.subscribe((sessions) => {
        this.syncWithGatewaySessions(sessions);
      });
    }
  }

  ngOnDestroy(): void {
    this.sessionsSub?.unsubscribe();
    this.sessionsSub = undefined;
  }

  private syncWithGatewaySessions(sessions: BackendAgent[]): void {
    const activeIds = new Set(sessions.map((s) => s.guid));
    let changed = false;
    for (const [agentType, guid] of Object.entries(this.agents)) {
      if (guid && !activeIds.has(guid)) {
        this.agents[agentType] = null;
        changed = true;
      }
    }
    if (changed) {
      this.mark();
    }
  }

  private mark(): void {
    this.cdr?.markForCheck();
  }

  getAgentDisplayName(agentType: string): string {
    const sessions = this.gateway.getAgents();
    const session = sessions.find((s: BackendAgent) => s.agentType === agentType);
    if (session?.displayName) return session.displayName;
    return agentType;
  }

  onRemoveAgent(agentType: string): void {
    this.epoch++;
    const guid = this.agents[agentType];
    if (guid) this.gateway.removeAgent(guid);
    this.agents[agentType] = null;
    this.mark();
  }

  removeAllActiveAgents(): void {
    this.epoch++;
    Object.entries(this.agents).forEach(([agentType, guid]) => {
      if (guid) this.gateway.removeAgent(guid);
      this.agents[agentType] = null;
    });
    this.initializingAgents = {};
    this.mark();
  }

  async onInitAgent(agentType: string, runCachedMessages: boolean): Promise<void> {
    if (runCachedMessages) return;
    if (this.agents[agentType] || this.initializingAgents[agentType]) return;
    const startEpoch = this.epoch;
    this.initializingAgents[agentType] = true;
    this.mark();
    try {
      const guid = await this.gateway.addAgent(agentType);
      if (startEpoch !== this.epoch) {
        if (guid) this.gateway.removeAgent(guid);
        return;
      }
      this.agents[agentType] = guid;
      this.mark();
    } catch (e) {
      console.error(`Init Agent ${agentType} failed:`, e);
    } finally {
      this.initializingAgents[agentType] = false;
      this.mark();
    }
  }
}
