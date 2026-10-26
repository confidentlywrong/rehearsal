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
    """Return all tools for the leadership_planner_with_memory agent."""
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
