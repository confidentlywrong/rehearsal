/**
 * Copyright 2026 Google LLC
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

import cacheRoute01 from '../../cached_routes/cache-1.json';
import cacheRoute02 from '../../cached_routes/cache-2.json';
import cacheRoute03 from '../../cached_routes/cache-3.json';

export const PRECONFIGURED_ROUTES: Record<string, unknown> = {
  cachedRoute01: cacheRoute01,
  cachedRoute02: cacheRoute02,
  cachedRoute03: cacheRoute03,
};

type DemoConfigType = Record<
  string,
  {
    title: string;
    agent: string;
    placeholderRoutes?: keyof typeof PRECONFIGURED_ROUTES;
    placeholderAgentMessage?: any;
    isSecurityDemo?: boolean;
    isBuildAgentsDemo?: boolean;
    isIntentToInfrastructureDemo?: boolean;
    promptPlaceholder?: string;
    orbitCamera?: boolean;
    recordingConfig?: {
      cachedMessageStreams?: string[];
      timeScale?: number;
    };
  }
>;

/**
 * Canonical three-card A2UI layout for simulation results.
 * Matches the schema defined in agents/leadership_planner_with_eval/prompts.py.
 * Used by all demo configurations that display sim_results.
 */
const LEADERSHIP_SIM_RESULTS_COMPONENTS = [
  // ── Card 1: Main Results ──────────────────────────────────────────
  { id: 'tag-main', component: { Text: { text: { literalString: 'SIMULATED' }, usageHint: 'label' } } },
  { id: 'sim-meta-main', component: { Text: { text: { literalString: '#0042' }, usageHint: 'caption' } } },
  { id: 'tag-row-main', component: { Row: { children: { explicitList: ['tag-main', 'sim-meta-main'] } } } },
  { id: 'title-main', component: { Text: { text: { literalString: 'AI Transformation Leadership' }, usageHint: 'h2' } } },
  { id: 'left-col-main', component: { Column: { children: { explicitList: ['tag-row-main', 'title-main'] } } } },
  { id: 'score-num-main', component: { Text: { text: { literalString: '75' }, usageHint: 'h1' } } },
  { id: 'score-lbl-main', component: { Text: { text: { literalString: 'Score' }, usageHint: 'caption' } } },
  { id: 'score-col-main', component: { Column: { children: { explicitList: ['score-num-main', 'score-lbl-main'] } } } },
  { id: 'header-main', component: { Row: { children: { explicitList: ['left-col-main', 'score-col-main'] } } } },
  { id: 'stake-l-main', component: { Text: { text: { literalString: 'Stakeholders (expected/simulated)' }, usageHint: 'body' } } },
  { id: 'stake-v-main', component: { Text: { text: { literalString: '10,000/1,000' }, usageHint: 'body' } } },
  { id: 'stake-r-main', component: { Row: { children: { explicitList: ['stake-l-main', 'stake-v-main'] } } } },
  { id: 'd1-main', component: { Divider: {} } },
  { id: 'sa-l-main', component: { Text: { text: { literalString: 'Stakeholder Alignment' }, usageHint: 'body' } } },
  { id: 'sa-v-main', component: { Text: { text: { literalString: '80' }, usageHint: 'body' } } },
  { id: 'sa-r-main', component: { Row: { children: { explicitList: ['sa-l-main', 'sa-v-main'] } } } },
  { id: 'dq-l-main', component: { Text: { text: { literalString: 'Decision Quality' }, usageHint: 'body' } } },
  { id: 'dq-v-main', component: { Text: { text: { literalString: '75' }, usageHint: 'body' } } },
  { id: 'dq-r-main', component: { Row: { children: { explicitList: ['dq-l-main', 'dq-v-main'] } } } },
  { id: 'cr-l-main', component: { Text: { text: { literalString: 'Crisis Readiness' }, usageHint: 'body' } } },
  { id: 'cr-v-main', component: { Text: { text: { literalString: '70' }, usageHint: 'body' } } },
  { id: 'cr-r-main', component: { Row: { children: { explicitList: ['cr-l-main', 'cr-v-main'] } } } },
  { id: 'd2-main', component: { Divider: {} } },
  { id: 'rerun-txt-main', component: { Text: { text: { literalString: 'Re-run Simulation' } } } },
  { id: 'rerun-btn-main', component: { Button: { child: 'rerun-txt-main', action: { name: 'run_simulation' }, primary: { literalBoolean: true } } } },
  { id: 'main-content', component: { Column: { children: { explicitList: ['header-main', 'stake-r-main', 'd1-main', 'sa-r-main', 'dq-r-main', 'cr-r-main', 'd2-main', 'rerun-btn-main'] } } } },
  { id: 'card-main', component: { Card: { child: 'main-content' } } },

  // ── Card 2: Strategy Overview ─────────────────────────────────────
  { id: 'strategy-title', component: { Text: { text: { literalString: 'Strategy Overview (Persona Breakdown)' }, usageHint: 'h2' } } },
  { id: 'vis-lbl', component: { Text: { text: { literalString: 'Visionary' }, usageHint: 'body' } } },
  { id: 'vis-val', component: { Text: { text: { literalString: '90% Aligned' }, usageHint: 'body' } } },
  { id: 'vis-row', component: { Row: { children: { explicitList: ['vis-lbl', 'vis-val'] } } } },
  { id: 'dip-lbl', component: { Text: { text: { literalString: 'Diplomat' }, usageHint: 'body' } } },
  { id: 'dip-val', component: { Text: { text: { literalString: '80% Aligned' }, usageHint: 'body' } } },
  { id: 'dip-row', component: { Row: { children: { explicitList: ['dip-lbl', 'dip-val'] } } } },
  { id: 'prg-lbl', component: { Text: { text: { literalString: 'Pragmatist' }, usageHint: 'body' } } },
  { id: 'prg-val', component: { Text: { text: { literalString: '60% Aligned' }, usageHint: 'body' } } },
  { id: 'prg-row', component: { Row: { children: { explicitList: ['prg-lbl', 'prg-val'] } } } },
  { id: 'dis-lbl', component: { Text: { text: { literalString: 'Disruptor' }, usageHint: 'body' } } },
  { id: 'dis-val', component: { Text: { text: { literalString: '40% Aligned' }, usageHint: 'body' } } },
  { id: 'dis-row', component: { Row: { children: { explicitList: ['dis-lbl', 'dis-val'] } } } },
  { id: 'd-strategy', component: { Divider: {} } },
  { id: 'winning-lbl', component: { Text: { text: { literalString: 'Winning Strategy' }, usageHint: 'caption' } } },
  { id: 'winning-desc', component: { Text: { text: { literalString: 'Visionary: Highly effective at establishing urgency and Coalition buy-in.' }, usageHint: 'body' } } },
  { id: 'strategy-content', component: { Column: { children: { explicitList: ['strategy-title', 'vis-row', 'dip-row', 'prg-row', 'dis-row', 'd-strategy', 'winning-lbl', 'winning-desc'] } } } },
  { id: 'card-strategy', component: { Card: { child: 'strategy-content' } } },

  // ── Card 3: Coaching Insights ─────────────────────────────────────
  { id: 'coach-title-card', component: { Text: { text: { literalString: 'Coaching Insights' }, usageHint: 'h2' } } },
  { id: 'c1-title', component: { Text: { text: { literalString: 'Kotter: Strengthen urgency framing' }, usageHint: 'body' } } },
  { id: 'c1-fw', component: { Text: { text: { literalString: 'KOTTER' }, usageHint: 'caption' } } },
  { id: 'c1-row', component: { Row: { children: { explicitList: ['c1-title', 'c1-fw'] } } } },
  { id: 'c1-desc', component: { Text: { text: { literalString: 'Early defections suggest weak urgency. Articulate the competitive threat first.' }, usageHint: 'body' } } },
  { id: 'c2-title', component: { Text: { text: { literalString: 'ADKAR: Close the Ability gap' }, usageHint: 'body' } } },
  { id: 'c2-fw', component: { Text: { text: { literalString: 'ADKAR' }, usageHint: 'caption' } } },
  { id: 'c2-row', component: { Row: { children: { explicitList: ['c2-title', 'c2-fw'] } } } },
  { id: 'c2-desc', component: { Text: { text: { literalString: 'Resource drain without buy-in means stakeholders lacked the tools to act. Articulate the training plan.' }, usageHint: 'body' } } },
  { id: 'c3-title', component: { Text: { text: { literalString: 'Synthesis' }, usageHint: 'body' } } },
  { id: 'c3-fw', component: { Text: { text: { literalString: 'TAKEAWAY' }, usageHint: 'caption' } } },
  { id: 'c3-row', component: { Row: { children: { explicitList: ['c3-title', 'c3-fw'] } } } },
  { id: 'c3-desc', component: { Text: { text: { literalString: 'Combine urgency (Kotter) with a concrete ability plan (ADKAR) in your next pitch.' }, usageHint: 'body' } } },
  { id: 'coach-content', component: { Column: { children: { explicitList: ['coach-title-card', 'c1-row', 'c1-desc', 'c2-row', 'c2-desc', 'c3-row', 'c3-desc'] } } } },
  { id: 'card-coaching', component: { Card: { child: 'coach-content' } } },

  // ── Root container ────────────────────────────────────────────────
  { id: 'root-col', component: { Column: { children: { explicitList: ['card-main', 'card-strategy', 'card-coaching'] } } } },
];

export const DEMO_CONFIG: DemoConfigType = {
  Sandbox: {
    title: 'Sandbox',
    agent: 'leadership_planner_with_memory',
    orbitCamera: false,
    placeholderAgentMessage: {
      guid: '934d9c7e-5203-4dd8-b31a-590e1bfc3f2f',
      speaker: 'leadership_planner_with_memory (934d9c)',
      emotion: '',
      isUser: false,
      timestamp: '2026-03-19T20:55:01.098Z',
      color: '#6bcb77',
      msgType: 'tool_end',
      icon: 'build',
      toolName: 'validate_and_emit_a2ui',
      text: 'a2ui',
      rawJson:
        '{"a2ui":{"surfaceUpdate":{"surfaceId":"marathon-dashboard","components":[{"id":"h1","component":{"Text":{"text":{"literalString":"Vegas Strip Marathon Plan"},"usageHint":"h3"}}},{"id":"d1","component":{"Divider":{}}},{"id":"t1","component":{"Text":{"text":{"literalString":"Theme: Neon Strip Run"},"usageHint":"body"}}},{"id":"t2","component":{"Text":{"text":{"literalString":"Date: November 17, 2024"},"usageHint":"body"}}},{"id":"t3","component":{"Text":{"text":{"literalString":"Distance: 26.2 Miles"},"usageHint":"body"}}},{"id":"t4","component":{"Text":{"text":{"literalString":"Participants: 10,000 Runners"},"usageHint":"body"}}},{"id":"t5","component":{"Text":{"text":{"literalString":"Budget: $1.2M (Estimated)"},"usageHint":"body"}}},{"id":"d2","component":{"Divider":{}}},{"id":"h2","component":{"Text":{"text":{"literalString":"Evaluation Metrics"},"usageHint":"h3"}}},{"id":"m1","component":{"Text":{"text":{"literalString":"Plan Quality: 0.1 (Status: Needs Improvement)"},"usageHint":"body"}}},{"id":"m2","component":{"Text":{"text":{"literalString":"Distance Compliance: 1.0 (Status: Pass)"},"usageHint":"body"}}},{"id":"d3","component":{"Divider":{}}},{"id":"btn-text","component":{"Text":{"text":{"literalString":"Run Simulation"}}}},{"id":"btn1","component":{"Button":{"child":"btn-text","action":{"name":"run_simulation"},"primary":{"literalBoolean":true}}}},{"id":"col1","component":{"Column":{"children":{"explicitList":["h1","d1","t1","t2","t3","t4","t5","d2","h2","m1","m2","d3","btn1"]}}}},{"id":"card1","component":{"Card":{"child":"col1"}}}]}}}',
      result: {
        surfaceUpdate: {
          surfaceId: 'sim_results',
          components: LEADERSHIP_SIM_RESULTS_COMPONENTS,
        },
      },
    },
    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-ci-log.ndjson'],
      timeScale: 1,
    },
  },
  '1': {
    title: 'Build Leadership Agents',
    agent: 'leadership_planner',
    orbitCamera: true,
    promptPlaceholder: 'Plan a leadership simulation for 10,000 stakeholders in an AI-powered enterprise transformation',
    isBuildAgentsDemo: true,
    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-1-log.ndjson'],
      timeScale: 2,
    },
  },
  '2': {
    title: 'Multi-Stakeholder Evaluation',
    agent: 'leadership_planner_with_eval',
    orbitCamera: false,
    promptPlaceholder: 'Plan a leadership simulation for 10,000 stakeholders in an AI-powered enterprise transformation',

    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-2-log.ndjson', '/assets/sim-2-run-log.ndjson'],
      timeScale: 2,
    },
  },
  '3': {
    title: 'Organizational Memory & Learning',
    agent: 'leadership_planner_with_memory',
    orbitCamera: false,
    promptPlaceholder: 'Plan a leadership simulation for 10,000 stakeholders in an AI-powered enterprise transformation',

    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-3-log.ndjson', '/assets/sim-3-run-log.ndjson'],
      timeScale: 6,
    },
  },
  '4': {
    title: 'Debugging at scale',
    agent: 'simulator_with_failure',
    orbitCamera: true,

    placeholderAgentMessage: {
      guid: '934d9c7e-5203-4dd8-b31a-590e1bfc3f2f',
      speaker: 'leadership_planner_with_memory (934d9c)',
      emotion: '',
      isUser: false,
      timestamp: '2026-03-19T20:55:01.098Z',
      color: '#6bcb77',
      msgType: 'tool_end',
      icon: 'build',
      toolName: 'validate_and_emit_a2ui',
      text: 'a2ui',
      rawJson:
        '{"a2ui":{"surfaceUpdate":{"surfaceId":"marathon-dashboard","components":[{"id":"h1","component":{"Text":{"text":{"literalString":"Vegas Strip Marathon Plan"},"usageHint":"h3"}}},{"id":"d1","component":{"Divider":{}}},{"id":"t1","component":{"Text":{"text":{"literalString":"Theme: Neon Strip Run"},"usageHint":"body"}}},{"id":"t2","component":{"Text":{"text":{"literalString":"Date: November 17, 2024"},"usageHint":"body"}}},{"id":"t3","component":{"Text":{"text":{"literalString":"Distance: 26.2 Miles"},"usageHint":"body"}}},{"id":"t4","component":{"Text":{"text":{"literalString":"Participants: 10,000 Runners"},"usageHint":"body"}}},{"id":"t5","component":{"Text":{"text":{"literalString":"Budget: $1.2M (Estimated)"},"usageHint":"body"}}},{"id":"d2","component":{"Divider":{}}},{"id":"h2","component":{"Text":{"text":{"literalString":"Evaluation Metrics"},"usageHint":"h3"}}},{"id":"m1","component":{"Text":{"text":{"literalString":"Plan Quality: 0.1 (Status: Needs Improvement)"},"usageHint":"body"}}},{"id":"m2","component":{"Text":{"text":{"literalString":"Distance Compliance: 1.0 (Status: Pass)"},"usageHint":"body"}}},{"id":"d3","component":{"Divider":{}}},{"id":"btn-text","component":{"Text":{"text":{"literalString":"Run Simulation"}}}},{"id":"btn1","component":{"Button":{"child":"btn-text","action":{"name":"run_simulation"},"primary":{"literalBoolean":true}}}},{"id":"col1","component":{"Column":{"children":{"explicitList":["h1","d1","t1","t2","t3","t4","t5","d2","h2","m1","m2","d3","btn1"]}}}},{"id":"card1","component":{"Card":{"child":"col1"}}}]}}}',

      result: {
        surfaceUpdate: {
          surfaceId: 'sim_results',
          components: LEADERSHIP_SIM_RESULTS_COMPONENTS,
        },
      },
    },
    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-4-log.ndjson'],
      timeScale: 1.5,
    },
  },
  '5a': {
    title: 'Intent to infrastructure with Gemini Cloud Assist',
    isIntentToInfrastructureDemo: true,
    agent: 'simulator_with_failure',
    orbitCamera: true,
    placeholderAgentMessage: {
      guid: '934d9c7e-5203-4dd8-b31a-590e1bfc3f2f',
      speaker: 'leadership_planner_with_memory (934d9c)',
      emotion: '',
      isUser: false,
      timestamp: '2026-03-19T20:55:01.098Z',
      color: '#6bcb77',
      msgType: 'tool_end',
      icon: 'build',
      toolName: 'validate_and_emit_a2ui',
      text: 'a2ui',
      rawJson:
        '{"a2ui":{"surfaceUpdate":{"surfaceId":"marathon-dashboard","components":[{"id":"h1","component":{"Text":{"text":{"literalString":"Vegas Strip Marathon Plan"},"usageHint":"h3"}}},{"id":"d1","component":{"Divider":{}}},{"id":"t1","component":{"Text":{"text":{"literalString":"Theme: Neon Strip Run"},"usageHint":"body"}}},{"id":"t2","component":{"Text":{"text":{"literalString":"Date: November 17, 2024"},"usageHint":"body"}}},{"id":"t3","component":{"Text":{"text":{"literalString":"Distance: 26.2 Miles"},"usageHint":"body"}}},{"id":"t4","component":{"Text":{"text":{"literalString":"Participants: 10,000 Runners"},"usageHint":"body"}}},{"id":"t5","component":{"Text":{"text":{"literalString":"Budget: $1.2M (Estimated)"},"usageHint":"body"}}},{"id":"d2","component":{"Divider":{}}},{"id":"h2","component":{"Text":{"text":{"literalString":"Evaluation Metrics"},"usageHint":"h3"}}},{"id":"m1","component":{"Text":{"text":{"literalString":"Plan Quality: 0.1 (Status: Needs Improvement)"},"usageHint":"body"}}},{"id":"m2","component":{"Text":{"text":{"literalString":"Distance Compliance: 1.0 (Status: Pass)"},"usageHint":"body"}}},{"id":"d3","component":{"Divider":{}}},{"id":"btn-text","component":{"Text":{"text":{"literalString":"Run Simulation"}}}},{"id":"btn1","component":{"Button":{"child":"btn-text","action":{"name":"run_simulation"},"primary":{"literalBoolean":true}}}},{"id":"col1","component":{"Column":{"children":{"explicitList":["h1","d1","t1","t2","t3","t4","t5","d2","h2","m1","m2","d3","btn1"]}}}},{"id":"card1","component":{"Card":{"child":"col1"}}}]}}}',
      result: {
        surfaceUpdate: {
          surfaceId: 'sim_results',
          components: LEADERSHIP_SIM_RESULTS_COMPONENTS,
        },
      },
    },
    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-5a-log.ndjson'],
      timeScale: 3,
    },
  },
  '5b': {
    title: 'Intent to infrastructure with Gemini Cloud Assist (Upgraded)',
    isIntentToInfrastructureDemo: true,
    agent: 'leadership_planner_with_memory',
    orbitCamera: true,

    placeholderAgentMessage: {
      guid: '934d9c7e-5203-4dd8-b31a-590e1bfc3f2f',
      speaker: 'leadership_planner_with_memory (934d9c)',
      emotion: '',
      isUser: false,
      timestamp: '2026-03-19T20:55:01.098Z',
      color: '#6bcb77',
      msgType: 'tool_end',
      icon: 'build',
      toolName: 'validate_and_emit_a2ui',
      text: 'a2ui',
      rawJson:
        '{"a2ui":{"surfaceUpdate":{"surfaceId":"marathon-dashboard","components":[{"id":"h1","component":{"Text":{"text":{"literalString":"Vegas Strip Marathon Plan"},"usageHint":"h3"}}},{"id":"d1","component":{"Divider":{}}},{"id":"t1","component":{"Text":{"text":{"literalString":"Theme: Neon Strip Run"},"usageHint":"body"}}},{"id":"t2","component":{"Text":{"text":{"literalString":"Date: November 17, 2024"},"usageHint":"body"}}},{"id":"t3","component":{"Text":{"text":{"literalString":"Distance: 26.2 Miles"},"usageHint":"body"}}},{"id":"t4","component":{"Text":{"text":{"literalString":"Participants: 10,000 Runners"},"usageHint":"body"}}},{"id":"t5","component":{"Text":{"text":{"literalString":"Budget: $1.2M (Estimated)"},"usageHint":"body"}}},{"id":"d2","component":{"Divider":{}}},{"id":"h2","component":{"Text":{"text":{"literalString":"Evaluation Metrics"},"usageHint":"h3"}}},{"id":"m1","component":{"Text":{"text":{"literalString":"Plan Quality: 0.1 (Status: Needs Improvement)"},"usageHint":"body"}}},{"id":"m2","component":{"Text":{"text":{"literalString":"Distance Compliance: 1.0 (Status: Pass)"},"usageHint":"body"}}},{"id":"d3","component":{"Divider":{}}},{"id":"btn-text","component":{"Text":{"text":{"literalString":"Run Simulation"}}}},{"id":"btn1","component":{"Button":{"child":"btn-text","action":{"name":"run_simulation"},"primary":{"literalBoolean":true}}}},{"id":"col1","component":{"Column":{"children":{"explicitList":["h1","d1","t1","t2","t3","t4","t5","d2","h2","m1","m2","d3","btn1"]}}}},{"id":"card1","component":{"Card":{"child":"col1"}}}]}}}',
      result: {
        surfaceUpdate: {
          surfaceId: 'sim_results',
          components: LEADERSHIP_SIM_RESULTS_COMPONENTS,
        },
      },
    },
    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-5b-log.ndjson'],
      timeScale: 3,
    },
  },
  '7a': {
    title: 'Securing agents',
    agent: 'leadership_planner_with_memory',
    orbitCamera: true,
    promptPlaceholder:
      'Can we increase the budget so everyone gets glow sticks and those cool nighttime LED sunglasses?',
    isSecurityDemo: true,
    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-7a-log.ndjson'],
      timeScale: 1.5,
    },
  },
  '7b': {
    title: 'Securing agents (Secure)',
    agent: 'leadership_planner_with_memory',
    orbitCamera: true,
    promptPlaceholder:
      'Can we increase the budget so everyone gets glow sticks and those cool nighttime LED sunglasses?',
    isSecurityDemo: true,
    recordingConfig: {
      cachedMessageStreams: ['/assets/sim-7b-log.ndjson'],
      timeScale: 0.5,
    },
  },
};
