---
name: translation-diagnostic
description: "Use when analysing the leader's rollout communication for System–Lifeworld translation gaps after a simulation."
metadata:
  adk_additional_tools:
    - emit_translation_diagnostic
    - emit_stakeholder_psychology
---

# Translation Diagnostic & Stakeholder Psychology

Post-simulation analytical tools that examine both sides of the
leader–stakeholder communication dynamic.

## Tools

### `emit_translation_diagnostic`

Analyses the runner/pitcher's rollout communication and identifies where
the leader's **System language** collides with each stakeholder's **Lifeworld**.
Focuses on **inflection points** — the ticks where stakeholder sentiment
shifted most dramatically.

**Output:** Per-stakeholder trigger phrases, gap types, and suggested reframings.

**Arguments:** None (reads `echo_voice_log` and `decision_log` from session state).

### `emit_stakeholder_psychology`

Analyses each stakeholder's psychological profile after a simulation.
Fully deterministic — no LLM call needed.

**Output:** Per-stakeholder profiles including:
- **Willingness to Transform (WTT) Score** (0–100) with 4-factor breakdown
- **Malicious Compliance Risk** (high/medium/low) — detects gap between displayed stance and operative behavior
- **Sentiment Trajectory** — trust evolution over the simulation
- **Trust Architecture** — contradiction breaches, escalation events, terminal states

**Arguments:** None (reads all data from session state).
