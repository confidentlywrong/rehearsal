# Copyright 2026 Christian Haeussinger
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

"""ADK tool loader for leadership_planner_with_memory.

Mirrors agents/planner_with_memory/adk_tools.py — combines all inherited
leadership_planner_with_eval tools with memory-specific tools.
"""

from google.adk.tools.function_tool import FunctionTool

from agents.leadership_planner_with_eval.adk_tools import get_tools as get_eval_tools
from agents.leadership_planner_with_memory.memory.adk_tools import get_memory_tools
from agents.leadership_planner_with_memory.simulation_report import (
    emit_simulation_report,
)
from agents.leadership_planner_with_memory.simulation_timeline import (
    emit_simulation_timeline,
)
from agents.leadership_planner_with_memory.stakeholder_graph import emit_stakeholder_graph
from agents.leadership_planner_with_memory.stakeholder_interview import (
    emit_stakeholder_interview,
)
from agents.leadership_planner_with_memory.stakeholder_personas import (
    emit_stakeholder_personas,
)
from agents.leadership_planner_with_memory.stakeholder_reactions import (
    emit_stakeholder_reactions,
)


def get_tools() -> list:
    """Return all tools for the leadership_planner_with_memory agent.

    Inherits every tool from leadership_planner_with_eval and adds:
      * ``emit_stakeholder_graph`` (Step 0 — the keynote-demo
        relationship-graph extraction the agent must call first).
      * ``emit_stakeholder_personas`` (Step 0.25 — Artifact 1: one
        synthetic persona per Actor/Group node from the Step-0 graph,
        carrying archetype, stance, sentiment_bias, influence_weight,
        and response_delay_sprints. Feeds the multi-tick simulation
        loop in Artifact 2).
      * ``emit_stakeholder_reactions`` (regime-B single-tick simulation
        — one synthetic reaction per stakeholder from the Step-0 graph;
        the agent's prompt must opt in to call it, otherwise it stays
        dormant).
      * ``emit_simulation_timeline`` (Step 0.75 — Artifact 2: 1–8 ticks
        of (stance, sentiment) shifts across sprints, one row per
        persona per tick. Reads ``state["stakeholder_personas"]`` as
        the actor set; produces the multi-tick projection consumed by
        the future after-action report and interview tools).
      * ``emit_simulation_report`` (Step 0.9 — Artifact 3: after-action
        report. Reads ``state["simulation_timeline"]`` and
        ``state["stakeholder_personas"]`` and distills them into a
        one-line headline, an outcome verdict
        (success/partial/contested/failed), an executive summary, 2–6
        key findings, optional per-stakeholder journey summaries, and
        0–5 prioritized recommendations.).
      * ``emit_stakeholder_interview`` (Step 1.0 — Artifact 4:
        stakeholder interview. Reads ``state["stakeholder_personas"]``
        and supplies a 1–8 turn deep-dive Q&A with a single persona,
        grounded in the timeline + report. May be called multiple
        times — once per persona the agent wants to surface.).
      * The 12 memory tools (store_scenario, record_scenario_outcome,
        store_stakeholder_artifacts, recall_stakeholder_artifacts,
        recall_scenarios, get_scenario, get_best_scenario,
        get_planned_scenarios_data, get_selected_scenarios,
        emit_scenario_list_a2ui, emit_scenario_detail_a2ui,
        retrieve_document_passages).
    """
    return (
        get_eval_tools()
        + [
            FunctionTool(emit_stakeholder_graph),
            FunctionTool(emit_stakeholder_personas),
            FunctionTool(emit_stakeholder_reactions),
            FunctionTool(emit_simulation_timeline),
            FunctionTool(emit_simulation_report),
            FunctionTool(emit_stakeholder_interview),
        ]
        + get_memory_tools()
    )
