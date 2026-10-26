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

import { Injectable } from '@angular/core';

export type AiProvider = 'cloud_gemini' | 'local_gemma';

export interface ModelTier {
  id: string;
  model: string;
  label: string;
  subtitle: string;
  size_gb: number;
  min_ram_gb: number;
  fits_ram: boolean;
  installed: boolean;
}

export interface EmbedEngineStatus {
  model: string;
  configured_model: string;
  label: string;
  subtitle: string;
  size_gb: number;
  native_dim: number;
  pgvector_dim: number;
  installed: boolean;
}

export interface HardwareProfile {
  platform: string;
  arch: string;
  chip: string;
  total_ram_gb: number;
  recommended_tier: string;
  recommended_model: string;
}

export interface PullProgress {
  active: boolean;
  model?: string;
  status?: string;
  percent: number;
  completed_bytes: number;
  total_bytes: number;
  done: boolean;
  error?: string;
  updated_at?: string;
}

export interface AiEngineStatus {
  provider: AiProvider;
  endpoint: string;
  text_model: string;
  embed_model: string;
  cloud_project_configured: boolean;
  connected: boolean;
  protocol?: string;
  latency_ms?: number;
  binary_installed: boolean;
  binary_path?: string;
  text_model_ready: boolean;
  embed_model_ready: boolean;
  installed_models: string[];
  hardware: HardwareProfile;
  model_tiers: ModelTier[];
  embed_engine: EmbedEngineStatus;
  pull_progress: PullProgress;
}

export interface AiConfigPayload {
  provider: AiProvider;
  endpoint: string;
  text_model: string;
  embed_model: string;
}

export interface AiTestResponse {
  reachable: boolean;
  endpoint: string;
  installed_models: string[];
  error: string;
  status: AiEngineStatus;
}

export interface AiPullResponse {
  pull_progress: PullProgress;
  status: AiEngineStatus;
  error?: string;
}

@Injectable({ providedIn: 'root' })
export class AiEngineService {
  async getStatus(endpoint?: string): Promise<AiEngineStatus> {
    const trimmed = endpoint?.trim();
    const url = trimmed
      ? `/api/v1/settings/ai?endpoint=${encodeURIComponent(trimmed)}`
      : '/api/v1/settings/ai';
    const resp = await fetch(url, {
      credentials: 'include',
    });
    if (!resp.ok) {
      throw new Error(await this.extractError(resp, 'Failed to load AI Engine status'));
    }
    return (await resp.json()) as AiEngineStatus;
  }

  async saveConfig(payload: AiConfigPayload): Promise<AiEngineStatus> {
    const resp = await fetch('/api/v1/settings/ai', {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      throw new Error(await this.extractError(resp, 'Failed to save AI Engine settings'));
    }
    return (await resp.json()) as AiEngineStatus;
  }

  async testConnection(endpoint: string, autoStart = true): Promise<AiTestResponse> {
    const resp = await fetch('/api/v1/settings/ai/test', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint, auto_start: autoStart }),
    });
    if (!resp.ok) {
      throw new Error(await this.extractError(resp, 'Connection test failed'));
    }
    return (await resp.json()) as AiTestResponse;
  }

  async startDaemon(endpoint: string): Promise<AiEngineStatus> {
    const resp = await fetch('/api/v1/settings/ai/start', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint }),
    });
    if (!resp.ok) {
      throw new Error(await this.extractError(resp, 'Failed to start local Ollama daemon'));
    }
    return (await resp.json()) as AiEngineStatus;
  }

  async pullModel(model: string, endpoint: string): Promise<AiPullResponse> {
    const resp = await fetch('/api/v1/settings/ai/pull', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, endpoint }),
    });
    if (!resp.ok) {
      throw new Error(await this.extractError(resp, `Failed to pull model ${model}`));
    }
    return (await resp.json()) as AiPullResponse;
  }

  private async extractError(resp: Response, fallback: string): Promise<string> {
    try {
      const body = (await resp.json()) as { error?: string };
      if (body?.error) {
        return body.error;
      }
    } catch {
      // ignore JSON parse errors
    }
    return `${fallback} (HTTP ${resp.status})`;
  }
}
