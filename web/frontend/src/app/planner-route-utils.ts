/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
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
 * Changed for The Rehearsal: clamp Haversine radicand against floating-point overshoot and guard malformed GeoJSON coordinate arrays.
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
}

export function tryImportPlannerRoute(text: string): void {
  const jsonMatch = text.match(/\{[\s\S]*\}/);
  if (!jsonMatch) return;
  try {
    const json = JSON.parse(jsonMatch[0]);

    // For tool_end messages, extract geojson from result
    // For other messages, check top-level or nested geojson
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
    if (!parsed) return;
    const name = parsed.name ?? fc.marathon_metadata?.start_location ?? 'Planner Route';
    window.dispatchEvent(
      new CustomEvent('hud:importPath', {
        detail: {
          coords: parsed.coords,
          name,
          waterStations: parsed.waterStations,
          medicalTents: parsed.medicalTents,
        },
      }),
    );
  } catch (e) {
    console.error('tryImportPlannerRoute failed:', e);
  }
}

export function computeDistance(coords: [number, number][]): number {
  let distM = 0;
  for (let i = 1; i < coords.length; i++) {
    const [x0, y0] = coords[i - 1];
    const [x1, y1] = coords[i];
    const R = 6371000;
    const dLat = ((y1 - y0) * Math.PI) / 180;
    const dLon = ((x1 - x0) * Math.PI) / 180;
    const rawA =
      Math.sin(dLat / 2) ** 2 +
      Math.cos((y0 * Math.PI) / 180) * Math.cos((y1 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
    const a = Math.min(1, Math.max(0, rawA));
    distM += R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }
  return distM;
}

export function parseGeoJSON(json: any): ParsedRoute | null {
  let features: any[] = [];

  if (json?.type === 'Feature') {
    if (json.geometry?.type === 'LineString' && Array.isArray(json.geometry.coordinates)) {
      return {
        coords: json.geometry.coordinates,
        name: json.properties?.name ?? null,
        waterStations: [],
        medicalTents: [],
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
    const c = lf.geometry.coordinates as [number, number][];
    coords.push(...c);
    if (!name && lf.properties?.name) name = lf.properties.name;
  }

  const extractStations = (type: string) =>
    features
      .filter((f: any) => f.properties?.type === type && f.geometry?.type === 'Point')
      .map((f: any) => ({
        id: f.id ?? 0,
        mi: f.properties?.mi ?? f.properties?.km_mark ?? 0,
        coords: f.geometry.coordinates as [number, number],
      }));

  const waterStations = extractStations('water_station');
  const medicalTents = extractStations('medical_tent');

  return { coords, name, waterStations, medicalTents };
}
