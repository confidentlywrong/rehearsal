---
name: consultant
description: "Use when generating simulation results dashboards and 90-Day Action Plan playbooks via A2UI."
metadata:
  adk_additional_tools:
    - emit_sim_results_a2ui
    - emit_action_plan_a2ui
---

# Leadership Consultant Tools

This skill provides the final steps of the Leadership Flight Simulator workflow. It allows the `leadership_planner_with_eval` agent to act as a Consultant and output structured dashboards and playbooks to the user's frontend.

## Tools

### `emit_sim_results_a2ui`

Deterministically emits the Simulation Results dashboard as a three-card A2UI layout (Main Results, Strategy Overview with Persona Breakdown, Coaching Insights). Reads `persona_status_counts` from session state to populate persona alignment percentages automatically.

**Arguments:**
- `scenario_name`: Title of the simulation scenario.
- `overall_score`: Overall evaluation score (0-100 integer).
- `stakeholder_alignment`: Stakeholder Alignment score (0-100).
- `decision_quality`: Decision Quality score (0-100).
- `crisis_readiness`: Crisis Readiness score (0-100).
- `coaching_kotter_title`: Kotter coaching insight title.
- `coaching_kotter_desc`: Kotter coaching insight description.
- `coaching_adkar_title`: ADKAR coaching insight title.
- `coaching_adkar_desc`: ADKAR coaching insight description.
- `coaching_synthesis`: One-sentence synthesis takeaway.

### `emit_action_plan_a2ui`

Emits the 90-Day Action Plan playbook as a markdown card to the frontend.

**Arguments:**
- `plan_markdown`: The detailed markdown content containing the 90-Day Action Plan.
