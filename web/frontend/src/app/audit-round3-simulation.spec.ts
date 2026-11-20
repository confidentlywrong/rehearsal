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

import * as THREE from 'three';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentGateway } from './agent-gateway-updates';
import { CarSimulator } from './car-simulator';
import { GLBRoadsLoader } from './glb-roads';
import { GradientPathMesh } from './gradient-path';
import { LabelOverlay } from './label-overlay';
import { CatmullRomPathAdapter, MapPath } from './path';
import { Runner } from './runner';
import { RunnerManager } from './simulation';
import { TrafficSimulator } from './traffic-simulator';
import { Context } from './viewport/context';
import { disposeSharedIconResources, tickZones } from './viewport/route/icons';
import { disposeSharedRouteResources, removeRoute, tickRouteDraw } from './viewport/route/route';
import { tickCameraIntro, tickCameraPan } from './viewport/scene/scene';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('RunnerManager (simulation.ts)', () => {
  it('evaluates simTargetGuid dynamically at event time even if set after addRunnerWithGuid', async () => {
    vi.useFakeTimers();
    const sendSpy = vi.spyOn(agentGateway, 'sendBroadcast').mockResolvedValue(undefined);
    vi.spyOn(agentGateway, 'isNdjsonReplayActive').mockReturnValue(false);

    const scene = new THREE.Scene();
    const mgr = new RunnerManager(scene);
    const path = new MapPath(1, [
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(100, 0, 0),
    ]);

    // Add runner BEFORE simTargetGuid is configured
    mgr.addRunnerWithGuid('sim-runner-1', path, {
      effectiveMph: 8,
      simDistanceIntegrator: 1,
    });
    mgr.simTargetGuid = 'planner-target-1';

    const runner = mgr.getRunner('sim-runner-1')!;
    // Trigger broadcast via runner's internal broadcastFn
    (runner as unknown as { broadcastFn?: (g: string, t: string) => void }).broadcastFn?.(
      'sim-runner-1',
      JSON.stringify({ event: 'milestone', text: 'Mile 1 reached' }),
    );

    await vi.advanceTimersByTimeAsync(1100);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0][1]).toEqual(['planner-target-1']);

    mgr.dispose();
  });

  it('guards non-object JSON payloads and clears timer + buffer on clearRunners/dispose', async () => {
    vi.useFakeTimers();
    const sendSpy = vi.spyOn(agentGateway, 'sendBroadcast').mockResolvedValue(undefined);
    vi.spyOn(agentGateway, 'isNdjsonReplayActive').mockReturnValue(false);

    const scene = new THREE.Scene();
    const mgr = new RunnerManager(scene);
    mgr.simTargetGuid = 'planner-target-2';
    const path = new MapPath(1, [
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(100, 0, 0),
    ]);
    mgr.addRunnerWithGuid('sim-runner-2', path, {
      effectiveMph: 8,
      simDistanceIntegrator: 1,
    });
    const runner = mgr.getRunner('sim-runner-2')!;
    const fn = (runner as unknown as { broadcastFn?: (g: string, t: string) => void }).broadcastFn!;

    // Arrays, primitives, and null must be handled safely without throwing
    expect(() => fn('sim-runner-2', 'null')).not.toThrow();
    expect(() => fn('sim-runner-2', '[1,2,3]')).not.toThrow();
    expect(() => fn('sim-runner-2', '123')).not.toThrow();

    // Clearing runners cancels the pending flush timer and buffer
    mgr.clearRunners();
    expect(mgr.hasRunners()).toBe(false);

    await vi.advanceTimersByTimeAsync(1500);
    expect(sendSpy).not.toHaveBeenCalled();

    mgr.dispose();
    expect(mgr.simTargetGuid).toBeNull();
  });
});

function mockCanvas2dContext(): void {
  const grad = { addColorStop: vi.fn() };
  const fakeCtx = {
    createRadialGradient: vi.fn(() => grad),
    fillRect: vi.fn(),
    clearRect: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    fillText: vi.fn(),
    measureText: vi.fn(() => ({ width: 40 })),
    setTransform: vi.fn(),
    scale: vi.fn(),
    translate: vi.fn(),
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(fakeCtx as any);
}

describe('3D Simulation & Path robustness', () => {
  it('guards NaN and non-positive values in Runner methods', () => {
    const scene = new THREE.Scene();
    const path = new MapPath(1, [
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(50, 0, 0),
    ]);
    const r = new Runner('sim-guard', {
      scene,
      path,
      effectiveMph: 6,
      simDistanceIntegrator: 0.1,
    });

    r.setWater(50);
    r.setWater(Number.NaN);
    expect(r.getWater()).toBe(50);

    r.setVelocity(1.5);
    r.setVelocity(Number.NaN);
    r.setVelocity(Number.POSITIVE_INFINITY);
    r.setRawMph(Number.NaN);
    r.tick(Number.NaN);
    r.tick(-1);

    const pos = r.getPosition();
    expect(Number.isFinite(pos.x) && Number.isFinite(pos.y) && Number.isFinite(pos.z)).toBe(true);
    r.dispose();
  });

  it('prevents NaN and division-by-zero in CatmullRomPathAdapter and MapPath', () => {
    const emptyAdapter = new CatmullRomPathAdapter([], 0);
    expect(emptyAdapter.getPositionAt(0.5).toArray()).toEqual([0, 0, 0]);
    expect(emptyAdapter.getTangentAt(Number.NaN).length()).toBeGreaterThan(0);

    const coincident = new CatmullRomPathAdapter(
      [new THREE.Vector3(3, 0, 4), new THREE.Vector3(3, 0, 4)],
      0,
    );
    const tang = coincident.getTangentAt(0.5);
    expect(Number.isFinite(tang.x) && Number.isFinite(tang.z)).toBe(true);
    expect(tang.length()).toBeGreaterThan(0);

    const emptyMapPath = new MapPath(1, []);
    expect(emptyMapPath.getPositionAt(Number.NaN).toArray()).toEqual([0, 0, 0]);
    const mapTang = emptyMapPath.getTangentAt(0.5);
    expect(mapTang.length()).toBeGreaterThan(0);

    const dupMapPath = new MapPath(2, [
      new THREE.Vector3(5, 0, 5),
      new THREE.Vector3(5, 0, 5),
    ]);
    const dupPos = dupMapPath.getPositionAt(Number.NaN);
    const dupTang = dupMapPath.getTangentAt(0.5);
    expect(Number.isFinite(dupPos.x)).toBe(true);
    expect(dupTang.length()).toBeGreaterThan(0);
  });

  it('guards NaN rates, non-finite counts, and degenerate points in GradientPathMesh', () => {
    const scene = new THREE.Scene();
    const shortPath = new MapPath(1, [new THREE.Vector3(0, 0, 0)]);
    const grad = new GradientPathMesh(scene, shortPath);
    expect(() => grad.updateRates([Number.NaN, Number.POSITIVE_INFINITY])).not.toThrow();
    expect(() => grad.setVisibleSegmentCount(Number.NaN)).not.toThrow();

    const dupPath = new MapPath(2, [
      new THREE.Vector3(1, 0, 1),
      new THREE.Vector3(1, 0, 1),
    ]);
    const grad2 = new GradientPathMesh(scene, dupPath);
    grad2.setVisibleSegmentCount(4);
    expect(() => grad2.updateRates([Number.NaN, 0.5])).not.toThrow();
    grad2.dispose();
    grad.dispose();
  });

  it('safely ticks TrafficSimulator & CarSimulator and handles empty road networks + dispose', () => {
    mockCanvas2dContext();
    const scene = new THREE.Scene();
    const nodes = [
      { id: 1, position: new THREE.Vector3(0, 0, 0) },
      { id: 2, position: new THREE.Vector3(20, 0, 0) },
    ];
    const edges = [
      {
        id: 1,
        from: 1,
        to: 999, // missing node must not throw
        points: [new THREE.Vector3(0, 0, 0), new THREE.Vector3(20, 0, 0)],
      },
      {
        id: 2,
        from: 1,
        to: 2,
        points: [new THREE.Vector3(0, 0, 0), new THREE.Vector3(20, 0, 0)],
      },
    ];

    const traffic = new TrafficSimulator(nodes, edges);
    traffic.addToScene(scene);
    expect(() => traffic.tick(Number.NaN)).not.toThrow();
    expect(() => traffic.tick(-0.5)).not.toThrow();
    traffic.removeFromScene(scene);

    const emptyCars = new CarSimulator([], [], null);
    expect(() => emptyCars.spawnCar()).not.toThrow();
    expect(() => emptyCars.tick(Number.NaN)).not.toThrow();

    const cars = new CarSimulator(nodes, edges, traffic);
    cars.addToScene(scene);
    cars.spawnCar();
    cars.tick(0.016);
    cars.removeFromScene(scene);
    // Re-adding recreates disposed carGeo / glowTexture cleanly
    cars.addToScene(scene);
    cars.spawnCar();
    expect(cars.getCarInfos().length).toBeGreaterThanOrEqual(1);
    cars.removeFromScene(scene);
  });

  it('handles GLBRoadsLoader disposal before load completion and LabelOverlay edge cases', () => {
    mockCanvas2dContext();
    const scene = new THREE.Scene();
    const roads = new GLBRoadsLoader(scene);
    roads.dispose();
    expect(roads.isLoaded()).toBe(false);

    const canvas = document.createElement('canvas');
    Object.defineProperty(canvas, 'getBoundingClientRect', {
      value: () => ({ left: 0, top: 0, width: 0, height: 0 }),
    });
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    const overlay = new LabelOverlay(scene, camera, canvas, () => {});
    canvas.dispatchEvent(new MouseEvent('mousemove', { clientX: 10, clientY: 10 }));
    overlay.updateRunnerWaterLevels(new Map([['sim-1', Number.NaN]]));
    expect(() => overlay.update()).not.toThrow();
    overlay.dispose();
    expect(canvas.style.cursor).toBe('');
  });

  it('prevents division-by-zero in viewport tickCameraIntro, tickCameraPan, tickRouteDraw, and tickZones', () => {
    const ctx = new Context();
    ctx.scene = new THREE.Scene();
    ctx.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    ctx.controls = { target: new THREE.Vector3(), update: vi.fn() } as any;

    const spline = new THREE.CatmullRomCurve3([
      new THREE.Vector3(0, 10, 0),
      new THREE.Vector3(10, 10, 10),
    ]);
    const doneIntro = vi.fn();
    ctx.cameraIntro = {
      active: true,
      movementHeld: false,
      elapsed: 0,
      duration: 0,
      spline,
      fromTarget: new THREE.Vector3(0, 0, 0),
      toTarget: new THREE.Vector3(5, 0, 5),
      windowLightStartT: 0.5,
      windowLightEndT: 0.5,
      windowMinZ: 0,
      windowMaxZ: 10,
      windowLightIndex: 0,
      onComplete: doneIntro,
    };

    tickCameraIntro(ctx, Number.NaN);
    expect(ctx.cameraIntro.active).toBe(true);
    tickCameraIntro(ctx, 0.1);
    expect(ctx.cameraIntro.active).toBe(false);
    expect(doneIntro).toHaveBeenCalledTimes(1);
    expect(Number.isFinite(ctx.camera.position.x)).toBe(true);

    const donePan = vi.fn();
    ctx.cameraPan = {
      active: true,
      elapsed: 0,
      duration: 0,
      fromPosition: new THREE.Vector3(0, 10, 0),
      toPosition: new THREE.Vector3(20, 10, 20),
      fromTarget: new THREE.Vector3(0, 0, 0),
      toTarget: new THREE.Vector3(10, 0, 10),
      onComplete: donePan,
    };
    tickCameraPan(ctx, 0.016);
    expect(ctx.cameraPan.active).toBe(false);
    expect(donePan).toHaveBeenCalledTimes(1);

    const routeGeo = new THREE.BufferGeometry();
    ctx.currentRoute = new THREE.Mesh(routeGeo, new THREE.MeshBasicMaterial());
    ctx.iconPickMeshes.push(ctx.currentRoute);
    ctx.routeDraw = { active: true, elapsed: 0, duration: 0, full: 100 };
    tickRouteDraw(ctx, Number.NaN);
    tickRouteDraw(ctx, 0.016);
    expect(ctx.routeDraw.active).toBe(false);

    removeRoute(ctx);
    expect(ctx.currentRoute).toBeNull();
    expect(ctx.iconPickMeshes.length).toBe(0);

    expect(() => tickZones(ctx, Number.NaN)).not.toThrow();
    expect(() => tickZones(ctx, -1)).not.toThrow();

    disposeSharedRouteResources();
    disposeSharedIconResources();
  });
});
