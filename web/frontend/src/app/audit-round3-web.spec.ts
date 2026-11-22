// Copyright 2026 Christian Haeussinger
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import '@angular/compiler';
import { ChangeDetectorRef, Injector, SecurityContext, runInInjectionContext } from '@angular/core';
import { DomSanitizer } from '@angular/platform-browser';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { MarkdownService } from 'ngx-markdown';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { IdentityService } from './auth/identity.service';
import { UserMenuComponent } from './auth/user-menu.component';
import { AgentGatewayAgentsService } from './components/ChatNavPanel/screens/AgentScreen/services/agent-gateway-agents.service';
import { SimulationPanelComponent } from './components/ChatNavPanel/screens/AgentScreen/SimulationPanel/simulation-panel.component';
import { SimulationReportCardComponent } from './components/ChatNavPanel/screens/Log/simulation-report-card.component';
import { StakeholderGraphCardComponent } from './components/ChatNavPanel/screens/Log/stakeholder-graph-card.component';
import { StakeholderInterviewCardComponent } from './components/ChatNavPanel/screens/Log/stakeholder-interview-card.component';
import { StakeholderReactionsCardComponent } from './components/ChatNavPanel/screens/Log/stakeholder-reactions-card.component';
import { CountdownOverlayComponent } from './components/CountdownOverlay/countdown-overlay.component';
import { ScoreCardComponent } from './components/ScoreCard/score-card.component';
import { DashboardComponent } from './dashboard.component';
import { GatewayService } from './gateway.service';
import { HudComponent } from './hud/hud.component';
import { SanitizedMarkdownPipe } from './pipes/sanitized-markdown.pipe';
import { PortalService } from './settings/billing/portal.service';
import { ProfileService } from './settings/profile/profile.service';
import { BillingService, isSafeBillingRedirectUrl } from './usage/billing.service';
import { UsageService } from './usage/usage.service';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
  document.head.innerHTML = '';
});

function setupLoginDom(): void {
  document.body.innerHTML = `
    <span id="mode-pill"></span>
    <h1 id="card-title"></h1>
    <p id="card-subtitle"></p>
    <form id="login-form">
      <input id="email" />
      <div id="password-row"><input id="password" /></div>
      <button id="submit" type="submit"></button>
    </form>
    <div id="error"></div>
    <div id="notice"></div>
    <div id="toggles">
      <a id="toggle-signup" data-mode="signup"></a>
      <a id="toggle-reset" data-mode="reset"></a>
    </div>
    <div id="toggles-back">
      <a id="toggle-signin" data-mode="signin"></a>
    </div>
    <div id="footer"></div>
  `;
}

async function runLoginBootstrap(search: string, hash = ''): Promise<{
  replaceSpy: ReturnType<typeof vi.fn>;
  appendedScript: HTMLScriptElement | null;
}> {
  setupLoginDom();
  const replaceSpy = vi.fn();
  const win = window as unknown as Record<string, unknown>;
  win['__firebaseConfig'] = { apiKey: 'test-key', projectId: 'test-proj' };
  win['__rcAuthReady'] = true;
  win['__rcAppCheckToken'] = async () => null;
  win['__rcSignIn'] = async () => 'id-token-123';

  Object.defineProperty(window, 'location', {
    configurable: true,
    value: {
      origin: 'https://app.example.com',
      search,
      hash,
      replace: replaceSpy,
    },
  });

  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 })),
  });

  let appendedScript: HTMLScriptElement | null = null;
  vi.spyOn(document.head, 'appendChild').mockImplementation(<T extends Node>(node: T): T => {
    if (node instanceof Element && node.tagName === 'SCRIPT') {
      appendedScript = node as unknown as HTMLScriptElement;
      return node;
    }
    return HTMLElement.prototype.appendChild.call(document.head, node) as T;
  });

  const code = readFileSync(
    resolve(__dirname, '../../public/login/login-app.js'),
    'utf8',
  );
  new Function(code)();
  return { replaceSpy, appendedScript };
}

describe('Login bootstrap hardening (login-app.js)', () => {
  it('blocks open redirects (//, /\\, external origins) and preserves SPA hash routes', async () => {
    for (const evil of [
      '?redirect=//evil.example/steal',
      '?next=/\\evil.example',
      '?redirect=https://evil.example',
      '?next=javascript:alert(1)',
    ]) {
      const { replaceSpy } = await runLoginBootstrap(evil, '#/dashboard');
      (document.getElementById('email') as HTMLInputElement).value = 'a@example.com';
      (document.getElementById('password') as HTMLInputElement).value = 'secret123';
      document.getElementById('login-form')!.dispatchEvent(new Event('submit', { cancelable: true }));
      await vi.waitFor(() => expect(replaceSpy).toHaveBeenCalledTimes(1));
      expect(replaceSpy).toHaveBeenCalledWith('/');
    }

    const { replaceSpy } = await runLoginBootstrap(
      '?next=' + encodeURIComponent('/workspace?tab=1'),
      '#/routes/new',
    );
    (document.getElementById('email') as HTMLInputElement).value = 'a@example.com';
    (document.getElementById('password') as HTMLInputElement).value = 'secret123';
    document.getElementById('login-form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    await vi.waitFor(() => expect(replaceSpy).toHaveBeenCalledTimes(1));
    expect(replaceSpy).toHaveBeenCalledWith('/workspace?tab=1#/routes/new');
  });

  it('shows an error banner if /login/firebase-bootstrap.js fails to load', async () => {
    const { appendedScript } = await runLoginBootstrap('');
    expect(appendedScript?.getAttribute('src')).toBe('/login/firebase-bootstrap.js');
    appendedScript?.onerror?.(new Event('error'));
    expect(document.getElementById('error')?.textContent).toContain(
      'Failed to load authentication service',
    );
  });
});

describe('Redirect URL validation & service payload guards', () => {
  it('rejects javascript:, data:, protocol-relative, backslash, and control-char URLs', () => {
    expect(isSafeBillingRedirectUrl('https://checkout.stripe.com/pay/cs_123')).toBe(true);
    expect(isSafeBillingRedirectUrl('/settings/billing?checkout=success')).toBe(true);
    expect(isSafeBillingRedirectUrl('javascript:alert(1)')).toBe(false);
    expect(isSafeBillingRedirectUrl('data:text/html,<script>alert(1)</script>')).toBe(false);
    expect(isSafeBillingRedirectUrl('//evil.example/phish')).toBe(false);
    expect(isSafeBillingRedirectUrl('/\\evil.example')).toBe(false);
    expect(isSafeBillingRedirectUrl('https://evil.example/\x00foo')).toBe(false);
    expect(isSafeBillingRedirectUrl('')).toBe(false);
  });

  it('rejects malformed checkout & portal redirect responses in BillingService & PortalService', async () => {
    Object.defineProperty(globalThis, 'fetch', {
      configurable: true,
      value: vi
        .fn()
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ outcome: 'redirect', url: 'javascript:alert(1)' }), {
            status: 200,
          }),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ url: '//evil.example/portal' }), { status: 200 }),
        ),
    });

    const billing = new BillingService();
    const checkout = await billing.createCheckoutSession('pro');
    expect(checkout.outcome).toBe('error');

    const portal = new PortalService();
    const opened = await portal.openPortal();
    expect(opened.outcome).toBe('error');
  });

  it('rejects non-object/null/array JSON responses in IdentityService, ProfileService, and UsageService', async () => {
    const badResponses = () =>
      new Response('null', { status: 200, headers: { 'Content-Type': 'application/json' } });

    Object.defineProperty(globalThis, 'fetch', {
      configurable: true,
      value: vi.fn().mockImplementation(async () => badResponses()),
    });

    const idSvc = new IdentityService();
    expect((await idSvc.fetchMe()).available).toBe(false);

    const profSvc = new ProfileService();
    expect((await profSvc.fetchProfile()).available).toBe(false);

    const usageSvc = new UsageService();
    expect((await usageSvc.fetchUsage()).available).toBe(false);
  });
});

describe('SanitizedMarkdownPipe & UserMenuComponent cleanup', () => {
  it('handles non-strings, sanitizes parsed markdown, and safely sanitizes fallback on parse failure', async () => {
    const sanitize = vi.fn((_ctx: SecurityContext, html: unknown) =>
      String(html).replace(/<script.*?>.*?<\/script>/gi, ''),
    );
    const bypassSecurityTrustHtml = vi.fn((v: string) => ({ trusted: v }));
    const parse = vi
      .fn()
      .mockResolvedValueOnce('<p>ok</p><script>evil()</script>')
      .mockRejectedValueOnce(new Error('marked failed'));

    const injector = Injector.create({
      providers: [
        { provide: DomSanitizer, useValue: { sanitize, bypassSecurityTrustHtml } },
        { provide: MarkdownService, useValue: { parse } },
      ],
    });
    const pipe = runInInjectionContext(injector, () => new SanitizedMarkdownPipe());

    expect(await pipe.transform(null)).toBe('');
    expect(await pipe.transform(123 as unknown as string)).toBe('');

    expect(await pipe.transform('**ok**')).toEqual({ trusted: '<p>ok</p>' });
    expect(sanitize).toHaveBeenCalledWith(SecurityContext.HTML, '<p>ok</p><script>evil()</script>');

    const fallback = await pipe.transform('fallback<script>bad()</script>');
    expect(fallback).toEqual({ trusted: 'fallback' });
  });

  it('clears pending retryTimer in UserMenuComponent.ngOnDestroy()', async () => {
    vi.useFakeTimers();
    const comp = new UserMenuComponent(
      { ensureInitialized: vi.fn().mockResolvedValue({ available: false, status: 503 }) } as any,
      {} as any,
      { markForCheck: vi.fn() } as any,
      { controls$: { subscribe: () => ({ unsubscribe: () => {} }) } } as any,
    );
    await comp.ngOnInit();
    await Promise.resolve();
    expect((comp as unknown as { retryTimer: unknown }).retryTimer).not.toBeNull();
    comp.ngOnDestroy();
    expect((comp as unknown as { retryTimer: unknown }).retryTimer).toBeNull();
  });
});

describe('HUD, SimulationPanel, CountdownOverlay, ScoreCard, Dashboard, AgentGatewayAgentsService', () => {
  it('clamps NaN and out-of-range metrics in SimulationPanelComponent', () => {
    const panel = new SimulationPanelComponent();
    panel.simulationProgress = Number.NaN;
    panel.averageDistance = 'not-a-number';
    panel.numberOfFinishers = Number.POSITIVE_INFINITY;
    expect(panel.safeProgress).toBe(0);
    expect(panel.safeAverageDistance).toBe(0);
    expect(panel.safeFinishers).toBe(0);
    expect(panel.hours).toBe(0);
    expect(panel.mins).toBe(0);

    panel.simulationProgress = 250;
    panel.numberOfFinishers = 3.9;
    expect(panel.safeProgress).toBe(100);
    expect(panel.safeFinishers).toBe(3);
  });

  it('ignores non-finite countdown values and cleans listeners on ngOnDestroy in CountdownOverlayComponent', () => {
    const cdr = { markForCheck: vi.fn() } as unknown as ChangeDetectorRef;
    const overlay = new CountdownOverlayComponent(cdr);

    window.dispatchEvent(new CustomEvent('countdown:show', { detail: Number.NaN }));
    expect(overlay.entered).toBe(false);
    expect(overlay.slotA).toBeNull();

    window.dispatchEvent(new CustomEvent('countdown:show', { detail: 3 }));
    expect(overlay.entered).toBe(true);
    expect(overlay.slotA).toBe(3);

    overlay.ngOnDestroy();
    window.dispatchEvent(new CustomEvent('countdown:show', { detail: 2 }));
    expect(overlay.slotA).toBe(3);
  });

  it('guards missing route_data and event object in ScoreCardComponent', () => {
    const card = new ScoreCardComponent();
    card.route = { name: 'Demo', route_data: undefined } as any;
    expect(() => card.ngOnInit()).not.toThrow();
    expect(() => card.onShowRoute()).not.toThrow();
    expect(() => card.onRunSimulation(undefined)).not.toThrow();
  });

  it('ignores non-array labels:update details in DashboardComponent', () => {
    const dash = new DashboardComponent({ markForCheck: vi.fn() } as any);
    window.dispatchEvent(new CustomEvent('labels:update', { detail: { bad: true } }));
    expect(dash.labels).toEqual([]);
    window.dispatchEvent(
      new CustomEvent('labels:update', {
        detail: [{ id: '1', name: 'Route A', metricText: '5 mi', accentColor: '#fff' }],
      }),
    );
    expect(dash.labels).toHaveLength(1);
    dash.ngOnDestroy();
  });

  it('guards HudComponent sync payloads, bounded debugRunnerCount, and cleans copyLogTimer on destroy', () => {
    const hud = new HudComponent(
      { run: (fn: () => void) => fn() } as any,
      { markForCheck: vi.fn() } as any,
      { startMode: 'sandbox' } as any,
      { nativeElement: document.createElement('div') } as any,
    );
    hud.ngOnInit();
    window.dispatchEvent(new CustomEvent('hud:sync', { detail: { paths: 'not-array' } }));
    expect(hud.pathCount).toBe(0);
    window.dispatchEvent(new CustomEvent('hud:addSimRunner', { detail: {} }));
    expect(hud.simRunnerGuids).toEqual([]);
    hud.debugRunnerCount = Number.NaN;
    expect(() => hud.onStartDebugRace()).not.toThrow();
    hud.ngOnDestroy();
  });

  it('prevents stale addAgent completion from resurrecting after removeAllActiveAgents()', async () => {
    let resolveAdd!: (guid: string) => void;
    const removeAgent = vi.fn();
    const fakeGateway = {
      getAgents: () => [],
      addAgent: () => new Promise<string>((r) => { resolveAdd = r; }),
      removeAgent,
    };
    const injector = Injector.create({
      providers: [
        { provide: GatewayService, useValue: fakeGateway },
        AgentGatewayAgentsService,
      ],
    });
    const svc = injector.get(AgentGatewayAgentsService);
    const pending = svc.onInitAgent('planner', false);
    svc.removeAllActiveAgents();
    resolveAdd('stale-guid-99');
    await pending;

    expect(svc.agents['planner']).toBeFalsy();
    expect(removeAgent).toHaveBeenCalledWith('stale-guid-99');
  });
});

describe('Log Card parsers & gateway.proto parity', () => {
  it('rejects array payloads and handles unknown citation kinds + trimmed deduplicated IDs', () => {
    const reportCard = new SimulationReportCardComponent();
    reportCard.payload = [{ headline: 'bad' }] as any;
    expect(reportCard.payload).toBeNull();
    expect(reportCard.citationKindLabel('future_kind' as any)).toBe('Source');

    const interviewCard = new StakeholderInterviewCardComponent();
    interviewCard.payload = { stakeholder_interview: [] } as any;
    expect(interviewCard.payload).toBeNull();
    expect(interviewCard.citationKindLabel('future_kind' as any)).toBe('Source');

    const graphCard = new StakeholderGraphCardComponent();
    graphCard.payload = {
      nodes: [
        { id: '   ', label: 'Blank ID', kind: 'actor' },
        { id: 'cfo', label: '  CFO  ', kind: 'actor' },
      ],
      edges: [],
    };
    expect(graphCard.graph?.nodes).toEqual([{ id: 'cfo', label: 'CFO', kind: 'Actor' }]);

    const reactionsCard = new StakeholderReactionsCardComponent();
    reactionsCard.payload = {
      reactions: [
        { stakeholder_id: ' alice ', stance: 'supportive', reaction_text: 'Yes!' },
        { stakeholder_id: 'alice', stance: 'resistant', reaction_text: 'Duplicate!' },
      ],
    };
    expect(reactionsCard.payload?.reactions).toHaveLength(1);
    expect(reactionsCard.payload?.reactions[0].stakeholder_id).toBe('alice');
  });

  it('keeps web/frontend and web/tester gateway.proto synchronized with gen_proto messages', () => {
    const frontendProto = readFileSync(resolve(__dirname, '../../public/gateway.proto'), 'utf8');
    const testerProto = readFileSync(
      resolve(__dirname, '../../../tester/public/gateway.proto'),
      'utf8',
    );
    for (const msg of ['message ResourcePool', 'message OrganizationalState', 'message SynchronicityAlert']) {
      expect(frontendProto).toContain(msg);
      expect(testerProto).toContain(msg);
    }
    expect(frontendProto).toContain('string display_name = 4;');
  });
});
