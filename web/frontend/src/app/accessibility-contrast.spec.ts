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
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { StakeholderPsychologyCardComponent } from './components/ChatNavPanel/screens/Log/stakeholder-psychology-card.component';

const APP_DIR = dirname(fileURLToPath(import.meta.url));
const SRC_DIR = resolve(APP_DIR, '..');

function srgbToLinear(c: number): number {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
}

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const sat = s / 100;
  const lig = l / 100;
  const c = (1 - Math.abs(2 * lig - 1)) * sat;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = lig - c / 2;
  let r1 = 0;
  let g1 = 0;
  let b1 = 0;
  if (h < 60) [r1, g1, b1] = [c, x, 0];
  else if (h < 120) [r1, g1, b1] = [x, c, 0];
  else if (h < 180) [r1, g1, b1] = [0, c, x];
  else if (h < 240) [r1, g1, b1] = [0, x, c];
  else if (h < 300) [r1, g1, b1] = [x, 0, c];
  else [r1, g1, b1] = [c, 0, x];
  return [
    Math.round((r1 + m) * 255),
    Math.round((g1 + m) * 255),
    Math.round((b1 + m) * 255),
  ];
}

function parseColorRgb(color: string): [number, number, number] {
  const hex = color.trim().match(/^#([0-9a-fA-F]{6})$/);
  if (hex) {
    return [
      parseInt(hex[1].slice(0, 2), 16),
      parseInt(hex[1].slice(2, 4), 16),
      parseInt(hex[1].slice(4, 6), 16),
    ];
  }
  const hsl = color
    .trim()
    .match(/^hsl\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)%\s*,\s*(\d+(?:\.\d+)?)%\s*\)$/);
  if (hsl) {
    return hslToRgb(Number(hsl[1]), Number(hsl[2]), Number(hsl[3]));
  }
  throw new Error(`Unsupported color token: ${color}`);
}

function compositeOver(
  fg: [number, number, number],
  alpha: number,
  bg: [number, number, number],
): [number, number, number] {
  return [
    alpha * fg[0] + (1 - alpha) * bg[0],
    alpha * fg[1] + (1 - alpha) * bg[1],
    alpha * fg[2] + (1 - alpha) * bg[2],
  ];
}

function contrastRatioRgb(
  rgb1: [number, number, number],
  rgb2: [number, number, number],
): number {
  const l1 =
    0.2126 * srgbToLinear(rgb1[0]) +
    0.7152 * srgbToLinear(rgb1[1]) +
    0.0722 * srgbToLinear(rgb1[2]);
  const l2 =
    0.2126 * srgbToLinear(rgb2[0]) +
    0.7152 * srgbToLinear(rgb2[1]) +
    0.0722 * srgbToLinear(rgb2[2]);
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

function contrastRatio(fg: string, bg: string): number {
  return contrastRatioRgb(parseColorRgb(fg), parseColorRgb(bg));
}

describe('Accessibility & WCAG 2.1 AA contrast compliance (web/frontend)', () => {
  it('ensures semantic text ink tokens and secondary ink achieve >= 4.5:1 on Obsidian Light surfaces', () => {
    const surfaces = ['#FFFFFF', '#F5F5F7', '#F2F2F7'];
    const inkTokens = [
      '#1D1D1F',
      '#52525B',
      '#6E6E73',
      'hsl(4, 76%, 38%)',
      'hsl(135, 62%, 26%)',
      'hsl(35, 95%, 27%)',
      'hsl(210, 100%, 38%)',
    ];
    for (const fg of inkTokens) {
      for (const bg of surfaces) {
        expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it('removes low-contrast #86868B and raw status hexes from small-text foreground declarations', () => {
    const checkedFiles = [
      '_variables.scss',
      'styles.scss',
      'app/auth/pending-approval.page.ts',
      'app/auth/user-menu.component.ts',
      'app/components/ChatNavPanel/screens/AgentScreen/agent-chat-composer/agent-chat-composer.component.scss',
      'app/components/ChatNavPanel/screens/AgentScreen/agent-chat-thread/agent-chat-thread.component.scss',
      'app/components/ChatNavPanel/screens/AgentScreen/agent-screen.scss',
      'app/components/ChatNavPanel/screens/Log/Log.scss',
      'app/components/ChatNavPanel/screens/Log/simulation-report-card.component.ts',
      'app/components/ChatNavPanel/screens/Log/simulation-timeline-card.component.ts',
      'app/components/ChatNavPanel/screens/Log/stakeholder-graph-card.component.ts',
      'app/components/ChatNavPanel/screens/Log/stakeholder-interview-card.component.ts',
      'app/components/ChatNavPanel/screens/Log/stakeholder-personas-card.component.ts',
      'app/components/ChatNavPanel/screens/Log/stakeholder-psychology-card.component.ts',
      'app/components/ChatNavPanel/screens/Log/stakeholder-reactions-card.component.ts',
      'app/components/ChatNavPanel/screens/Log/translation-diagnostic-card.component.ts',
      'app/components/a2ui/a2-ui-controller.scss',
      'app/hud/hud.scss',
      'app/memory/components/documents-panel.component.ts',
      'app/memory/pages/analyses-list.page.ts',
      'app/memory/pages/analysis-detail.page.ts',
      'app/settings/ai-engine/ai-engine.page.ts',
      'app/settings/profile/profile.page.ts',
      'app/usage/usage-tile.component.ts',
    ];

    for (const rel of checkedFiles) {
      const content = readFileSync(resolve(SRC_DIR, rel), 'utf-8');
      expect(content).not.toMatch(/(?<![\w-])color:\s*#86868B\b/i);
      expect(content).not.toMatch(/(?<![\w-])color:\s*#(?:34C759|FF9500|FF3B30)\b/i);
      expect(content).not.toMatch(/--_text-color:\s*#(?:34C759|FF9500|FF3B30)\b/i);
    }

    const stylesScss = readFileSync(resolve(SRC_DIR, 'styles.scss'), 'utf-8');
    expect(stylesScss).toContain('@media (forced-colors: active)');
    expect(stylesScss).not.toMatch(/\.apple-btn-destructive\s*\{[^}]*background:\s*#FF3B30/i);
  });

  it('uses >= 4.5:1 accessible ink colors in StakeholderPsychologyCardComponent helpers', () => {
    const comp = new StakeholderPsychologyCardComponent();
    expect(contrastRatio(comp.wttColor(85), '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.wttColor(55), '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.wttColor(20), '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.trendColor('rising'), '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.trendColor('declining'), '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.trendColor('flat'), '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.riskStyle('high').color, '#FFFFFF')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.riskStyle('medium').color, '#FFFFFF')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(comp.riskStyle('low').color, '#FFFFFF')).toBeGreaterThanOrEqual(4.5);

    const logTs = readFileSync(
      resolve(SRC_DIR, 'app/components/ChatNavPanel/screens/Log/log.component.ts'),
      'utf-8',
    );
    expect(logTs).not.toContain('hashToHsl');
    expect(logTs).toContain('hashToMonochromeToken');
  });

  it('prevents < 4.5:1 small-text regressions in translation-diagnostic-card, login page, and analyses-list delete button', () => {
    const diagCard = readFileSync(
      resolve(SRC_DIR, 'app/components/ChatNavPanel/screens/Log/translation-diagnostic-card.component.ts'),
      'utf-8',
    );
    const tickBlock = diagCard.match(/\.trigger-table__tick\s*\{[^}]*\}/);
    expect(tickBlock).not.toBeNull();
    expect(tickBlock![0]).not.toMatch(/color:\s*#0071E3\b/i);
    expect(tickBlock![0]).toMatch(/color:\s*hsl\(210,\s*100%,\s*38%\)/);
    expect(contrastRatio('hsl(210, 100%, 38%)', '#F5F5F7')).toBeGreaterThanOrEqual(4.5);

    const loginHtml = readFileSync(resolve(SRC_DIR, '../public/login/index.html'), 'utf-8');
    expect(loginHtml).not.toMatch(/--blue-hover:\s*#0077ED\b/i);
    expect(loginHtml).not.toMatch(/\.toggle-link:hover\s*\{[^}]*#0077ED\b/i);
    expect(loginHtml).toMatch(/--blue-hover:\s*hsl\(210,\s*100%,\s*38%\)/);
    expect(contrastRatio('hsl(210, 100%, 38%)', '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio('hsl(210, 100%, 38%)', '#FFFFFF')).toBeGreaterThanOrEqual(4.5);

    const analysesList = readFileSync(
      resolve(SRC_DIR, 'app/memory/pages/analyses-list.page.ts'),
      'utf-8',
    );
    const deleteBlock = analysesList.match(/\.analyses__card-delete\s*\{[^}]*\}/);
    expect(deleteBlock).not.toBeNull();
    expect(deleteBlock![0]).not.toMatch(/opacity\s*:\s*0\.\d+/);
    expect(deleteBlock![0]).toMatch(/color:\s*hsl\(4,\s*76%,\s*38%\)/);
    expect(contrastRatio('hsl(4, 76%, 38%)', '#FFFFFF')).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio('hsl(4, 76%, 38%)', '#F5F5F7')).toBeGreaterThanOrEqual(4.5);
  });

  it('ensures Smoked Titanium thead th and .mirror-topline text achieve >= 4.5:1 worst-case contrast', () => {
    const canvasRgb = parseColorRgb('#F5F5F7');
    const whiteRgb = parseColorRgb('#FFFFFF');
    const smokedRgb = parseColorRgb('#1D1D1F');
    const tableCardMinBg = compositeOver(whiteRgb, 0.56, canvasRgb);
    const smokedTableWorstBg = compositeOver(smokedRgb, 0.12, tableCardMinBg);
    const glassCardMinBg = compositeOver(whiteRgb, 0.68, canvasRgb);
    const smokedToplineWorstBg = compositeOver(smokedRgb, 0.12, glassCardMinBg);

    const stylesScss = readFileSync(resolve(SRC_DIR, 'styles.scss'), 'utf-8');
    const theadThBlock = stylesScss.match(/\.apple-table\s+thead\s+th\s*\{[^}]*\}/);
    expect(theadThBlock).not.toBeNull();
    const theadColorMatch = theadThBlock![0].match(/color:\s*([^;]+);/);
    expect(theadColorMatch).not.toBeNull();
    expect(theadColorMatch![1].trim().toUpperCase()).not.toBe('#6E6E73');
    expect(
      contrastRatioRgb(parseColorRgb(theadColorMatch![1]), smokedTableWorstBg),
    ).toBeGreaterThanOrEqual(4.5);

    const toplineChecks: Array<{ file: string; selectorRe: RegExp }> = [
      { file: 'app/auth/pending-approval.page.ts', selectorRe: /\.approval-eyebrow\s*\{[^}]*\}/ },
      { file: 'app/settings/billing/billing.page.ts', selectorRe: /\.billing-page__current-label\s*\{[^}]*\}/ },
      { file: 'app/shared/obsidian/apple-info-popover.component.ts', selectorRe: /\.apple-popover-panel__subtitle\s*\{[^}]*\}/ },
      { file: 'app/usage/upgrade-modal.component.ts', selectorRe: /\.upgrade-modal__close\s*\{[^}]*\}/ },
    ];
    for (const check of toplineChecks) {
      const content = readFileSync(resolve(SRC_DIR, check.file), 'utf-8');
      const block = content.match(check.selectorRe);
      expect(block, `Missing rule in ${check.file}`).not.toBeNull();
      const colorMatch = block![0].match(/color:\s*([^;]+);/);
      expect(colorMatch, `Missing color in ${check.file}`).not.toBeNull();
      expect(colorMatch![1].trim().toUpperCase()).not.toBe('#6E6E73');
      expect(
        contrastRatioRgb(parseColorRgb(colorMatch![1]), smokedToplineWorstBg),
      ).toBeGreaterThanOrEqual(4.5);
    }
  });
});
