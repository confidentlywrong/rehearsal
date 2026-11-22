/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: remove synthetic demo routes, mocked initial chat messages, and static route bundles for 100% SSOT.
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

type DemoConfigType = Record<
  string,
  {
    title: string;
    agent: string;
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

export const DEMO_CONFIG: DemoConfigType = {
  Sandbox: {
    title: 'Sandbox',
    agent: 'leadership_planner_with_memory',
    orbitCamera: false,
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
