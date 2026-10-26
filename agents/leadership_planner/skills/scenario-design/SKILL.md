---
name: scenario-design
description: >
  Use when the leadership simulation needs a structured scenario designed
  with stakeholder topology, decision rounds, and resource allocation.
  Triggered by scenario-planning, stakeholder-design, or simulation-setup
  language.
license: Apache-2.0
metadata:
  adk_additional_tools:
    - design_leadership_scenario
    - report_leadership_scenario
    - plan_consensus_pathway
---

# Scenario Design

You use this skill to design the structure of a leadership simulation scenario.

## Overview

This skill generates complete leadership simulation configurations including
organizational structures, stakeholder personas, decision round configurations,
and resource allocation models.

## Tools

- `design_leadership_scenario(scenario_type, stakeholder_count, budget_allocation, complexity)`:
  Generate a complete scenario configuration.
  - `scenario_type`: Type of scenario (transformation, crisis, merger, innovation, compliance).
  - `stakeholder_count`: Number of stakeholder personas (default: 10).
  - `budget_allocation`: Budget strategy (balanced, aggressive, conservative).
  - `complexity`: Scenario complexity (simple, moderate, complex).
- `report_leadership_scenario()`: Emit the scenario to the gateway for visualization.
- `plan_consensus_pathway(scenario_type, budget_allocation, complexity, stakeholder_count, seed)`:
  Generate a DCNS consensus pathway.
  - Other args default from the current scenario context.

### Decision-Making Guidance
- For most requests, call `design_leadership_scenario()` with default arguments.
- Use `scenario_type` to match the user's requested scenario context.
- Use `complexity` to control the number of decision rounds and difficulty.
- This tool is idempotent: calling it twice returns the cached result.
