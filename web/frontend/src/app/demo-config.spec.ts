/**
 * Copyright 2026 Christian Haeussinger
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

import { describe, expect, it } from 'vitest';
import { DEMO_CONFIG } from './demo-config';

describe('DEMO_CONFIG (100% SSOT)', () => {
  it('contains no placeholderAgentMessage, placeholderRoutes, or static Vegas demo payloads', () => {
    for (const [id, cfg] of Object.entries(DEMO_CONFIG)) {
      const rec = cfg as unknown as Record<string, unknown>;
      expect(rec['placeholderAgentMessage'], `Demo ${id} must not define placeholderAgentMessage`).toBeUndefined();
      expect(rec['placeholderRoutes'], `Demo ${id} must not define placeholderRoutes`).toBeUndefined();
      expect(rec['chatMessages']).toBeUndefined();
    }
    const serialized = JSON.stringify(DEMO_CONFIG);
    expect(serialized).not.toContain('Vegas Strip Marathon Plan');
    expect(serialized).not.toContain('Neon Strip Run');
    expect(serialized.toLowerCase()).not.toContain('#6bcb77');
  });
});
