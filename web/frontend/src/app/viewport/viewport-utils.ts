/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: guard computeDistance and parseGeoJSON against malformed geometries, missing tuples, and non-finite coordinates.
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
 *
 * Changed for The Rehearsal: clamp Haversine radicand against floating-point overshoot and guard malformed GeoJSON coordinate arrays and station points.
 */

/**
 * Utilities for parsing planner route GeoJSON from agent tool output
 * and dispatching it to the 3D layer via hud:importPath.
 */

export interface ParsedRoute {
  coords: [number, number][];
  name: string | null;
  waterStations: { id: number; mi: number; coords: [number, number] }[];
  medicalTents: { id: number; mi: number; coords: [number, number] }[];
  crowdZones: { id: number; mi: number; coords: [number, number] }[];
  portableToilets: { id: number; mi: number; coords: [number, number] }[];
}

/**
 * Parses GeoJSON route data from agent tool output and dispatches
 * it to the 3D viewport via the hud:importPath event.
 * Called from AgentScreen via gateway:routeGeojson event.
 */
export function visualizeRoutePlan(text: string): void {
  const jsonMatch = text.match(/\{[\s\S]*\}/);
  if (!jsonMatch) return;
  try {
    const json = JSON.parse(jsonMatch[0]);

    let fc: any = null;
    if (json?.type === 'tool_end' && json?.result?.geojson) {
      fc = json.result.geojson;
    } else if (json?.type === 'FeatureCollection' || Array.isArray(json?.features)) {
      fc = json;
    } else if (json?.result?.geojson) {
      fc = json.result.geojson;
    } else if (json?.geojson) {
      fc = json.geojson;
    } else if (json?.route_geojson) {
      fc = json.route_geojson;
    }

    if (!fc || !Array.isArray(fc.features)) return;

    if (!fc.type) fc.type = 'FeatureCollection';

    const parsed = parseGeoJSON(fc);
    if (!parsed || parsed.coords.length < 2) return;

    const name = parsed.name ?? fc.marathon_metadata?.start_location ?? 'Planner Route';

    window.dispatchEvent(
      new CustomEvent('hud:importPath', {
        detail: {
          coords: parsed.coords,
          name,
          waterStations: parsed.waterStations,
          medicalTents: parsed.medicalTents,
          crowdZones: parsed.crowdZones,
          // portableToilets: parsed.portableToilets,
          showOldRoute: true,
        },
      }),
    );
  } catch (e) {
    console.error('visualizeRoutePlan failed:', e);
  }
}

export function computeDistance(coords: [number, number][]): number {
  if (!Array.isArray(coords) || coords.length < 2) return 0;
  let distM = 0;
  let prev: [number, number] | null = null;
  const R = 6371000;
  for (let i = 0; i < coords.length; i++) {
    const pt = coords[i];
    if (
      !Array.isArray(pt) ||
      pt.length < 2 ||
      !Number.isFinite(pt[0]) ||
      !Number.isFinite(pt[1])
    ) {
      continue;
    }
    if (prev) {
      const [x0, y0] = prev;
      const [x1, y1] = pt;
      const dLat = ((y1 - y0) * Math.PI) / 180;
      const dLon = ((x1 - x0) * Math.PI) / 180;
      const rawA =
        Math.sin(dLat / 2) ** 2 +
        Math.cos((y0 * Math.PI) / 180) * Math.cos((y1 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
      const a = Math.min(1, Math.max(0, rawA));
      distM += R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }
    prev = pt;
  }
  return distM;
}

function isFiniteCoordTuple(pt: unknown): pt is [number, number] {
  return (
    Array.isArray(pt) &&
    pt.length >= 2 &&
    typeof pt[0] === 'number' &&
    typeof pt[1] === 'number' &&
    Number.isFinite(pt[0]) &&
    Number.isFinite(pt[1])
  );
}

export function parseGeoJSON(json: any): ParsedRoute | null {
  let features: any[] = [];

  if (json?.type === 'Feature') {
    if (json.geometry?.type === 'LineString' && Array.isArray(json.geometry.coordinates)) {
      const coords = json.geometry.coordinates.filter(isFiniteCoordTuple);
      if (coords.length === 0) return null;
      return {
        coords,
        name: typeof json.properties?.name === 'string' ? json.properties.name : null,
        waterStations: [],
        medicalTents: [],
        crowdZones: [],
        portableToilets: [],
      };
    }
    return null;
  } else if (json?.type === 'FeatureCollection' || Array.isArray(json?.features)) {
    features = Array.isArray(json.features) ? json.features : [];
  } else {
    return null;
  }

  // Collect all LineString features and concatenate their coordinates
  const lineFeatures = features.filter(
    (f: any) => f?.geometry?.type === 'LineString' && Array.isArray(f.geometry.coordinates),
  );
  if (lineFeatures.length === 0) return null;

  const coords: [number, number][] = [];
  let name: string | null = null;
  for (const lf of lineFeatures) {
    const c = (lf.geometry.coordinates as unknown[]).filter(isFiniteCoordTuple);
    coords.push(...c);
    if (!name && typeof lf.properties?.name === 'string' && lf.properties.name) {
      name = lf.properties.name;
    }
  }
  if (coords.length === 0) return null;

  const extractStations = (type: string) =>
    features
      .filter(
        (f: any) =>
          f?.properties?.type === type &&
          f?.geometry?.type === 'Point' &&
          isFiniteCoordTuple(f.geometry.coordinates),
      )
      .map((f: any) => ({
        id: f.id ?? 0,
        mi: f.properties?.mi ?? f.properties?.mile ?? f.properties?.km_mark ?? f.properties?.km ?? 0,
        coords: f.geometry.coordinates as [number, number],
      }));

  const waterStations = extractStations('water_station');
  const medicalTents = extractStations('medical_tent');
  const crowdZones = [
    ...extractStations('cheer_zone'),
    ...extractStations('crowd_zone'),
  ];
  const portableToilets = extractStations('portable_toilet');

  return { coords, name, waterStations, medicalTents, crowdZones, portableToilets };
}
