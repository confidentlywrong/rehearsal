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
import { A2uiTreeService } from './a2ui-tree.service';
import type { A2uiComponentRecord, A2uiNode } from './a2ui-surface.model';

describe('A2uiTreeService', () => {
  it('preserves component payload when recovering lowercase component types', () => {
    const tree = new A2uiTreeService();
    const node = {
      id: 'sb-1',
      component: {
        scorebar: {
          label: { literalString: 'Trust Score' },
          value: { literalNumber: 84 },
        },
      },
    } as unknown as A2uiNode;

    expect(tree.getType(node)).toBe('ScoreBar');
    expect(tree.getLabel(node)).toBe('Trust Score');
    expect(tree.getValue(node)).toBe(84);
  });

  it('unwraps wrapped { literalBoolean: false } without treating objects as truthy', () => {
    const tree = new A2uiTreeService();
    const buttonNode = {
      id: 'btn-1',
      component: {
        Button: {
          child: 'lbl-1',
          primary: { literalBoolean: false },
        },
      },
    } as unknown as A2uiNode;

    const videoNode = {
      id: 'vid-1',
      component: {
        Video: {
          url: { literalString: 'https://example.com/clip.mp4' },
          autoplay: { literalBoolean: false },
          loop: { literalBoolean: true },
        },
      },
    } as unknown as A2uiNode;

    const dtNode = {
      id: 'dt-1',
      component: {
        DateTimeInput: {
          enableDate: { literalBoolean: false },
          enableTime: { literalBoolean: true },
          value: { literalString: '09:30' },
        },
      },
    } as unknown as A2uiNode;

    expect(tree.isPrimary(buttonNode)).toBe(false);
    expect(tree.isAutoplay(videoNode)).toBe(false);
    expect(tree.isLoop(videoNode)).toBe(true);
    expect(tree.getDateTimeInputType(dtNode)).toBe('time');
    expect(tree.getDateTimeValue(dtNode)).toBe('09:30');
  });

  it('resolves wrapped URLs and blocks javascript:/vbscript:/unsafe data: URLs', () => {
    const tree = new A2uiTreeService();

    const safeImg = {
      id: 'img-1',
      component: { Image: { url: { literalString: 'https://example.com/logo.png' } } },
    } as unknown as A2uiNode;
    const xssImg = {
      id: 'img-2',
      component: { Image: { url: { literalString: 'javascript:alert(1)' } } },
    } as unknown as A2uiNode;
    const htmlDataImg = {
      id: 'img-3',
      component: { Image: { url: 'data:text/html,<script>alert(1)</script>' } },
    } as unknown as A2uiNode;
    const controlCharXss = {
      id: 'img-4',
      component: { Image: { url: 'java\tscript:alert(1)' } },
    } as unknown as A2uiNode;
    const svgDataXss = {
      id: 'img-5',
      component: { Image: { url: 'data:image/svg+xml,<svg onload="alert(1)"/>' } },
    } as unknown as A2uiNode;
    const pngDataSafe = {
      id: 'img-6',
      component: { Image: { url: 'data:image/png;base64,AAAA' } },
    } as unknown as A2uiNode;

    expect(tree.getUrl(safeImg)).toBe('https://example.com/logo.png');
    expect(tree.getUrl(xssImg)).toBe('');
    expect(tree.getUrl(htmlDataImg)).toBe('');
    expect(tree.getUrl(controlCharXss)).toBe('');
    expect(tree.getUrl(svgDataXss)).toBe('');
    expect(tree.getUrl(pngDataSafe)).toBe('data:image/png;base64,AAAA');
    expect(tree.resolveOptionLabel(null)).toBe('');
    expect(tree.resolveOptionLabel({ label: 0 })).toBe('0');
    expect(tree.resolveOptionValue({ value: { literalString: 'opt-a' } })).toBe('opt-a');
  });

  it('terminates safely on cyclic component references in findInnermostCardContaining', () => {
    const tree = new A2uiTreeService();
    const records: A2uiComponentRecord[] = [
      { id: 'card-a', component: { Card: { child: 'row-b' } } },
      { id: 'row-b', component: { Row: { children: { explicitList: ['card-a'] } } } },
    ];
    const map = tree.buildComponentsMap(records);
    const root = map.get('card-a') ?? null;
    expect(tree.findInnermostCardContaining(map, root, 'missing-target')).toBeNull();
    expect(tree.findInnermostCardContaining(map, root, 'row-b')?.id).toBe('card-a');
  });
});
