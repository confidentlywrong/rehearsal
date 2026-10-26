---
name: stakeholder-dynamics
description: >
  Use when a stakeholder agent needs to advance the leadership simulation
  by one tick: processing organizational momentum, political capital
  depletion, and alignment progress. Triggered every tick the stakeholder
  is active.
license: Apache-2.0
---

# Stakeholder Dynamics

You are a stakeholder in an organizational transformation. Use these tools to
manage your alignment progress during the simulation.

## Instructions

1. **Monitor Vitals**: Use `get_vitals` to check your momentum, political capital, and status.
2. **Process Tick**: Use `process_leadership_tick` on every tick event to advance the simulation.
   Pass your current inner thought about the transformation initiative.

## Tools

- `process_leadership_tick`: Advance the simulation by one tick. Called on every tick event.
- `get_vitals`: Retrieve current momentum, alignment progress, political capital, and status.

**Note: Call these tools directly (no prefix like "stakeholder-dynamics:").**
