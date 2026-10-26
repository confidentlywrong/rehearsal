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

"""ADK tool loader for the leadership_planner agent.

Mirrors agents/planner/adk_tools.py but loads leadership-domain skills
(scenario-design, stakeholder-mapping) instead of marathon skills.
Optionally registers Google Maps MCP tools for real-world venue grounding.
"""

import importlib.util
import logging
import pathlib

from google.adk.skills import load_skill_from_dir
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.adk.tools.skill_toolset import SkillToolset

logger = logging.getLogger(__name__)


def _load_additional_tools(skills_dir: pathlib.Path) -> list:
    """Load skill tool functions as callables for SkillToolset additional_tools.

    These tools become available to the LLM only after load_skill activates
    the owning skill. ADK wraps them as FunctionTools automatically and
    injects tool_context at call time.
    """
    tools = []

    # Scenario design tools
    scenario_path = skills_dir / "scenario-design" / "scripts" / "tools.py"
    if scenario_path.exists():
        spec = importlib.util.spec_from_file_location("scenario_tools", scenario_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for name in [
                "design_leadership_scenario",
                "report_leadership_scenario",
                "plan_consensus_pathway",
            ]:
                func = getattr(module, name, None)
                if func:
                    tools.append(func)

    # Stakeholder mapping tools
    mapping_path = skills_dir / "stakeholder-mapping" / "scripts" / "tools.py"
    if mapping_path.exists():
        spec = importlib.util.spec_from_file_location("mapping_tools", mapping_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for name in ["map_stakeholders"]:
                func = getattr(module, name, None)
                if func:
                    tools.append(func)

    return tools


def get_tools() -> list:
    """Build the leadership planner's tool list with lazy-loaded skills.

    Uses SkillToolset for load_skill/run_skill_script support.
    Skill tools are passed as additional_tools to SkillToolset
    so they become available only after load_skill activates the owning skill.
    """
    from google.adk.code_executors.unsafe_local_code_executor import UnsafeLocalCodeExecutor

    skills_dir = pathlib.Path(__file__).parent / "skills"

    skills = []
    if skills_dir.exists():
        skills = [
            load_skill_from_dir(d)
            for d in sorted(skills_dir.iterdir())
            if d.is_dir() and not d.name.startswith("_") and (d / "SKILL.md").exists()
        ]

    additional_tools = _load_additional_tools(skills_dir)

    skill_toolset = SkillToolset(
        skills=skills,
        code_executor=UnsafeLocalCodeExecutor(),
        additional_tools=additional_tools,
    )

    tools = [
        skill_toolset,
        PreloadMemoryTool(),
    ]

    # Google Maps MCP tools for real-world venue grounding (optional).
    # Returns [] if GOOGLE_MAPS_API_KEY is not configured.
    from agents.planner.adk_tools import get_maps_tools

    tools.extend(get_maps_tools())

    return tools
