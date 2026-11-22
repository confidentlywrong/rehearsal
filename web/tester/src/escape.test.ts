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

import { describe, expect, it } from "vitest";
import { MessageBoxRenderer } from "./a2ui/components/messageBox";
import { escapeHTML } from "./escape";

describe("Round 4 — tester escaping & MessageBoxRenderer safety", () => {
  it("escapes HTML metacharacters and coerces nullish/non-string values safely", () => {
    expect(escapeHTML(null)).toBe("");
    expect(escapeHTML(undefined)).toBe("");
    expect(escapeHTML(0)).toBe("0");
    expect(escapeHTML(`<img src=x onerror="alert('xss')">&'`)).toBe(
      "&lt;img src=x onerror=&quot;alert(&#39;xss&#39;)&quot;&gt;&amp;&#39;",
    );
  });

  it("renders non-string speaker/typeLabel/rawMessage without script injection", () => {
    const el = MessageBoxRenderer({
      speaker: "<script>evil()</script>",
      typeLabel: "<b onclick=alert(1)>TOOL</b>",
      text: null,
      rawMessage: { note: "</script><script>alert(1)</script>" },
      sessionId: "sess-<1>",
      simulationId: "sim-&-2",
    });

    expect(el.querySelector("script")).toBeNull();
    expect(el.textContent).toContain("<script>evil()</script>");
    expect(el.textContent).toContain("<b onclick=alert(1)>TOOL</b>");
  });
});
