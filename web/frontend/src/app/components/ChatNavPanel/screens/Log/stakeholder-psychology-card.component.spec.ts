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
import { describe, expect, it } from 'vitest';
import { StakeholderPsychologyCardComponent } from './stakeholder-psychology-card.component';

describe('StakeholderPsychologyCardComponent', () => {
  it('parses advocacy_actions and escalation_events correctly', () => {
    const comp = new StakeholderPsychologyCardComponent();
    comp.payload = {
      stakeholder_psychology: {
        profiles: [
          {
            stakeholder_id: 'vp_operations',
            stakeholder_label: 'vp_operations',
            tier: 1,
            wtt_score: 82,
            wtt_breakdown: {
              trust_level: 80,
              resistance_absence: 85,
              principle_engagement: 90,
              trust_integrity: 100,
            },
            sentiment_trajectory: {
              initial: 0.1,
              final: 0.8,
              min: 0.1,
              max: 0.8,
              max_delta_tick: 2,
              trend: 'rising',
            },
            coalition_allies: ['cfo (0.8)', 'data_scientist_lead'],
            advocacy_actions: ['VP Operations champions the initiative internally'],
            escalation_events: ['VP Operations demands emergency intervention on core domain principles'],
          },
        ],
        summary: 'All stakeholders receptive.',
      },
    };

    expect(comp.hasValidPsychology).toBe(true);
    expect(comp.parsed).not.toBeNull();
    const p = comp.parsed!.profiles[0];
    expect(p.stakeholder_label).toBe('VP Operations');
    expect(p.coalition_allies).toEqual(['CFO', 'Data Scientist Lead']);
    expect(p.advocacy_actions).toEqual(['VP Operations champions the initiative internally']);
    expect(p.escalation_events).toEqual(['VP Operations demands emergency intervention on core domain principles']);
  });

  it('diverts legacy champion or aligned events in escalation_events to advocacy_actions', () => {
    const comp = new StakeholderPsychologyCardComponent();
    comp.payload = {
      profiles: [
        {
          stakeholder_id: 'cfo',
          tier: 1,
          wtt_score: 75,
          escalation_events: [
            'CFO champions the initiative internally',
            'CFO is now fully aligned with leadership',
            'CFO escalates concerns to external stakeholders',
          ],
        },
      ],
    };

    expect(comp.hasValidPsychology).toBe(true);
    const p = comp.parsed!.profiles[0];
    expect(p.advocacy_actions).toEqual([
      'CFO champions the initiative internally',
      'CFO is now fully aligned with leadership',
    ]);
    expect(p.escalation_events).toEqual([
      'CFO escalates concerns to external stakeholders',
    ]);
  });

  it('maps legacy emergency fallback schema to standard frontend types', () => {
    const comp = new StakeholderPsychologyCardComponent();
    comp.payload = {
      stakeholder_psychology: {
        profiles: [
          {
            stakeholder_id: 'cfo',
            tier: 1,
            wtt_score: 75,
            wtt_breakdown: {
              sentiment_contribution: 25,
              resistance_penalty: 15,
              principle_engagement: 30,
              trust_penalty: 0,
            },
            sentiment_trajectory: {
              start: 0.0,
              end: 0.5,
              delta: 0.5,
              trend: 'improving',
            },
            coalition_allies: [],
          },
        ],
      },
    };

    expect(comp.hasValidPsychology).toBe(true);
    const p = comp.parsed!.profiles[0];
    expect(p.stakeholder_label).toBe('CFO');
    expect(p.wtt_breakdown.trust_level).toBe(25);
    expect(p.wtt_breakdown.resistance_absence).toBe(85);
    expect(p.wtt_breakdown.trust_integrity).toBe(100);
    expect(p.sentiment_trajectory.initial).toBe(0.0);
    expect(p.sentiment_trajectory.final).toBe(0.5);
    expect(p.sentiment_trajectory.trend).toBe('rising');
  });

  it('formats acronyms and roles consistently', () => {
    const comp = new StakeholderPsychologyCardComponent();
    expect(comp.formatRoleDisplay('vp_operations')).toBe('VP Operations');
    expect(comp.formatRoleDisplay('cfo')).toBe('CFO');
    expect(comp.formatRoleDisplay('data_scientist_lead')).toBe('Data Scientist Lead');
    expect(comp.formatRoleDisplay('head_of_ai')).toBe('Head of AI');
    expect(comp.formatRoleDisplay('director_of_operations')).toBe('Director of Operations');
    expect(comp.formatRoleDisplay('svp_cbo')).toBe('SVP CBO');
    expect(comp.formatRoleDisplay('evp_cro')).toBe('EVP CRO');
  });

  it('cleans embedded snake_case slugs and deduplicates events in frontend', () => {
    const comp = new StakeholderPsychologyCardComponent();
    comp.payload = {
      profiles: [
        {
          stakeholder_id: 'cfo',
          tier: 1,
          wtt_score: 80,
          wtt_breakdown: {
            trust_level: 150, // exceeds 100, should clamp
            resistance_absence: -20, // negative, should clamp
            principle_engagement: 80,
            trust_integrity: 100,
          },
          advocacy_actions: [
            'CFO champions the initiative with vp_operations',
            'CFO champions the initiative with vp_operations', // duplicate
          ],
          escalation_events: [
            'CFO escalates concerns with data_scientist_lead',
            'CFO escalates concerns with data_scientist_lead', // duplicate
            'CFO champions the initiative with head_of_ai', // legacy champion in escalations
          ],
        },
      ],
    };

    expect(comp.hasValidPsychology).toBe(true);
    const p = comp.parsed!.profiles[0];
    // Check slug cleaning and deduplication
    expect(p.advocacy_actions).toEqual([
      'CFO champions the initiative with VP Operations',
      'CFO champions the initiative with Head of AI',
    ]);
    expect(p.escalation_events).toEqual([
      'CFO escalates concerns with Data Scientist Lead',
    ]);

    // Check clamping of bars
    const bars = comp.wttBars(p.wtt_breakdown);
    expect(bars[0].value).toBe(100);
    expect(bars[1].value).toBe(0);
  });
});
