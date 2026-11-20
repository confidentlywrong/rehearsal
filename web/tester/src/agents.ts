/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 Christian Haeussinger
 * Changed for The Rehearsal: unify gateway HTTP base URL normalization and validate session creation payloads.
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

export interface Skill {
  id: string;
  name: string;
  description: string;
  tags: string[];
}

export interface AgentCard {
  name: string;
  description: string;
  version: string;
  url: string;
  preferred_transport: string;
  skills: Skill[];
}

export interface AgentCatalog {
  [key: string]: AgentCard;
}

export function resolveGatewayHttpBase(gatewayUrl: string): string {
  const trimmed = gatewayUrl.trim();
  if (!trimmed) {
    throw new Error("Gateway URL is required");
  }
  if (trimmed.startsWith("//") || trimmed.startsWith("/\\")) {
    throw new Error("Invalid relative gateway URL");
  }
  let baseUrl = trimmed;
  if (trimmed.startsWith("/")) {
    baseUrl = window.location.origin + trimmed;
  }
  return baseUrl
    .replace(/^ws(s?):\/\//i, (_m, s) => (s ? "https://" : "http://"))
    .replace(/\/ws\/?(?:\?.*)?$/i, "")
    .replace(/\/+$/, "");
}

export async function fetchAgentTypes(
  gatewayUrl: string,
): Promise<AgentCatalog> {
  const baseUrl = resolveGatewayHttpBase(gatewayUrl);

  const response = await fetch(`${baseUrl}/api/v1/agent-types`);
  if (!response.ok) {
    throw new Error(`Failed to fetch agent types: ${response.statusText}`);
  }
  const data = await response.json();
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("Invalid agent types response payload");
  }
  return data as AgentCatalog;
}

export async function createAgentSession(
  agentUrl: string,
  userId: string = "tester-ui",
): Promise<string> {
  const baseUrl = agentUrl.trim().replace(/\/+$/, "");
  if (!baseUrl) {
    throw new Error("Agent URL is required");
  }
  // ADK create_session call
  const response = await fetch(`${baseUrl}/create_session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      user_id: userId,
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to create agent session: ${response.statusText}`);
  }

  const data = await response.json();
  const sessionId =
    data && typeof data === "object"
      ? (data as Record<string, unknown>).id ??
        (data as Record<string, unknown>).session_id
      : undefined;
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    throw new Error("Failed to create agent session: missing session id");
  }
  return sessionId;
}

export async function createOrchestratedSession(
  gatewayUrl: string,
  agentType: string,
  userId: string = "tester-ui",
): Promise<string> {
  const trimmedType = agentType.trim();
  if (!trimmedType) {
    throw new Error("Agent type is required");
  }
  const baseUrl = resolveGatewayHttpBase(gatewayUrl);

  const response = await fetch(`${baseUrl}/api/v1/sessions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      agentType: trimmedType,
      userId,
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to trigger orchestration: ${response.statusText}`);
  }

  const data = await response.json();
  const sessionId =
    data && typeof data === "object"
      ? (data as Record<string, unknown>).sessionId
      : undefined;
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    throw new Error("Failed to trigger orchestration: missing sessionId");
  }
  return sessionId;
}
