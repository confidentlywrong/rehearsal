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

import { Injectable } from '@angular/core';
import type { A2uiComponentRecord, A2uiNode } from './a2ui-surface.model';
import { A2UI_VIEW_ACTION_NAMES } from './a2ui-actions.service';

export const A2UI_COMPONENT_TYPES = [
  'Text',
  'Image',
  'Icon',
  'Video',
  'AudioPlayer',
  'Divider',
  'Row',
  'Column',
  'List',
  'Card',
  'Tabs',
  'Modal',
  'Button',
  'CheckBox',
  'TextField',
  'DateTimeInput',
  'MultipleChoice',
  'Slider',
  'ScoreBar',
  // StakeholderGraph: custom leadership component that renders the
  // stakeholder relationship constellation (Actor/Group/Structure/
  // Friction/Belief nodes + edges) via <app-stakeholder-graph-card>.
  'StakeholderGraph',
] as const;

/**
 * Case-insensitive lookup: maps lowercase type name → canonical PascalCase.
 * Used by getType() to recover from agents emitting 'scorebar' or 'scoreBar'.
 */
const A2UI_TYPE_NORMALIZE_MAP = new Map<string, string>(
  A2UI_COMPONENT_TYPES.map((t) => [t.toLowerCase(), t]),
);

@Injectable({ providedIn: 'root' })
export class A2uiTreeService {
  /**
   * Tracks `${kind}:${key}` strings we've already warned about so we don't
   * spam the console on every Angular change-detection pass for a permanently
   * malformed payload. Cleared when the page reloads.
   */
  private readonly warnedKeys = new Set<string>();

  private warnOnce(kind: string, key: string, message: string): void {
    const cacheKey = `${kind}:${key}`;
    if (this.warnedKeys.has(cacheKey)) return;
    this.warnedKeys.add(cacheKey);
    console.warn(`[A2UI] ${message}`);
  }

  buildComponentsMap(components?: A2uiComponentRecord[] | null): Map<string, A2uiNode> {
    const map = new Map<string, A2uiNode>();
    if (!components) return map;
    for (const item of components) {
      if (item?.id) {
        map.set(String(item.id), item as A2uiNode);
      }
    }
    return map;
  }

  findRootComponent(componentsMap: Map<string, A2uiNode>): A2uiNode | null {
    const allIds = new Set(componentsMap.keys());
    const referencedIds = new Set<string>();

    for (const component of componentsMap.values()) {
      const children = this.getChildren(component);
      for (const child of children) {
        if (typeof child === 'string') {
          referencedIds.add(child);
        }
      }
      for (const tab of this.getTabItems(component)) {
        const childId = this.getTabChild(tab as Record<string, unknown>);
        if (childId) {
          referencedIds.add(childId);
        }
      }
      if (this.getType(component) === 'Modal') {
        const data = this.getData(component);
        if (data.entryPointChild) referencedIds.add(String(data.entryPointChild));
        if (data.contentChild) referencedIds.add(String(data.contentChild));
      }
    }

    for (const id of allIds) {
      if (!referencedIds.has(id)) {
        return componentsMap.get(id) ?? null;
      }
    }

    return Array.from(componentsMap.values())[0] ?? null;
  }

  resolveNode(componentsMap: Map<string, A2uiNode>, nodeOrId: unknown): A2uiNode | null {
    if (typeof nodeOrId === 'string') {
      const node = componentsMap.get(nodeOrId);
      if (!node) {
        // Dangling child id — agent referenced a component that doesn't
        // exist in the surface's components array. Warn so it surfaces in
        // devtools instead of failing silently as a blank render slot.
        this.warnOnce(
          'dangling-child',
          nodeOrId,
          `resolveNode: child id '${nodeOrId}' not found in components map.`,
        );
      }
      return node ?? null;
    }
    return (nodeOrId as A2uiNode) ?? null;
  }

  getType(node: A2uiNode | null | undefined): string {
    if (!node) return '';

    if (node.component) {
      const componentKeys = Object.keys(node.component);
      // Exact PascalCase match first
      const typeKey = componentKeys.find((k) =>
        (A2UI_COMPONENT_TYPES as readonly string[]).includes(k),
      );
      if (typeKey) return typeKey;
      // Case-insensitive fallback: try normalizing each key
      for (const k of componentKeys) {
        const normalized = A2UI_TYPE_NORMALIZE_MAP.get(k.toLowerCase());
        if (normalized) {
          this.warnOnce(
            'case-mismatch',
            k,
            `getType: component type '${k}' resolved via case-insensitive match to '${normalized}'. Fix the agent to emit PascalCase.`,
          );
          return normalized;
        }
      }
      const fallback = componentKeys[0] ?? 'Unknown';
      this.warnOnce(
        'unknown-type',
        fallback,
        `getType: component type '${fallback}' not in A2UI_COMPONENT_TYPES registry. Add it to a2ui-tree.service.ts.`,
      );
      return fallback;
    }

    const keys = Object.keys(node);
    // Exact PascalCase match
    const typeKey = keys.find((k) => (A2UI_COMPONENT_TYPES as readonly string[]).includes(k));
    if (typeKey) return typeKey;
    // Case-insensitive fallback
    for (const k of keys) {
      const normalized = A2UI_TYPE_NORMALIZE_MAP.get(k.toLowerCase());
      if (normalized) {
        this.warnOnce(
          'case-mismatch',
          k,
          `getType: component type '${k}' resolved via case-insensitive match to '${normalized}'. Fix the agent to emit PascalCase.`,
        );
        return normalized;
      }
    }
    // No recognized A2UI component type found among the node keys.
    // Return 'Unknown' so the controller rejects non-A2UI payloads
    // (e.g. {"status": "success", ...} from internal tool diagnostics).
    const fallback = 'Unknown';
    this.warnOnce(
      'unknown-type',
      keys.join(','),
      `getType: no recognized component type among keys [${keys.join(', ')}]. Node is not an A2UI component.`,
    );
    return fallback;
  }

  /** Wire-format component payload; intentionally loose for A2UI JSON. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  getData(node: A2uiNode | null | undefined): any {
    if (!node) return {};

    if (node.component) {
      const type = this.getType(node);
      return node.component[type] || {};
    }

    const type = this.getType(node);
    return node[type] || node;
  }

  getId(node: A2uiNode | null | undefined): string {
    if (!node) return '';
    if (node.id) return String(node.id);
    const data = this.getData(node);
    return String(data.id || '');
  }

  getChildren(node: A2uiNode | null | undefined): unknown[] {
    const data = this.getData(node);

    let children = (data.children ?? data.child ?? data.items) as unknown;
    if (!children && data.slots && typeof data.slots === 'object') {
      const slots = data.slots as Record<string, any>;
      children = slots['children'] ?? slots['child'] ?? slots['items'];
    }

    if (!children) return [];
    if (Array.isArray(children)) return children;
    if (
      children &&
      typeof children === 'object' &&
      'explicitList' in (children as Record<string, unknown>)
    ) {
      return ((children as { explicitList: unknown[] }).explicitList ?? []) as unknown[];
    }

    return [children];
  }

  getTabItems(node: A2uiNode | null | undefined): unknown[] {
    const data = this.getData(node);
    return (data.tabItems as unknown[]) || [];
  }

  getTabTitle(tab: Record<string, unknown>): string {
    const title = tab['title'];
    if (!title) return '';
    if (typeof title === 'string') return title;
    if (typeof title === 'object' && title !== null && 'literalString' in title) {
      return String((title as { literalString: unknown }).literalString);
    }
    return String(title);
  }

  getTabChild(tab: Record<string, unknown>): string {
    return String(tab['child'] || '');
  }

  resolveValue(value: unknown): unknown {
    if (value && typeof value === 'object') {
      const o = value as Record<string, any>;
      if ('literalString' in o) return o['literalString'];
      if ('literalNumber' in o) return o['literalNumber'];
      if ('literalBoolean' in o) return o['literalBoolean'];
      if ('literalArray' in o) return o['literalArray'];
      if ('path' in o) return `[${o['path']}]`;
    }
    return value;
  }

  findInnermostCardContaining(
    componentsMap: Map<string, A2uiNode>,
    root: A2uiNode | null | undefined,
    componentId: string,
    n: A2uiNode | null = root ?? null,
    cardStack: A2uiNode[] = [],
  ): A2uiNode | null {
    if (!n || !componentId) return null;
    const type = this.getType(n);
    const stack = type === 'Card' ? [...cardStack, n] : cardStack;
    if (this.getId(n) === componentId) {
      const len = stack.length;
      return len > 0 ? stack[len - 1]! : null;
    }
    if (type === 'Modal') {
      const data = this.getData(n);
      for (const ref of [data.entryPointChild, data.contentChild]) {
        if (!ref) continue;
        const child = this.resolveNode(componentsMap, ref);
        const found = this.findInnermostCardContaining(
          componentsMap,
          root,
          componentId,
          child,
          stack,
        );
        if (found) return found;
      }
      return null;
    }
    if (type === 'Tabs') {
      for (const tab of this.getTabItems(n)) {
        const child = this.resolveNode(componentsMap, this.getTabChild(tab as Record<string, unknown>));
        const found = this.findInnermostCardContaining(
          componentsMap,
          root,
          componentId,
          child,
          stack,
        );
        if (found) return found;
      }
      return null;
    }
    for (const childRef of this.getChildren(n)) {
      const child = this.resolveNode(componentsMap, childRef);
      const found = this.findInnermostCardContaining(
        componentsMap,
        root,
        componentId,
        child,
        stack,
      );
      if (found) return found;
    }
    return null;
  }

  cardExpansionKey(parsedNode: A2uiNode | null, componentsMap: Map<string, A2uiNode>, cardNode: A2uiNode): string {
    const sid = parsedNode?.surfaceUpdate?.surfaceId as string | undefined;
    const cardId = this.getId(cardNode);
    return sid ? `${sid}:${cardId}` : cardId;
  }

  isFinancialRefusalSurface(parsedNode: A2uiNode | null): boolean {
    const sid = parsedNode?.surfaceUpdate?.surfaceId as string | undefined;
    return typeof sid === 'string' && sid.includes('financial_refusal');
  }

  isFinancialUpdateSurface(parsedNode: A2uiNode | null): boolean {
    const sid = parsedNode?.surfaceUpdate?.surfaceId as string | undefined;
    return typeof sid === 'string' && sid.includes('financial_update');
  }

  isRootCardListRoutesSurface(
    parsedNode: A2uiNode | null,
    rootNode: A2uiNode | null,
    componentsMap: Map<string, A2uiNode>,
    cardNode: A2uiNode,
  ): boolean {
    const sid = parsedNode?.surfaceUpdate?.surfaceId as string | undefined;
    if (typeof sid !== 'string' || !sid.includes('route_list')) return false;
    if (!cardNode || !rootNode) return false;
    if (this.getType(rootNode) !== 'Card') return false;
    return this.getId(cardNode) === this.getId(rootNode);
  }

  /**
   * A Card is rendered with a collapsible body when any of these hold:
   *   1. `Card.expandable` literalBoolean is `true` (explicit opt-in), OR
   *   2. its single Column child's first child has id `"header"` / `/^header-/`
   *      (legacy convention — kept for back-compat).
   * In both cases the Card must wrap exactly one Column with ≥ 2 children.
   * The footer is only auto-rendered when the LAST Column child has id
   *   `"footer"` / `/^footer-/`. Otherwise everything after the header is
   *   collapsible body (no auto-rip into a footer slot).
   */
  isExpandableSummaryCard(componentsMap: Map<string, A2uiNode>, node: A2uiNode): boolean {
    if (this.getType(node) !== 'Card') return false;
    const children = this.getChildren(node);
    if (children.length !== 1) return false;
    const col = this.resolveNode(componentsMap, children[0]);
    if (!col || this.getType(col) !== 'Column') return false;
    const colChildren = this.getChildren(col);
    if (colChildren.length < 2) return false;

    // Path 1: explicit opt-in via Card.expandable.
    const cardData = this.getData(node);
    const expandableProp = cardData?.expandable;
    if (expandableProp && typeof expandableProp === 'object'
        && (expandableProp as Record<string, unknown>)['literalBoolean'] === true) {
      return true;
    }

    // Path 2: legacy id-prefix convention (≥ 3 children required to preserve
    // historical layouts that relied on the auto-footer behavior).
    if (colChildren.length < 3) return false;
    const first = this.resolveNode(componentsMap, colChildren[0]);
    const id = first != null ? this.getId(first) : '';
    return id === 'header' || /^header-/.test(id);
  }

  getExpandableColumnRef(node: A2uiNode): unknown {
    return this.getChildren(node)[0];
  }

  /** True iff the first Column child is the conventional header (id === "header" | "header-*"). */
  private hasHeaderFirstChild(componentsMap: Map<string, A2uiNode>, colChildren: unknown[]): boolean {
    if (colChildren.length === 0) return false;
    const first = this.resolveNode(componentsMap, colChildren[0]);
    const id = first != null ? this.getId(first) : '';
    return id === 'header' || /^header-/.test(id);
  }

  /** True iff the last Column child is the conventional footer (id === "footer" | "footer-*"). */
  private hasFooterLastChild(componentsMap: Map<string, A2uiNode>, colChildren: unknown[]): boolean {
    if (colChildren.length === 0) return false;
    const last = this.resolveNode(componentsMap, colChildren[colChildren.length - 1]);
    const id = last != null ? this.getId(last) : '';
    return id === 'footer' || /^footer-/.test(id);
  }

  getExpandableHeaderChildRef(componentsMap: Map<string, A2uiNode>, cardNode: A2uiNode): unknown {
    const col = this.resolveNode(componentsMap, this.getExpandableColumnRef(cardNode));
    if (!col) return null;
    const list = this.getChildren(col);
    if (list.length === 0) return null;
    // If the first child is a conventional header, use it. Otherwise synthesize:
    // when the agent opted in via Card.expandable but did not provide a header-*
    // id, treat the first child as the header anyway (it is what the user sees
    // in the collapsed state).
    return list[0];
  }

  getExpandableMiddleChildRefs(componentsMap: Map<string, A2uiNode>, cardNode: A2uiNode): unknown[] {
    const col = this.resolveNode(componentsMap, this.getExpandableColumnRef(cardNode));
    if (!col) return [];
    const list = this.getChildren(col);
    if (list.length <= 1) return [];
    // If there is a conventional footer at the end, exclude it from the body.
    // Otherwise the whole tail after the header is body content (no auto-rip).
    const end = this.hasFooterLastChild(componentsMap, list) ? list.length - 1 : list.length;
    return list.slice(1, end);
  }

  getExpandableFooterChildRef(componentsMap: Map<string, A2uiNode>, cardNode: A2uiNode): unknown {
    const col = this.resolveNode(componentsMap, this.getExpandableColumnRef(cardNode));
    if (!col) return null;
    const list = this.getChildren(col);
    if (!this.hasFooterLastChild(componentsMap, list)) return null;
    return list[list.length - 1];
  }

  getExpandableColumnDataId(componentsMap: Map<string, A2uiNode>, cardNode: A2uiNode): string {
    const col = this.resolveNode(componentsMap, this.getExpandableColumnRef(cardNode));
    return col ? this.getId(col) : '';
  }

  isImageTextRow(componentsMap: Map<string, A2uiNode>, node: A2uiNode): boolean {
    const children = this.getChildren(node);
    if (children.length !== 2) return false;

    const child1 = this.resolveNode(componentsMap, children[0]);
    const child2 = this.resolveNode(componentsMap, children[1]);

    const type1 = this.getType(child1);
    const type2 = this.getType(child2);

    return (type1 === 'Image' && type2 === 'Text') || (type1 === 'Text' && type2 === 'Image');
  }

  getImageFromRow(componentsMap: Map<string, A2uiNode>, node: A2uiNode): string {
    const children = this.getChildren(node);
    for (const child of children) {
      const resolved = this.resolveNode(componentsMap, child);
      if (resolved && this.getType(resolved) === 'Image') {
        return this.getUrl(resolved);
      }
    }
    return '';
  }

  getTextFromRow(componentsMap: Map<string, A2uiNode>, node: A2uiNode): string {
    const children = this.getChildren(node);
    for (const child of children) {
      const resolved = this.resolveNode(componentsMap, child);
      if (resolved && this.getType(resolved) === 'Text') {
        return this.getText(resolved);
      }
    }
    return '';
  }

  getText(node: A2uiNode): string {
    const data = this.getData(node);
    const text = (data.text ?? data.label ?? '') as unknown;
    const resolved = this.resolveValue(text);
    return resolved != null ? String(resolved) : '';
  }

  getIconName(node: A2uiNode): string {
    const data = this.getData(node);
    const name = (data.name ?? data.text ?? data.label ?? '') as unknown;
    const resolved = this.resolveValue(name);
    return resolved != null ? String(resolved) : '';
  }

  getUrl(node: A2uiNode): string {
    const data = this.getData(node);
    const url = data.url as Record<string, any> | undefined;
    const src = data.src as Record<string, any> | undefined;
    const urlLit = url?.['literalString'];
    const srcLit = src?.['literalString'];
    return String(urlLit ?? srcLit ?? '');
  }

  getUsageHint(node: A2uiNode | null): string {
    if (!node) return 'body';
    const data = this.getData(node);
    return String(data.usageHint || 'body');
  }

  isAutoplay(node: A2uiNode): boolean {
    return !!this.getData(node).autoplay;
  }

  isLoop(node: A2uiNode): boolean {
    return !!this.getData(node).loop;
  }

  isMuted(node: A2uiNode): boolean {
    return !!this.getData(node).muted;
  }

  isPlaysinline(node: A2uiNode): boolean {
    return !!this.getData(node).playsinline;
  }

  isPrimary(node: A2uiNode): boolean {
    return !!this.getData(node).primary;
  }

  getAxis(node: A2uiNode): string {
    const data = this.getData(node);
    return String(data.axis || 'horizontal');
  }

  getLabel(node: A2uiNode): string {
    const data = this.getData(node);
    const label = data.label || '';
    const resolved = this.resolveValue(label);
    return resolved != null ? String(resolved) : '';
  }

  getFieldType(node: A2uiNode): string {
    const data = this.getData(node);
    const typeMap: Record<string, string> = {
      shortText: 'text',
      longText: 'textarea',
      number: 'number',
      date: 'date',
      obscured: 'password',
    };
    return typeMap[String(data.textFieldType || 'shortText')] || 'text';
  }

  getDateTimeInputType(node: A2uiNode): string {
    const data = this.getData(node);
    if (data.enableDate && data.enableTime) return 'datetime-local';
    if (data.enableDate) return 'date';
    if (data.enableTime) return 'time';
    return 'datetime-local';
  }

  getDateTimeIcon(node: A2uiNode): string {
    const data = this.getData(node);
    if (data.enableDate && data.enableTime) return 'event';
    if (data.enableDate) return 'calendar_today';
    if (data.enableTime) return 'schedule';
    return 'event';
  }

  getDateTimeValue(node: A2uiNode): string {
    const data = this.getData(node);
    const raw = this.resolveValue(data.value);
    if (!raw) return '';
    try {
      const d = new Date(String(raw));
      if (isNaN(d.getTime())) return String(raw);
      if (data.enableDate && data.enableTime) {
        return d.toISOString().slice(0, 16);
      }
      if (data.enableDate) {
        return d.toISOString().slice(0, 10);
      }
      if (data.enableTime) {
        return d.toISOString().slice(11, 16);
      }
    } catch {
      return String(raw);
    }
    return String(raw);
  }

  getValue(node: A2uiNode): unknown {
    const data = this.getData(node);
    const resolved = this.resolveValue(data.value);
    if (resolved === undefined || resolved === null) {
      // Warn so a missing `value` prop on Slider/ScoreBar/CheckBox doesn't
      // silently render as zero/empty — which has bitten us before. We keep
      // the `?? 0` fallback for template stability.
      const id = String((node as { id?: unknown }).id ?? this.getType(node));
      this.warnOnce(
        'missing-value',
        id,
        `getValue: component '${id}' has no value prop; falling back to 0.`,
      );
      return 0;
    }
    return resolved;
  }

  getMinValue(node: A2uiNode): number {
    const data = this.getData(node);
    // Accept either a raw number or an A2UI Value wrapper (e.g.
    // {literalNumber: 800}). Without resolveValue() the wrapper was being
    // passed straight to Number(), yielding NaN — which made every
    // ScoreBar render with band 'low' (red), regardless of its actual
    // value, because NaN comparisons fall through to the final branch.
    if (data.minValue === undefined) return 0;
    const resolved = this.resolveValue(data.minValue);
    return Number(resolved);
  }

  getMaxValue(node: A2uiNode): number {
    const data = this.getData(node);
    if (data.maxValue === undefined) return 100;
    const resolved = this.resolveValue(data.maxValue);
    return Number(resolved);
  }

  /**
   * Optional explicit color band override on a ScoreBar.
   * Returns "high" | "mid" | "low" when the agent supplies
   * `colorBand: {literalString: ...}` (or a raw string), else null.
   *
   * This lets metrics where "high = bad" (e.g. count of Lost stakeholders)
   * paint themselves red despite the default auto-band logic that treats
   * high percentages as green.
   */
  getColorBandOverride(node: A2uiNode): 'high' | 'mid' | 'low' | null {
    const data = this.getData(node);
    const raw = data.colorBand;
    if (raw === undefined || raw === null) return null;
    const s = String(this.resolveValue(raw) ?? '').toLowerCase();
    if (s === 'high' || s === 'mid' || s === 'low') return s;
    return null;
  }

  getModalEntryPoint(node: A2uiNode): unknown {
    return this.getData(node).entryPointChild ?? null;
  }

  getModalContent(node: A2uiNode): unknown {
    return this.getData(node).contentChild ?? null;
  }

  getOptions(node: A2uiNode): unknown[] {
    return (this.getData(node).options as unknown[]) || [];
  }

  resolveOptionLabel(option: Record<string, unknown>): string {
    const label = option['label'];
    if (!label) return '';
    const resolved = this.resolveValue(label);
    return resolved != null ? String(resolved) : '';
  }

  isScorecardExpandToggleButton(node: A2uiNode): boolean {
    const actionData = this.getData(node)['action'] as Record<string, unknown> | undefined;
    const name = this.resolveValue(actionData?.['name']);
    return typeof name === 'string'
      && (A2UI_VIEW_ACTION_NAMES as readonly string[]).includes(name);
  }

  isRunSimulationButton(node: A2uiNode): boolean {
    const actionData = this.getData(node)['action'] as Record<string, unknown> | undefined;
    return this.resolveValue(actionData?.['name']) === 'run_simulation';
  }
}
