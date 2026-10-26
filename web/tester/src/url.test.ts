/**
 * Copyright 2026 Google LLC
 * Modifications copyright 2026 The Confidently Wrong Authors
 * Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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

import { describe, it, expect } from "vitest";
import { wsToHttp } from "./url";

describe("wsToHttp", () => {
  it("converts ws:// to http:// and strips /ws path", () => {
    expect(wsToHttp("ws://localhost:9101/ws")).toBe("http://localhost:9101");
  });

  it("converts wss:// to https:// and strips /ws path", () => {
    expect(wsToHttp("wss://tester.sim.example.com/ws")).toBe(
      "https://tester.sim.example.com",
    );
  });

  it("does not mangle wss:// into http://s://", () => {
    const result = wsToHttp("wss://example.com/ws");
    expect(result).toBe("https://example.com");
    expect(result).not.toContain("http://s://");
  });

  it("preserves path segments other than trailing /ws", () => {
    expect(wsToHttp("ws://localhost:9101/some/path/ws")).toBe(
      "http://localhost:9101/some/path",
    );
  });

  it("does not strip /ws if not at end of URL", () => {
    expect(wsToHttp("ws://localhost:9101/ws/extra")).toBe(
      "http://localhost:9101/ws/extra",
    );
  });

  it("handles URL without /ws path", () => {
    expect(wsToHttp("wss://example.com")).toBe("https://example.com");
  });
});
