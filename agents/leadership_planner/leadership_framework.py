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

"""Leadership × Innovation-Management Framework — doctrinal reference tables.

This module is the SINGLE source of truth for the doctrinal lens the
leadership planner, evaluator and consultant agents use when designing
scenarios, scoring plans, and writing coaching insights.

It is intentionally a plain Python module of string constants — NOT a
RAG-indexed corpus — because the content is:

* Small (a dozen rows today).
* Stable (the doctrine changes deliberately, not by retrieval).
* Cited verbatim (the evaluator's score keys must match Table B's
  principle labels 1:1 — drift breaks downstream key matching).

How to extend
=============
1. Add a new module-level constant containing the markdown table:

       TABLE_X = '''\\
       ## Table X — <short name>
       <one-line "use this when ..." sentence>

       | Column A | Column B | Column C |
       |----------|----------|----------|
       | ...      | ...      | ...      |
       '''

2. Append the new constant to ``_TABLES`` below.  The framework string
   ``FRAMEWORK_PROMPT`` is reassembled in order — that is what the
   planner prompt ultimately consumes.

3. If the new table introduces canonical labels that downstream code
   pattern-matches against (e.g. evaluator score keys), update the
   relevant whitelist in the consumer module at the same time.

4. Run ``uv run pytest agents/leadership_planner/`` to confirm prompt
   assembly + downstream tests still pass.

Do NOT add prose paragraphs to this module — the planner already has a
SCENARIO_QUALITY section, a WORKFLOW section, etc.  Keep this file for
*tables* only.
"""


# ---------------------------------------------------------------------------
# Preamble — appears once at the top of the framework block
# ---------------------------------------------------------------------------

_PREAMBLE = """\
# Leadership × Innovation-Management Framework (reference doctrine)
This is the doctrinal lens for scenario design, evaluation and coaching.
Cite specific rows when you justify a decision, surface a finding, or write
a coaching insight — do NOT invent alternate principles or pressures."""


# ---------------------------------------------------------------------------
# Table A — Environmental Pressure → Algorithmic Effect → Strategic Mandate
# ---------------------------------------------------------------------------

TABLE_A_ENVIRONMENTAL_PRESSURE = """\
## Table A — Environmental Pressure → Algorithmic Effect → Strategic Mandate
Use this when designing crisis events, framing the macro climate of a
scenario, or interpreting why a simulation produced its observed pathways.

| Environmental Pressure       | Algorithmic Effect on Execution                                                                                       | Strategic Innovation Mandate                                                                                                  |
|------------------------------|-----------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| **Extreme Heat** (The Burning Platform) | Time Cost drops (faster). Morale Cost spikes (exhaustion). Urgency compresses deliberation cycles but burns out the org. | **Exploit the Urgency.** Engage the absolute minimum coalition for consensus. Do NOT try to bring everyone over. Accept the emotional tax. |
| **The Storm** (Market Disruption)        | Global Resistance increases. Capital and Time Costs spike. Communication degrades and defensive stakeholders demand budget to mitigate perceived risk. | **Sheltered Resilience.** Abandon aggressive efficiency. Seek routes through natural allies, attempting to bridge to resistance holdouts. |
| **High Wind** (Organizational Turbulence) | Algorithm Variance widens. Cost remains baseline, but the perturbation factor doubles, producing highly volatile, unpredictable pathways. | **Real Options Theory.** Do NOT use a rigid, 12-step engagement plan. Generate multiple divergent options and hedge for the most robust. |
| **Clear Skies** (The Innovator's Dilemma) | Baseline 1.0 (no friction modifiers). Stakeholder resistance rests at its natural, comfortable level. There is zero external urgency. | **Manufacture the Crisis.** Paradoxically, this is the hardest condition for transformation. To initiate change, the leader must artificially manufacture a burning platform. |
| **The Fog** (Strategic Uncertainty)      | Influence Distance amplifies. Time and Morale Costs rise slightly. Vague mandates push already-distant stakeholders further away, paralyzing consensus. | **Discovery-Driven Planning.** You cannot reach across the org in a fog. Focus exclusively on stakeholders who are already near; build an inner core of conviction. |"""


# ---------------------------------------------------------------------------
# Table B — Leadership Principle → Innovation-Mgmt Context → High-Leverage Action
# ---------------------------------------------------------------------------

TABLE_B_LEADERSHIP_PRINCIPLES = """\
## Table B — Leadership Principle → Innovation-Mgmt Context → High-Leverage Action
Use this when evaluating a plan, surfacing findings, generating coaching
insights, or writing the 90-Day Action Plan.  These seven principles are
the canonical rubric — the evaluator's score keys map to them 1:1.

| Leadership Principle      | Innovation-Management Context                                                                                                      | High-Leverage Strategic Action                                                                                                            |
|---------------------------|------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------|
| **Strategic Foresight**   | Innovation cannot survive in a vacuum; it requires a documented horizon. The engine penalizes short-term tactical pivots that lack long-term context. | **Publish the Horizon.** Explicitly communicate the 3-year disruption roadmap to all operational departments to establish a shared anchor for change. |
| **Adaptive Decision-Making** | Bureaucracy demands rigidity; innovation demands iteration. The engine rewards leaders who systematically de-risk failure before demanding scale. | **De-Risk the Rollout.** Mandate a localized, 2-week operational pilot before attempting to force enterprise-wide adoption.                |
| **Stakeholder Engagement** | Alignment is a decaying isotope. The engine demands continuous, active injections of leadership energy to maintain coalition momentum. | **Surface the Telemetry.** Proactively share organizational well-being (OWB) data with the frontline, and schedule direct 1:1 interventions with explicitly resistant Gatekeepers. |
| **Ethical Leadership**    | Disruption creates profound anxiety regarding redundancy and fairness. The engine measures whether a leader protects the ecosystem while altering it. | **Architect the Guardrails.** Commission an immediate algorithmic-bias audit and publish a transparent AI-ethics charter before deployment. |
| **Empowerment**           | Top-down mandates destroy frontline bandwidth. The engine rewards the decentralization of authority to the nodes closest to the execution. | **Decentralize Authority.** Explicitly delegate vendor selection or operational tuning directly to the engineering and frontline logistics teams. |
| **Resilience**            | Strategic inflection points guarantee chaos. The engine tests whether a leader has engineered organizational shock absorbers for when budgets or timelines collapse. | **Engineer Contingency.** Formally establish a crisis-command team and publish a strict operational fallback plan for the "Capital Depletion" scenario. |
| **Resource Optimization** | Visionary ambition frequently ignores operational limits. The engine demands that leaders act as strict stewards of finite organizational oxygen. | **Reallocate Bandwidth.** Defend the core initiative by aggressively halting low-impact legacy projects to free up Team Bandwidth (TBU) for the transformation. |"""


# ---------------------------------------------------------------------------
# How-to-apply footer — appears once at the bottom of the framework block
# ---------------------------------------------------------------------------

_APPLICATION_GUIDE = """\
## How to apply the framework
- **Scenario design**: pick ONE primary Environmental Pressure from Table A
  and weave its Algorithmic Effect into the resource model + crisis events.
  At least three of the seven Leadership Principles from Table B must be
  testable by the decision rounds you generate.
- **Evaluation**: when reporting findings, name the specific Leadership
  Principle the finding maps to (verbatim from Table B). When suggesting
  improvements, prefer the matching High-Leverage Strategic Action.
- **Coaching**: the Kotter / ADKAR insights you emit at the end of a
  simulation MUST cite at least one Table-A pressure interaction and one
  Table-B principle that the simulated leader handled well or poorly.
- Use the canonical bolded labels exactly as written.  Do NOT invent
  alternates like "Strategic Vision" for "Strategic Foresight" — that
  breaks downstream evaluator key matching."""


# ---------------------------------------------------------------------------
# Assembled framework prompt — append new tables to _TABLES below.
# ---------------------------------------------------------------------------

_TABLES: tuple[str, ...] = (
    TABLE_A_ENVIRONMENTAL_PRESSURE,
    TABLE_B_LEADERSHIP_PRINCIPLES,
    # Add new tables here — they will be rendered in this order between the
    # preamble and the application guide.
)


FRAMEWORK_PROMPT: str = "\n\n".join((_PREAMBLE, *_TABLES, _APPLICATION_GUIDE))
"""The complete framework block, ready to inject into a PromptBuilder section.

Consumed by ``agents.leadership_planner.prompts`` (see the ``framework`` key
in ``LEADERSHIP_PLANNER``).  All downstream agents that override the planner
inherit it automatically through ``PromptBuilder.override``.
"""


__all__ = [
    "FRAMEWORK_PROMPT",
    "TABLE_A_ENVIRONMENTAL_PRESSURE",
    "TABLE_B_LEADERSHIP_PRINCIPLES",
]
