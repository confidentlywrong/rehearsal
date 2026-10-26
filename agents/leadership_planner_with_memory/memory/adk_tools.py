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

"""ADK FunctionTool registry for leadership scenario memory tools.

Provides :func:`get_memory_tools` which returns all memory tools
wrapped as :class:`google.adk.tools.FunctionTool` instances, ready for
registration on an ADK agent.

Mirrors agents/planner_with_memory/memory/adk_tools.py but with
leadership-domain tool functions.
"""

from google.adk.tools.function_tool import FunctionTool

from agents.leadership_planner_with_memory.memory.retrieval_tools import (
    retrieve_document_passages,
)
from agents.leadership_planner_with_memory.memory.tools import (
    emit_scenario_detail_a2ui,
    emit_scenario_list_a2ui,
    get_best_scenario,
    get_planned_scenarios_data,
    get_scenario,
    get_selected_scenarios,
    recall_scenarios,
    recall_stakeholder_artifacts,
    record_scenario_outcome,
    store_scenario,
    store_stakeholder_artifacts,
)


def get_memory_tools() -> list[FunctionTool]:
    """Return all leadership memory tools as ADK FunctionTools."""
    return [
        FunctionTool(store_scenario),
        FunctionTool(record_scenario_outcome),
        FunctionTool(store_stakeholder_artifacts),
        FunctionTool(recall_stakeholder_artifacts),
        FunctionTool(recall_scenarios),
        FunctionTool(get_scenario),
        FunctionTool(get_best_scenario),
        FunctionTool(get_planned_scenarios_data),
        # Fast-path for Organizer UI requests: atomic fetch + build +
        # emit in one tool call. Collapses the previous 4-5 LLM turns
        # into 1, cutting Organizer response latency from ~42s to ~5s.
        # The classifier in prompts.py routes all "list top N"
        # requests here.
        FunctionTool(emit_scenario_list_a2ui),
        # Fast-path for the Organizer "Show Scenario" button. Same
        # atomic pattern as the list tool: fetches one scenario,
        # builds the detail A2UI card, emits surfaceUpdate +
        # beginRendering, done. Classifier in prompts.py routes any
        # message matching the frontend's "Get the route for the
        # seed <UUID>" broadcast text here.
        FunctionTool(emit_scenario_detail_a2ui),

        # RAG over uploaded documents — vector search over chunked
        # document text. Auto-called at Step 3b so the planner has
        # document context when designing scenarios.
        FunctionTool(retrieve_document_passages),
        # User-selected past simulations — reads scenario IDs from
        # session state (set by Organizer sidebar checkboxes).
        FunctionTool(get_selected_scenarios),
    ]
