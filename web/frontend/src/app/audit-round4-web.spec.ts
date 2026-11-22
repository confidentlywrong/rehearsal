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
import { Injector, runInInjectionContext } from '@angular/core';
import { Router, UrlTree } from '@angular/router';
import * as THREE from 'three';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { agentGateway, type ChatMessage } from './agent-gateway-updates';
import { approvalGuard } from './auth/approval.guard';
import { IdentityService } from './auth/identity.service';
import { CatmullRomPathAdapter } from './catmullrom-path-adapter';
import { isDebugRaceRunning, startDebugRace, stopDebugRace } from './debug-race';
import { hasGeoData, parseGatewayPayload } from './gateway-messages';
import { RoadNetwork } from './road-network';
import { simLog } from './sim-logger';
import { simulationState } from './simulation-state';
import { AgentMessageType } from './types';
import { PerfMonitor } from './viewport/debug/perf-monitor';
import { Context } from './viewport/context';
import {
  disposeSharedIconResources,
  getCrowdZone,
  getMedicalZone,
  getPortableToiletZone,
  getTrafficZones,
  getWaterZone,
  lngLatToWorld,
  tickStartZoneAnimations,
} from './viewport/route/icons';
import {
  drawRouteSuppressSegmentToTexture,
  drawTrafficjamSegmentToTexture,
  drawTrafficjamToTexture,
  removeRoute,
} from './viewport/route/route';
import { tickCameraFollow } from './viewport/scene/scene';
import { createAnimatedParticles } from './viewport/shaders/animated-particle-shader';
import { createConfettiParticles } from './viewport/shaders/confetti-shader';
import { computeDistance, parseGeoJSON } from './viewport/viewport-utils';
import { MedicalTent, SimpleStationZone, WaterStation } from './water-station';

function mockCanvas2dContext(): void {
  const fakeCtx = {
    font: '',
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    shadowColor: '',
    shadowBlur: 0,
    textBaseline: 'alphabetic',
    textAlign: 'left',
    lineCap: 'butt',
    lineJoin: 'miter',
    measureText: vi.fn(() => ({ width: 48 })),
    createRadialGradient: vi.fn(() => ({ addColorStop: vi.fn() })),
    fillRect: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    arcTo: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    fillText: vi.fn(),
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(fakeCtx as any);
}

describe('Round 4 — RoadNetwork hardening & Three.js disposal', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('clamps extreme Web-Mercator latitudes and skips malformed GeoJSON coordinates', () => {
    const net = RoadNetwork.fromGeoJSON({
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: { name: 'Polar Road' },
          geometry: {
            type: 'LineString',
            coordinates: [
              [-71.06, 90],
              [-71.05, -90],
              [Number.NaN, 42.35],
              ['bad', 42.36],
              [-71.04],
            ],
          },
        },
        {
          type: 'Feature',
          properties: { name: 'Too Short After Filter' },
          geometry: {
            type: 'LineString',
            coordinates: [
              [-71.06, 42.35],
              [-71.06, Number.POSITIVE_INFINITY],
            ],
          },
        },
        {
          type: 'Feature',
          geometry: { type: 'Point', coordinates: [-71.06, 42.35] },
        },
      ],
    });

    expect(net.edges.length).toBeGreaterThanOrEqual(1);
    for (const n of net.nodes) {
      expect(Number.isFinite(n.position.x)).toBe(true);
      expect(Number.isFinite(n.position.y)).toBe(true);
      expect(Number.isFinite(n.position.z)).toBe(true);
    }

    expect(() => RoadNetwork.fromGeoJSON(null)).not.toThrow();
    expect(() => RoadNetwork.fromGeoJSON({ type: 'FeatureCollection', features: null })).not.toThrow();
    expect(RoadNetwork.fromGeoJSON({ type: 'FeatureCollection', features: [] }).nodes).toEqual([]);
    expect(net.findEdgesCrossingPath([], -5)).toEqual([]);
  });

  it('handles headless/null 2D canvas contexts gracefully in buildRoadNameLabels', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);

    const net = RoadNetwork.fromGeoJSON({
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: { name: 'Boylston St' },
          geometry: {
            type: 'LineString',
            coordinates: [
              [-71.07, 42.35],
              [-71.05, 42.351],
            ],
          },
        },
      ],
    });

    const scene = new THREE.Scene();
    expect(() => net.addToScene(scene)).not.toThrow();
    expect(() => net.removeFromScene(scene)).not.toThrow();
  });

  it('disposes Lines, Sprites, CanvasTextures, Barriers, and template geometries/materials in removeFromScene', () => {
    mockCanvas2dContext();
    const net = RoadNetwork.fromGeoJSON({
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: { name: 'Commonwealth Ave' },
          geometry: {
            type: 'LineString',
            coordinates: [
              [-71.08, 42.35],
              [-71.05, 42.352],
            ],
          },
        },
        {
          type: 'Feature',
          properties: { name: 'Arlington St' },
          geometry: {
            type: 'LineString',
            coordinates: [
              [-71.07, 42.345],
              [-71.07, 42.355],
            ],
          },
        },
      ],
    });

    const scene = new THREE.Scene();
    net.addToScene(scene);

    const edgeIds = net.edges.map((e) => e.id);
    expect(edgeIds.length).toBeGreaterThanOrEqual(2);
    net.closeEdges([edgeIds[0]], scene);
    net.closeEdges([edgeIds[1]], scene);

    const roadGroup = scene.getObjectByName('road-network')!;
    const barrierGroup = scene.getObjectByName('road-barriers')!;
    expect(roadGroup).toBeTruthy();
    expect(barrierGroup).toBeTruthy();

    const lineGeos: THREE.BufferGeometry[] = [];
    const lineMats: THREE.Material[] = [];
    const spriteMats: THREE.SpriteMaterial[] = [];
    const canvasTexs: THREE.Texture[] = [];
    const spriteGeos: THREE.BufferGeometry[] = [];

    roadGroup.traverse((obj) => {
      if (obj instanceof THREE.Line) {
        lineGeos.push(obj.geometry);
        lineMats.push(obj.material as THREE.Material);
      } else if (obj instanceof THREE.Sprite) {
        spriteGeos.push(obj.geometry);
        const sm = obj.material;
        spriteMats.push(sm);
        if (sm.map) canvasTexs.push(sm.map);
      }
    });

    expect(lineGeos.length).toBeGreaterThan(0);
    expect(spriteMats.length).toBeGreaterThan(0);
    expect(canvasTexs.length).toBeGreaterThan(0);

    const spriteGeoDisposeSpy = vi.spyOn(spriteGeos[0], 'dispose');
    const lineGeoSpies = lineGeos.map((g) => vi.spyOn(g, 'dispose'));
    const lineMatSpies = lineMats.map((m) => vi.spyOn(m, 'dispose'));
    const spriteMatSpies = spriteMats.map((m) => vi.spyOn(m, 'dispose'));
    const texSpies = canvasTexs.map((t) => vi.spyOn(t, 'dispose'));

    const allMatDisposeSpy = vi.spyOn(THREE.Material.prototype, 'dispose');
    const allGeoDisposeSpy = vi.spyOn(THREE.BufferGeometry.prototype, 'dispose');

    net.removeFromScene(scene);

    expect(scene.getObjectByName('road-network')).toBeUndefined();
    expect(scene.getObjectByName('road-barriers')).toBeUndefined();

    // Shared global Sprite quad geometry must NOT be disposed
    expect(spriteGeoDisposeSpy).not.toHaveBeenCalled();
    for (const s of lineGeoSpies) expect(s).toHaveBeenCalledTimes(1);
    for (const s of lineMatSpies) expect(s).toHaveBeenCalledTimes(1);
    for (const s of spriteMatSpies) expect(s).toHaveBeenCalledTimes(1);
    for (const s of texSpies) expect(s).toHaveBeenCalledTimes(1);
    expect(allMatDisposeSpy).toHaveBeenCalled();
    expect(allGeoDisposeSpy).toHaveBeenCalled();
  });
});

describe('Round 4 — WaterStation, MedicalTent, CrowdZone, PortableToilet & CatmullRomPathAdapter', () => {
  it('guards NaN/negative radii and non-finite positions in SimpleStationZone, WaterStation, and MedicalTent', () => {
    for (const type of ['water_station', 'medical_tent', 'crowd_zone', 'portable_toilet'] as const) {
      const badNeg = new SimpleStationZone(1, type, new THREE.Vector3(0, 0, 0), -10);
      expect(badNeg.radius).toBe(0);
      expect(badNeg.isInsideRadius(new THREE.Vector3(0, 0, 0))).toBe(false);

      const badNaN = new SimpleStationZone(
        2,
        type,
        new THREE.Vector3(Number.NaN, 0, Number.POSITIVE_INFINITY),
        Number.NaN,
      );
      expect(badNaN.worldPos.toArray()).toEqual([0, 0, 0]);
      expect(badNaN.radius).toBe(0);
      expect(badNaN.isInsideRadius(new THREE.Vector3(0, 0, 0))).toBe(false);
    }

    const scene = new THREE.Scene();
    const water = new WaterStation({ id: 1, kmMark: Number.NaN, worldPos: new THREE.Vector3(10, 0, 20) });
    water.addToScene(scene);
    expect(water.kmMark).toBe(0);
    expect(water.isInsideRadius(new THREE.Vector3(Number.NaN, 0, 20))).toBe(false);
    expect(water.isInsideRadius(new THREE.Vector3(12, 0, 20))).toBe(true);

    const disposeMatSpy = vi.spyOn((water as any).groundMesh.material, 'dispose');
    water.removeFromScene(scene);
    water.removeFromScene(scene);
    expect(disposeMatSpy).toHaveBeenCalledTimes(1);

    const med = new MedicalTent({ id: 2, kmMark: 5, worldPos: new THREE.Vector3(0, 0, 0) });
    med.addToScene(scene);
    expect(med.isInsideRadius(new THREE.Vector3(0, 0, 5))).toBe(true);
    med.removeFromScene(scene);
  });

  it('handles < 2 control points, zero-length curves, and NaN/out-of-range t in catmullrom-path-adapter.ts', () => {
    const emptyCurve = { points: [], getLength: () => 0 } as unknown as THREE.CatmullRomCurve3;
    const emptyAdapter = new CatmullRomPathAdapter(emptyCurve, { lengthMi: Number.NaN });
    expect(emptyAdapter.lengthMi).toBeNull();
    expect(emptyAdapter.getTotalLength()).toBe(0);
    expect(emptyAdapter.getPositionAt(0.5).toArray()).toEqual([0, 0, 0]);
    expect(emptyAdapter.getTangentAt(Number.NaN).length()).toBeGreaterThan(0);

    const singleCurve = {
      points: [new THREE.Vector3(7, 0, -3)],
      getLength: () => {
        throw new Error('cannot measure 1 point');
      },
    } as unknown as THREE.CatmullRomCurve3;
    const singleAdapter = new CatmullRomPathAdapter(singleCurve);
    expect(singleAdapter.getPositionAt(0.5).toArray()).toEqual([7, 0, -3]);
    expect(singleAdapter.getTangentAt(2).length()).toBeGreaterThan(0);

    const zeroLenCurve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(4, 0, 9),
      new THREE.Vector3(4, 0, 9),
    ]);
    const zeroLenAdapter = new CatmullRomPathAdapter(zeroLenCurve);
    expect(zeroLenAdapter.getTotalLength()).toBe(0);
    expect(zeroLenAdapter.getPositionAt(0.5).toArray()).toEqual([4, 0, 9]);
    const tang = zeroLenAdapter.getTangentAt(Number.NaN);
    expect(Number.isFinite(tang.x) && Number.isFinite(tang.z)).toBe(true);
    expect(tang.length()).toBeGreaterThan(0);

    const validCurve = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(10, 0, 0),
      new THREE.Vector3(20, 0, 0),
    ]);
    const validAdapter = new CatmullRomPathAdapter(validCurve, { lengthMi: 13.1 });
    expect(validAdapter.lengthMi).toBe(13.1);
    expect(validAdapter.getPositionAt(-5).x).toBeCloseTo(0, 5);
    expect(validAdapter.getPositionAt(5).x).toBeCloseTo(20, 5);
    expect(Number.isFinite(validAdapter.getPositionAt(Number.NaN).x)).toBe(true);
    expect(validAdapter.getTangentAt(Number.NaN).length()).toBeGreaterThan(0);
  });
});

describe('Round 4 — SimulationStateService, SimLogger & debug-race.ts', () => {
  afterEach(() => {
    simulationState.deactivate();
    simulationState.removeAllListeners();
    simLog.clear();
    stopDebugRace();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('guards malformed/non-object payloads, NaN metrics, and reads rawJson in advance_tick', () => {
    let onChatCb!: (msg: ChatMessage) => void;
    vi.spyOn(agentGateway, 'onChat').mockImplementation((cb) => {
      onChatCb = cb;
      return () => {};
    });

    simulationState.activate('sim-1');

    // Non-string AGENT_START text & array/primitive TOOL_END JSON must not throw
    expect(() =>
      onChatCb({
        guid: 'sim-1',
        speaker: 'Sim',
        text: undefined as unknown as string,
        isUser: false,
        timestamp: new Date(),
        msgType: AgentMessageType.AGENT_START,
      }),
    ).not.toThrow();

    onChatCb({
      guid: 'sim-1',
      speaker: 'Sim',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_END,
      toolName: 'spawn_runners',
      rawJson: '[1, 2, 3]',
    });
    expect(simulationState.getState().runnerSessionIds).toEqual([]);

    // Non-finite config values fall back safely
    onChatCb({
      guid: 'sim-1',
      speaker: 'Sim',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_END,
      toolName: 'prepare_simulation',
      result: { duration_seconds: Number.NaN, runner_count: 'bad' },
    });
    expect(simulationState.getState().config).toEqual({
      durationSeconds: 60,
      tickIntervalSeconds: 5,
      totalRaceHours: 6,
      runnerCount: 10,
    });

    // advance_tick via rawJson fallback with nested result
    onChatCb({
      guid: 'sim-1',
      speaker: 'Sim',
      text: '',
      isUser: false,
      timestamp: new Date(),
      msgType: AgentMessageType.TOOL_END,
      toolName: 'advance_tick',
      rawJson: JSON.stringify({
        result: { tick: 3, max_ticks: 10, avg_velocity: 7.5 },
      }),
    });
    expect(simulationState.getState().currentTick).toBe(3);
    expect(simulationState.getState().snapshots[0].avgVelocity).toBe(7.5);
  });

  it('bounds SimLogger entries and handles missing navigator.clipboard cleanly', async () => {
    simLog.clear();
    for (let i = 0; i < 5025; i++) {
      simLog.log('TEST', 'runner-id-001', `entry-${i}`);
    }
    expect(simLog.size).toBe(5000);
    expect((simLog as unknown as { entries: string[] }).entries[0]).toContain('entry-25');

    const origNav = globalThis.navigator;
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: {},
    });
    await expect(simLog.copyToClipboard()).resolves.toBeUndefined();
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: origNav,
    });
  });

  it('cancels pending start timers on stopDebugRace and rejects invalid count/speed', async () => {
    vi.useFakeTimers();
    const events: string[] = [];
    const onStarted = () => events.push('sim:raceStarted');
    window.addEventListener('sim:raceStarted', onStarted);

    startDebugRace(0, 1);
    expect(isDebugRaceRunning()).toBe(false);
    startDebugRace(Number.NaN, 1);
    expect(isDebugRaceRunning()).toBe(false);

    // Start, stop before 500ms/1500ms timeouts, start a new race: stale timeouts must not double-fire
    startDebugRace(2, Number.NaN);
    expect(isDebugRaceRunning()).toBe(true);
    stopDebugRace();
    expect(isDebugRaceRunning()).toBe(false);

    await vi.advanceTimersByTimeAsync(2000);
    expect(events).toEqual([]);

    window.removeEventListener('sim:raceStarted', onStarted);
  });
});

describe('Round 4 — approvalGuard & gateway-messages.ts', () => {
  it('catches identity initialization rejections fail-closed to /pending-approval', async () => {
    const mockUrlTree = new UrlTree();
    const createUrlTree = vi.fn(() => mockUrlTree);

    const runWithEnsure = async (fn: () => Promise<any>) => {
      const injector = Injector.create({
        providers: [
          { provide: IdentityService, useValue: { ensureInitialized: fn } },
          { provide: Router, useValue: { createUrlTree } },
        ],
      });
      return runInInjectionContext(injector, () => approvalGuard({} as any, {} as any));
    };

    const rejectedRes = await runWithEnsure(() => Promise.reject(new Error('network boom')));
    expect(rejectedRes).toBe(mockUrlTree);
    expect(createUrlTree).toHaveBeenCalledWith(['/pending-approval']);

    // Still fails open when gateway intentionally reports unavailable snapshot
    const unavailableRes = await runWithEnsure(async () => ({ available: false, data: null }));
    expect(unavailableRes).toBe(true);
  });

  it('guards null/array/non-string inputs in parseGatewayPayload and hasGeoData', () => {
    expect(parseGatewayPayload(null as any)).toBeNull();
    expect(parseGatewayPayload(undefined as any)).toBeNull();
    expect(parseGatewayPayload([] as any)).toBeNull();
    expect(parseGatewayPayload({ text: 123 as any, emotion: 456 as any })).toBeNull();
    expect(parseGatewayPayload({ text: '[1, 2, 3]' })).toEqual({
      kind: 'narrative',
      text: '[1, 2, 3]',
      emotion: '',
    });

    expect(hasGeoData(undefined, null)).toBe(false);
    expect(hasGeoData('unknown_tool', undefined)).toBe(false);
    expect(hasGeoData('add_water_stations', undefined)).toBe(true);
  });
});

describe('Round 4 — Viewport Mercator, Zone Resource Disposal, Utils, PerfMonitor & Shaders', () => {
  beforeEach(() => {
    disposeSharedIconResources();
  });

  afterEach(() => {
    disposeSharedIconResources();
    vi.restoreAllMocks();
  });

  it('clamps Web-Mercator latitudes in lngLatToWorld and guards getTrafficZones & tickStartZoneAnimations', async () => {
    const pole = lngLatToWorld(-71.06, 90);
    expect(Number.isFinite(pole.x) && Number.isFinite(pole.z)).toBe(true);
    expect(lngLatToWorld(Number.NaN, 42.35).toArray()).toEqual([0, 0, 0]);

    const ctx = new Context();
    ctx.textureLoader = { loadAsync: vi.fn(async () => new THREE.Texture()) } as any;
    const zones = await getTrafficZones(ctx, [
      { coordinates: [Number.NaN, 42.35], cross_streets: ['A', 'B'], impact_level: 'high' },
      { coordinates: [-71.06, 42.35], cross_streets: null as any, impact_level: 'medium' },
    ]);
    expect(zones).toHaveLength(1);
    expect(Number.isFinite(zones[0].position.x)).toBe(true);

    const mesh = new THREE.Mesh();
    ctx.vignettePass = {
      uniforms: { uColor: { value: new THREE.Color(0x000000) }, darkness: { value: 1.0 } },
    } as any;
    ctx.startZoneAnimations.push({
      mesh,
      material: null,
      fromColor: new THREE.Color(0xffffff),
      elapsed: 0,
      pulseDuration: 0,
      pulseCount: 1,
      fromScale: 1,
      toScale: 1,
      lastPulseIndex: 0,
      fromVignetteColor: new THREE.Color(0x000000),
      fromVignetteDarkness: 1,
      restoring: false,
      restoreElapsed: 0,
      restoreDuration: 0,
      restoreFromDarkness: 1,
    });
    tickStartZoneAnimations(ctx, Number.NaN);
    expect(ctx.startZoneAnimations).toHaveLength(1);
    tickStartZoneAnimations(ctx, 1.0);
    expect(ctx.startZoneAnimations).toHaveLength(0);
  });

  it('disposes discarded materials/textures on zone creation and deduplicates/resets caches on removeRoute', async () => {
    const ctx = new Context();
    ctx.scene = new THREE.Scene();
    ctx.textureLoader = { loadAsync: vi.fn(async () => new THREE.Texture()) } as any;

    const matDisposeSpy = vi.spyOn(THREE.ShaderMaterial.prototype, 'dispose');
    const texDisposeSpy = vi.spyOn(THREE.DataTexture.prototype, 'dispose');

    const w1 = await getWaterZone(ctx);
    const w2 = await getWaterZone(ctx);
    const m1 = await getMedicalZone(ctx);
    const m2 = await getMedicalZone(ctx);
    const p1 = await getPortableToiletZone(ctx);
    const c1 = await getCrowdZone(ctx);

    // Discarded duplicate particle materials & replaced 1x1 blackTex DataTextures were disposed
    expect(matDisposeSpy.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(texDisposeSpy.mock.calls.length).toBeGreaterThanOrEqual(5);
    matDisposeSpy.mockRestore();
    texDisposeSpy.mockRestore();

    const routeGroup = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial());
    routeGroup.add(w1, w2, m1, m2, p1, c1);
    ctx.currentRoute = routeGroup;
    ctx.scene.add(routeGroup);

    const cylMat = ctx.causticsCylinderMaterial!;
    const cylDisposeSpy = vi.spyOn(cylMat, 'dispose');

    removeRoute(ctx);

    // Shared cylinder material is disposed ONCE even though w1 & w2 both referenced it
    expect(cylDisposeSpy).toHaveBeenCalledTimes(1);
    expect(ctx.causticsCylinderMaterial).toBeNull();
    expect(ctx.causticsParticleMaterial).toBeNull();
    expect(ctx.medicalCylinderMaterial).toBeNull();
    expect(ctx.medicalParticleMaterial).toBeNull();
    expect(ctx.crowdParticleMaterial).toBeNull();
  });

  it('guards null 2D canvas contexts in viewport/route/route.ts canvas helpers', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    const ctx = new Context();

    await expect(
      drawRouteSuppressSegmentToTexture(ctx, [
        { x: 0, z: 0 },
        { x: 5, z: 5 },
      ]),
    ).resolves.toBeUndefined();
    await expect(drawTrafficjamToTexture(ctx, 0, 0)).resolves.toBeUndefined();
    await expect(
      drawTrafficjamSegmentToTexture(ctx, [
        { x: 0, z: 0 },
        { x: 5, z: 5 },
      ]),
    ).resolves.toBeUndefined();
  });

  it('guards PerfMonitor, tickCameraFollow, viewport-utils, and particle/confetti shaders against invalid inputs', () => {
    const perf = new PerfMonitor();
    const fakeRenderer = {
      info: {
        render: { calls: 10, triangles: 100 },
        memory: { textures: 2, geometries: 3 },
        reset: vi.fn(),
      },
    } as any;
    perf.tick(Number.NaN, fakeRenderer);
    perf.tick(-0.016, fakeRenderer);
    perf.tick(0.016, fakeRenderer);
    expect(perf.fps).toBeCloseTo(62.5, 1);

    const ctx = new Context();
    ctx.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    ctx.camera.position.set(10, 20, 30);
    ctx.controls = { target: new THREE.Vector3(0, 0, 0), autoRotateSpeed: 1 } as any;
    ctx.cameraFollow = {
      mesh: new THREE.Mesh(),
      offset: new THREE.Vector3(1, 2, 3),
      autoRotate: true,
    };
    tickCameraFollow(ctx, Number.NaN);
    expect(ctx.camera.position.toArray()).toEqual([10, 20, 30]);

    expect(computeDistance([])).toBe(0);
    expect(computeDistance([[Number.NaN, 42], [-71.06, 42.35]])).toBe(0);
    const parsed = parseGeoJSON({
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: { name: 'Route' },
          geometry: {
            type: 'LineString',
            coordinates: [
              [-71.06, 42.35],
              [Number.NaN, 42.36],
              [-71.05, 42.36],
            ],
          },
        },
        {
          type: 'Feature',
          properties: { type: 'water_station', mi: 1 },
          geometry: { type: 'Point', coordinates: [Number.NaN, 42.35] },
        },
        {
          type: 'Feature',
          properties: { type: 'crowd_zone', mi: 2 },
          geometry: { type: 'Point', coordinates: [-71.055, 42.355] },
        },
      ],
    });
    expect(parsed?.coords).toHaveLength(2);
    expect(parsed?.waterStations).toHaveLength(0);
    expect(parsed?.crowdZones).toHaveLength(1);

    const pts = createAnimatedParticles(0xffffff, new THREE.Texture(), 50, -10);
    expect(pts.geometry.getAttribute('position').count).toBe(0);
    pts.geometry.dispose();
    (pts.material as THREE.Material).dispose();

    const confetti = createConfettiParticles({ count: Number.NaN, colors: [] });
    expect(confetti.geometry.getAttribute('position').count).toBe(0);
    confetti.geometry.dispose();
    (confetti.material as THREE.Material).dispose();
  });
});
