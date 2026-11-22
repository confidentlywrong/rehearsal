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
 * Changed for The Rehearsal: anchor WebSocket scheme conversion case-insensitively and strip trailing /ws/ and query strings.
 */

/** Convert a ws:// or wss:// URL to http:// or https:// base (strip /ws path and trailing slashes). */
export function wsToHttp(wsUrl: string): string {
  return wsUrl
    .trim()
    .replace(/^wss:\/\//i, "https://")
    .replace(/^ws:\/\//i, "http://")
    .replace(/\/ws\/?(?:\?.*)?$/i, "")
    .replace(/\/+$/, "");
}
