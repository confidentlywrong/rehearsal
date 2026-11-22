/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: guard curves with fewer than 2 control points, zero length, or NaN/out-of-range parameters.
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

import * as THREE from 'three';
import { IMapPath } from './path';
import { StationZone } from './water-station';

/**
 * Lightweight adapter wrapping a CatmullRomCurve3 to satisfy IMapPath.
 * Used by viewport-lookdev.ts so Runner can traverse CatmullRom splines
 * without building full MapPath ribbon geometry.
 */
export class CatmullRomPathAdapter implements IMapPath {
  readonly allStations: StationZone[] = [];
  readonly lengthMi: number | null;
  private curve: THREE.CatmullRomCurve3 | null = null;
  private totalLen = 0;
  private validCurve = false;
  private fallbackPoint = new THREE.Vector3(0, 0, 0);

  constructor(
    curve: THREE.CatmullRomCurve3,
    options?: {
      lengthMi?: number;
      stations?: StationZone[];
    },
  ) {
    const pts = Array.isArray(curve?.points)
      ? curve.points.filter(
          (p): p is THREE.Vector3 =>
            !!p && Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.z),
        )
      : [];
    if (pts.length > 0) {
      this.fallbackPoint = pts[0].clone();
    }

    if (pts.length >= 2) {
      const targetCurve =
        pts.length === curve.points.length ? curve : new THREE.CatmullRomCurve3(pts);
      this.curve = targetCurve;
      let rawLen = 0;
      try {
        rawLen = targetCurve.getLength();
      } catch {
        rawLen = 0;
      }
      this.totalLen = Number.isFinite(rawLen) && rawLen > 0 ? rawLen : 0;
      this.validCurve = this.totalLen > 0;
    }

    this.lengthMi =
      options?.lengthMi != null && Number.isFinite(options.lengthMi) && options.lengthMi >= 0
        ? options.lengthMi
        : null;
    if (Array.isArray(options?.stations)) this.allStations.push(...options.stations);
  }

  getPositionAt(t: number): THREE.Vector3 {
    if (!this.validCurve || !this.curve) return this.fallbackPoint.clone();
    const safeT = Number.isFinite(t) ? Math.max(0, Math.min(1, t)) : 0;
    const pos = this.curve.getPointAt(safeT);
    if (!Number.isFinite(pos.x) || !Number.isFinite(pos.y) || !Number.isFinite(pos.z)) {
      return this.fallbackPoint.clone();
    }
    return pos;
  }

  getTangentAt(t: number): THREE.Vector3 {
    if (!this.validCurve || !this.curve) return new THREE.Vector3(1, 0, 0);
    const safeT = Number.isFinite(t) ? Math.max(0, Math.min(1, t)) : 0;
    const tang = this.curve.getTangentAt(safeT);
    if (
      !Number.isFinite(tang.x) ||
      !Number.isFinite(tang.y) ||
      !Number.isFinite(tang.z) ||
      tang.lengthSq() === 0
    ) {
      return new THREE.Vector3(1, 0, 0);
    }
    return tang;
  }

  getTotalLength(): number {
    return this.totalLen;
  }
}
