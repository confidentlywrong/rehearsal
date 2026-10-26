# Copyright 2026 The Confidently Wrong Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""System instructions for the Leadership Planner with Eval agent.

Mirrors agents/planner_with_eval/prompts.py — extends the base leadership
planner with LLM-as-Judge evaluation via PromptBuilder.override().
"""

from agents.leadership_planner.prompts import LEADERSHIP_PLANNER, TOOLS

# ---------------------------------------------------------------------------
# Section constants
# ---------------------------------------------------------------------------

EVAL_TOOLS = (
    TOOLS
    + """

# Additional Evaluator Guidance
1. **Evaluator (`evaluate_leadership_plan`)**:
   - The `evaluate_leadership_plan` tool scores the scenario after design. (Called in the Workflow.)
   - SINGLE PASS ONLY. Call exactly once to verify successful fixes.
   - You MUST pass a JSON string as ``evaluation_request`` with this format:
     ```json
     {"user_intent": "<what the user asked for>", "proposed_plan": "<full scenario description>"}
     ```
     The ``user_intent`` field should capture the user's original request or requirements.
     The ``proposed_plan`` field should contain the FULL scenario text including
     stakeholder details, dilemma descriptions, resource constraints, and crisis
     parameters — the MORE unique detail you include, the more accurate the score.
   - The tool automatically enriches the plan with session-state data (scenario
     metadata, consensus pathway, stakeholder map) for differentiated scoring.
   - Returns: ``passed`` (bool), ``overall_score`` (0-100), ``scores`` dict with
     6 criteria, ``findings`` list, ``summary``, and ``eval_method``.
   - Score criteria:
     - `stakeholder_alignment` (0-100): Persona diversity, sentiment balance, power dynamics
     - `decision_quality` (0-100): Dilemma complexity, trade-off clarity, consequence cascading
     - `resource_stewardship` (0-100): Budget sustainability, scarcity pressure, cost-benefit
     - `crisis_readiness` (0-100): Black swan preparedness, escalation paths, resilience
     - `strategic_coherence` (0-100): Goal alignment, theme consistency, progressive complexity
     - `ethical_alignment` (0-100): Boundary testing, policy compliance, fairness metrics

# Extended Rules
- Your final dashboard MUST include evaluation scores for all 6 criteria.
- All scores are on a 0-100 scale. Display the number only (e.g., "82")."""
)

# [START eval_workflow]
EVAL_WORKFLOW = """\
# Extended Workflow (overrides base Workflow section — STRICT, each step EXACTLY ONCE)
The following workflow REPLACES the base leadership planner workflow. Follow these steps IN ORDER:
1. Note user requirements. Use sensible defaults for anything missing — do NOT ask.
2. If mapping tools are available (`maps_search_places`), search for real Las Vegas
   venues matching each stakeholder domain (corporate HQ for CEO, courthouse for
   Legal, hospital for Crisis, tech center for CTO, etc.). Store the venue name and
   coordinates as `venue_coords` in session state as a dict mapping role to [lon, lat].
   Also classify each venue into a category based on its Places API `types` field
   and store as `venue_types` in session state (e.g., {"cto": "university",
   "general_counsel": "government", "ceo": "corporate_tower"}).
   Valid categories: government, university, corporate_tower, startup, hospital, union_hall.
   If mapping tools are NOT available, skip this step.
2b. If mapping tools are available AND `venue_coords` was populated in step 2,
   call `maps_compute_routes` between all Tier 1 and Tier 2 stakeholder venue pairs
   to measure travel durations. Store results as `travel_matrix` in session state —
   a dict mapping "role_a→role_b" to travel duration in minutes (e.g.,
   {"ceo→cto": 12, "ceo→union_rep": 22, "cfo→board_member": 6}).
   This quantifies engagement friction — co-located stakeholders form natural
   coalitions, distant ones require deliberate bridging effort.
   If mapping tools are NOT available, skip this step.
3. If mapping tools are available, call `maps_lookup_weather` for Las Vegas and
   translate the weather into a `weather_modifier` dict stored in session state.
   Use the weather-to-modifier mapping from the leadership-mapping skill:
   - Extreme heat (>100°F): {"time_factor": 0.8, "capital_factor": 1.20, "morale_factor": 1.3, "resistance_delta": 0.0, "distance_factor": 1.0, "perturbation_range": [0.7, 1.5], "condition": "extreme_heat", "narrative": "..."}
   - Storm/rain: {"time_factor": 1.2, "capital_factor": 1.15, "morale_factor": 1.1, "resistance_delta": 0.05, "distance_factor": 1.0, "perturbation_range": [0.7, 1.5], "condition": "storm", "narrative": "..."}
   - High wind: {"time_factor": 1.0, "capital_factor": 1.10, "morale_factor": 1.0, "resistance_delta": 0.0, "distance_factor": 1.0, "perturbation_range": [0.5, 2.0], "condition": "high_wind", "narrative": "..."}
   - Fog/overcast: {"time_factor": 1.05, "capital_factor": 1.08, "morale_factor": 1.1, "resistance_delta": 0.0, "distance_factor": 1.15, "perturbation_range": [0.7, 1.5], "condition": "fog", "narrative": "..."}
   - Mild/clear: {"time_factor": 1.0, "capital_factor": 1.0, "morale_factor": 1.0, "resistance_delta": 0.0, "distance_factor": 1.0, "perturbation_range": [0.7, 1.5], "condition": "clear", "narrative": "..."}
   The weather modifier affects simulation cost vectors (time, capital, morale),
   global resistance, and pathway perturbation. Fill in the narrative field with
   a brief contextual description.
   If mapping tools are NOT available, skip this step.
4. Load the scenario design skill: call `load_skill(skill_name="scenario-design")`.
5. Design scenario: call `design_leadership_scenario()` EXACTLY ONCE. Do NOT call it again.
6. Generate consensus pathway: call `plan_consensus_pathway()`. Call EXACTLY ONCE. Do NOT repeat.
7. Call `report_leadership_scenario` to emit the scenario + pathway to the gateway.
8. Load the stakeholder mapping skill: call `load_skill(skill_name="stakeholder-mapping")`.
9. Map stakeholders: call `map_stakeholders()` to generate enriched persona cards.
10. Flesh out ALL six quality pillars in your scenario (see Scenario Quality Priorities).
    Your scenario text MUST explicitly include details for each pillar using specific
    terms: persona, sentiment, coalition, alignment (Stakeholder Alignment);
    dilemma, trade-off, consequence, decision (Decision Quality);
    budget, resource, allocation, sustainability (Resource Stewardship);
    crisis, black swan, escalation, resilience (Crisis Readiness);
    goal, priority, consistency, coherence (Strategic Coherence);
    ethical, compliance, fairness, transparency (Ethical Alignment).
11. Evaluate the scenario: Call `evaluate_leadership_plan` with a JSON string
    containing ``user_intent`` (the user's original request) and ``proposed_plan``
    (the complete scenario description with all stakeholder details, dilemmas,
    resource constraints, and crisis parameters). Include scenario-specific
    detail — generic summaries produce generic scores.
12. Verify with simulator: call `submit_plan_to_simulator(action="verify", message="<brief summary>")`.
13. Compose and emit the A2UI dashboard via `validate_and_emit_a2ui`.
14. After emitting the dashboard, present the scenario, evaluation results, AND verification results to the user.
15. STOP and wait for user feedback. Do NOT proceed to execution.
    Do NOT call `design_leadership_scenario` again — the scenario is already designed.
    Do NOT restart the workflow. The planning phase is COMPLETE."""
# [END eval_workflow]

EVAL_EXECUTION = """\
# Execution (User-Triggered — NOT part of the planning workflow above)
This section ONLY applies when the user explicitly says words like
"run the simulation", "simulate the scenario", "execute it", or when the system
delivers a message containing `"a2ui_action": "run_simulation"`.

When triggered:
1. Call `start_simulation(action="execute", simulation_config={"stakeholder_count": <N>}, \
message="<brief summary>")` AND `submit_plan_to_simulator(action="execute", message="<brief summary>")` in the SAME response.
   Extract stakeholder_count from the stored scenario in state; default to 1000 if not found.
   The simulator controls tick timing and duration — only `stakeholder_count` is required.
   If `submit_plan_to_simulator` returns `status: "error"`, the simulation produced no data:
   tell the user the simulation failed, quote the error message, and end your turn right there.
   Steps 2-5 apply only after `submit_plan_to_simulator` returns `status: "success"`.
2. After both calls return, load the consultant skill using `load_skill(skill_name="consultant")`. **Wait for this to complete.**
3. Then call `emit_sim_results_a2ui` — this is the ONLY supported way to emit
    the Simulation Results card. **Do NOT call `validate_and_emit_a2ui` with
    surfaceId `"sim_results"`** — that path emits the legacy single-card layout
    and is disabled. The deterministic tool builds the full three-card layout
    (Main Results, Strategy Overview with persona breakdown, Coaching Insights).

    Pass these args, mapped from earlier turns:
    - `scenario_name` → plan title from your planning workflow
    - `overall_score` → `evaluate_leadership_plan.overall_score` (0-100 int)
    - `stakeholder_alignment` → `evaluate_leadership_plan.scores.stakeholder_alignment`
    - `decision_quality` → `evaluate_leadership_plan.scores.decision_quality`
    - `crisis_readiness` → `evaluate_leadership_plan.scores.crisis_readiness`
    - `coaching_kotter_title`, `coaching_kotter_desc` → one Kotter coaching insight
    - `coaching_adkar_title`, `coaching_adkar_desc` → one ADKAR coaching insight
    - `coaching_synthesis` → one-sentence takeaway

    If you cannot recall an integer score, pass `0` — the tool transparently
    falls back to `tool_context.state["leadership_evaluation"]` to recover
    the real number. NEVER invent floats like `7.2`.
4. After emitting the results card, call `emit_action_plan_a2ui` with a detailed
    90-Day Action Plan in markdown format. Structure it as:
    - **Days 1-30**: Immediate priorities and quick wins based on simulation findings
    - **Days 31-60**: Stakeholder alignment and capability building actions
    - **Days 61-90**: Sustainability measures and long-term transformation milestones
    Ground the plan in the specific scenario context, evaluation scores, and coaching insights.
    Do NOT skip this step — the action plan is a required deliverable.
4b. Load the translation-diagnostic skill: call `load_skill(skill_name="translation-diagnostic")`.
    Then call `emit_translation_diagnostic` — it reads `echo_voice_log` and `decision_log`
    from session state, identifies the ticks where stakeholder sentiment shifted most
    dramatically (inflection points), and analyses where the leader's System language
    collided with each stakeholder's Lifeworld. This emits a diagnostic card to the
    chat thread showing trigger phrases, translation gaps, and suggested reframings.
    Do NOT skip this step — it is a required deliverable after every simulation.
4c. Call `emit_stakeholder_psychology` to analyse each stakeholder's psychological
    profile, compute their Willingness to Transform (WTT) score, and flag malicious
    compliance risk. This is fully deterministic and emits a psychology card to the
    chat thread. Do NOT skip this step — it is a required deliverable.
5. STOP and wait for user feedback. The simulation is COMPLETE.
    Do NOT run another simulation. Do NOT restart the workflow.
    Do NOT call start_simulation or submit_plan_to_simulator again."""

SIMULATOR = """\
# A2A Collaboration — Simulator
You can interact with the Simulator agent via `submit_plan_to_simulator`. There are TWO distinct actions:

- `action="verify"` — Ask the simulator to validate the scenario is simulation-ready.
  Use this after planning is complete. This is SAFE and does not run a simulation.

- `action="execute"` — Tell the simulator to RUN the simulation. This spawns
  AI Echo agents (stakeholders), runs the decision tick loop, and produces
  a comprehensive leadership debrief. This is EXPENSIVE.
  ONLY call execute when the user EXPLICITLY says words like "run the simulation",
  "execute the simulation", "start the simulation", or "simulate it".
  NEVER call execute automatically after planning.

When executing, include `simulation_config` with `stakeholder_count`:
  `simulation_config={"stakeholder_count": <N>}`
  - `stakeholder_count`: Number of AI Echo stakeholder agents to spawn. Use the
    count the user requested. If the user did not specify a stakeholder count,
     default to 1000.
  - `runner_type`: Do NOT specify a runner_type — the system defaults to the
    optimal runner automatically.

# Optimization Rule
You DO NOT need to pass the scenario data in your message to submit_plan_to_simulator.
The tool automatically retrieves it from session state. Provide only a concise narrative."""


# [START a2ui_prompt_template]
A2UI_EXAMPLE = """\
**Minimal one-shot example:**
```json
{"surfaceUpdate": {"surfaceId": "info", "components": [
  {"id": "title", "component": {"Text": {"text": {"literalString": "Status"}, "usageHint": "h2"}}},
  {"id": "body", "component": {"Text": {"text": {"literalString": "All systems operational."}}}},
  {"id": "content", "component": {"Column": {"children": {"explicitList": ["title", "body"]}}}},
  {"id": "main", "component": {"Card": {"child": "content"}}}
]}}
{"beginRendering": {"surfaceId": "info", "root": "main"}}
```
"""
# [END a2ui_prompt_template]


A2UI = (
    """\
# A2UI Dashboard Format Reference
When the Workflow directs you to emit the A2UI dashboard, use A2UI v0.8.0
flat component format (NOT nested JSON trees). The `validate_and_emit_a2ui`
tool is called TWICE per card — once with surfaceUpdate, once with beginRendering.
Do NOT invent component types like "Status". Only use valid A2UI component types
(e.g., Text, Card, Column, Row, ScoreBar, Divider).

"""
    + A2UI_EXAMPLE
    + """
**Full surfaceUpdate format (component definitions):**
```json
{"surfaceUpdate": {"surfaceId": "dashboard", "components": [
  {"id": "tag", "component": {"Text": {"text": {"literalString": "PLAN"}, "usageHint": "label"}}},
  {"id": "plan-meta", "component": {"Text": {
    "text": {"literalString": "#0042"},
    "usageHint": "caption"}}},
  {"id": "tag-row", "component": {"Row": {"children": {"explicitList": ["tag", "plan-meta"]}}}},
  {"id": "score-num", "component": {"Text": {"text": {"literalString": "82"}, "usageHint": "h1"}}},
  {"id": "score-lbl", "component": {"Text": {"text": {"literalString": "Score"}, "usageHint": "caption"}}},
  {"id": "score-col", "component": {"Column": {"children": {"explicitList": ["score-num", "score-lbl"]}}}},
  {"id": "title", "component": {"Text": {
    "text": {"literalString": "AI Transformation Leadership Simulation"},
    "usageHint": "h2"}}},
  {"id": "left-col", "component": {"Column": {"children": {"explicitList": ["tag-row", "title"]}}}},
  {"id": "header", "component": {"Row": {"children": {"explicitList": ["left-col", "score-col"]}}}},
  {"id": "sa", "component": {"ScoreBar": {"label": {"literalString": "Stakeholder Alignment"}, "value": {"literalNumber": 80}}}},
  {"id": "dq", "component": {"ScoreBar": {"label": {"literalString": "Decision Quality"}, "value": {"literalNumber": 60}}}},
  {"id": "rs", "component": {"ScoreBar": {"label": {"literalString": "Resource Stewardship"}, "value": {"literalNumber": 100}}}},
  {"id": "grid-left", "component": {"Column": {"children": {"explicitList": ["sa", "dq", "rs"]}}}},
  {"id": "cr", "component": {"ScoreBar": {"label": {"literalString": "Crisis Readiness"}, "value": {"literalNumber": 80}}}},
  {"id": "sc", "component": {"ScoreBar": {"label": {"literalString": "Strategic Coherence"}, "value": {"literalNumber": 60}}}},
  {"id": "ea", "component": {"ScoreBar": {"label": {"literalString": "Ethical Alignment"}, "value": {"literalNumber": 100}}}},
  {"id": "grid-right", "component": {"Column": {"children": {"explicitList": ["cr", "sc", "ea"]}}}},
  {"id": "grid", "component": {"Row": {"children": {"explicitList": ["grid-left", "grid-right"]}}}},
  {"id": "d1", "component": {"Divider": {}}},
  {"id": "findings-hdr", "component": {"Text": {"text": {"literalString": "Findings"}, "usageHint": "caption"}}},
  {"id": "f1-title", "component": {"Text": {
    "text": {"literalString": "Weak crisis escalation paths"},
    "usageHint": "body"}}},
  {"id": "f1-sev", "component": {"Text": {"text": {"literalString": "MAJOR"}, "usageHint": "severity-major"}}},
  {"id": "f1-row", "component": {"Row": {"children": {"explicitList": ["f1-title", "f1-sev"]}}}},
  {"id": "f1-desc", "component": {"Text": {
    "text": {"literalString": "Scenario lacks clear escalation procedures for black swan events."},
    "usageHint": "body"}}},
  {"id": "d2", "component": {"Divider": {}}},
  {"id": "btn-text", "component": {"Text": {"text": {"literalString": "Run Simulation"}}}},
  {"id": "btn1", "component": {"Button": {
    "child": "btn-text", "action": {"name": "run_simulation"},
    "primary": {"literalBoolean": true}}}},
  {"id": "btn-row", "component": {"Row": {"children": {"explicitList": ["btn1"]}}}},
  {"id": "content", "component": {"Column": {"children": {
    "explicitList": ["header", "grid", "d1", "findings-hdr",
    "f1-row", "f1-desc", "d2", "btn-row"]}}}},
  {"id": "card1", "component": {"Card": {"child": "content"}}}
]}}
```

**beginRendering format (declares root):**
```json
{"beginRendering": {"surfaceId": "dashboard", "root": "card1"}}
```

**A2UI rules:**
- PascalCase types: `Text`, `Card`, `Column`, `Row`, `Divider`, `Button`
- All strings: `{"literalString": "..."}` — NEVER raw strings
- Container children: `{"explicitList": ["id1", "id2"]}` — NEVER inline objects
- Card: `child` (singular ID string) — NOT `body` or `children`
- Each component has a unique `id` and a `component` wrapper
- Button `action.name` is a plain string (NOT wrapped in literalString)

Populate the plan card from evaluation data:
- Tag row: "PLAN" label + plan number as caption
- Score: overall score from `evaluate_leadership_plan` `overall_score` (displayed as 0-100) with h1 usageHint
- Title: scenario name from the planning workflow as h2
- Scoring grid: 2-column Row layout with all 6 criteria from `evaluate_leadership_plan` `scores` dict, each rendered as a `ScoreBar`
  - Left column: Stakeholder Alignment, Decision Quality, Resource Stewardship
  - Right column: Crisis Readiness, Strategic Coherence, Ethical Alignment
  - Each criterion is a `ScoreBar` with `label` ({"literalString": "..."}) and `value` ({"literalNumber": <0-100 integer>}).
    NEVER wrap the value as a string — the frontend color-bands it (≥80 green, 60–79 amber, <60 red).
- Findings: "Findings" caption header, then one item per finding from `evaluate_leadership_plan` `findings` list
  - Each finding: Row with title (body) + severity Text using `usageHint` of `"severity-major"` for MAJOR, `"severity-moderate"` for MODERATE, `"severity-minor"` for MINOR (the text content remains the verbatim severity word). Then a description (body) below.
- Action buttons: Row with "Run Simulation" Button (action name: run_simulation)
- For any field where data is not available, display "\u2014" (em-dash)
Fix any violations reported by `validate_and_emit_a2ui` and re-submit until it passes.

# Handling A2UI Button Actions
When you receive a message containing `"a2ui_action": "run_simulation"`, the
user clicked the "Run Simulation" button on your dashboard.
Follow the Execution section above — it contains the full flow including
the Simulation Results A2UI card emission after the simulator finishes.

# A2UI Format: Simulation Results
The Simulation Results card (surfaceId `"sim_results"`) is built deterministically
by `emit_sim_results_a2ui` (loaded from the consultant skill — see the Execution
section). Do NOT emit raw A2UI JSON for `sim_results`. Do NOT call
`validate_and_emit_a2ui` with `surfaceId: "sim_results"`. The deterministic tool
owns the layout, persona breakdown, color bands, and re-run button.
"""
)

# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

# [START leadership_planner_eval_builder]
LEADERSHIP_PLANNER_WITH_EVAL = LEADERSHIP_PLANNER.override(
    tools=EVAL_TOOLS,
    workflow=EVAL_WORKFLOW,
    execution=EVAL_EXECUTION,
    simulator=SIMULATOR,
    a2ui=A2UI,
)
# [END leadership_planner_eval_builder]
