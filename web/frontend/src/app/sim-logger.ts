/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: bound log buffer capacity and guard missing navigator.clipboard or performance APIs.
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

const MAX_SIM_LOG_ENTRIES = 5000;

function nowMs(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now();
}

class SimLogger {
  private entries: string[] = [];
  private t0 = 0;

  clear(): void {
    this.entries = [];
    this.t0 = nowMs();
  }

  log(category: string, id: string, msg: string): void {
    const now = nowMs();
    if (!this.t0) this.t0 = now;
    const t = ((now - this.t0) / 1000).toFixed(3);
    const safeId = id == null ? '' : String(id);
    const short = safeId.length > 20 && !safeId.includes(' ') ? safeId.slice(-8) : safeId;
    this.entries.push(`[${t}s] [${category}] ${short}: ${msg}`);
    if (this.entries.length > MAX_SIM_LOG_ENTRIES) {
      this.entries.splice(0, this.entries.length - MAX_SIM_LOG_ENTRIES);
    }
  }

  copyToClipboard(): Promise<void> {
    if (!this.entries.length) return Promise.resolve();
    if (
      typeof navigator === 'undefined' ||
      !navigator.clipboard ||
      typeof navigator.clipboard.writeText !== 'function'
    ) {
      return Promise.resolve();
    }
    return navigator.clipboard.writeText(this.entries.join('\n')).catch(() => {});
  }

  get size(): number { return this.entries.length; }
}

export const simLog = new SimLogger();
