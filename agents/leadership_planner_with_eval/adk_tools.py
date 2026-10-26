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

"""ADK tool loader for the leadership_planner_with_eval agent.

Mirrors agents/planner_with_eval/adk_tools.py — combines leadership planner
tools with the LLM-as-Judge evaluator.
"""

import importlib.util
import logging
import pathlib

from google.adk.skills import load_skill_from_dir
from google.adk.code_executors.unsafe_local_code_executor import UnsafeLocalCodeExecutor
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.adk.tools.skill_toolset import SkillToolset

from agents.leadership_planner.adk_tools import _load_additional_tools
from agents.leadership_planner_with_eval.evaluator.tools import evaluate_leadership_plan
from agents.leadership_planner_with_eval.tools import start_simulation, submit_plan_to_simulator

logger = logging.getLogger(__name__)


def _load_a2ui_tool(shared_skills_dir: pathlib.Path):
    """Load validate_and_emit_a2ui as a top-level callable.

    This tool is a cross-cutting utility used regardless of which skill is
    active, so it is registered as a direct FunctionTool (always available)
    rather than a SkillToolset additional_tool (gated behind load_skill).
    """
    a2ui_path = shared_skills_dir / "a2ui-rendering" / "tools.py"
    if a2ui_path.exists():
        spec = importlib.util.spec_from_file_location("a2ui_tools", a2ui_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return getattr(mod, "validate_and_emit_a2ui", None)
    return None


def get_tools() -> list:
    """Build the leadership-planner-with-eval tool list.

    Inherits leadership planner skills and adds evaluation capabilities.
    """
    # Load skills from the base leadership planner
    base_planner_dir = pathlib.Path(__file__).parent.parent / "leadership_planner"
    base_skills_dir = base_planner_dir / "skills"

    # Load evaluation skills from own skills/ directory (if any)
    local_skills_dir = pathlib.Path(__file__).parent / "skills"

    # Load shared skills from the agents/skills/ directory (e.g. a2ui-rendering)
    shared_skills_dir = pathlib.Path(__file__).parent.parent / "skills"

    skills = []
    for skills_dir in [base_skills_dir, local_skills_dir, shared_skills_dir]:
        if skills_dir.exists():
            skills.extend(
                load_skill_from_dir(d)
                for d in sorted(skills_dir.iterdir())
                if d.is_dir() and not d.name.startswith("_") and (d / "SKILL.md").exists()
            )

    additional_tools = _load_additional_tools(base_skills_dir)

    # Load consultant tools for on-demand activation via load_skill
    consultant_tools_path = local_skills_dir / "consultant" / "tools.py"
    if consultant_tools_path.exists():
        spec = importlib.util.spec_from_file_location("consultant_tools", consultant_tools_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for name in ["emit_sim_results_a2ui", "emit_action_plan_a2ui"]:
                func = getattr(mod, name, None)
                if func:
                    additional_tools.append(func)

    # Load translation-diagnostic tools for on-demand activation via load_skill
    td_tools_path = local_skills_dir / "translation-diagnostic" / "tools.py"
    if td_tools_path.exists():
        spec = importlib.util.spec_from_file_location("td_tools", td_tools_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            func = getattr(mod, "emit_translation_diagnostic", None)
            if func:
                additional_tools.append(func)
            func = getattr(mod, "emit_stakeholder_psychology", None)
            if func:
                additional_tools.append(func)

    skill_toolset = SkillToolset(
        skills=skills,
        code_executor=UnsafeLocalCodeExecutor(),
        additional_tools=additional_tools,
    )

    tools = [
        skill_toolset,
        PreloadMemoryTool(),
        FunctionTool(func=evaluate_leadership_plan),
    ]

    # Register simulator collaboration tools (exclusive to planner_with_eval)
    tools.append(FunctionTool(func=start_simulation))
    tools.append(FunctionTool(func=submit_plan_to_simulator))

    # Register A2UI validation tool (cross-cutting, always available)
    a2ui_func = _load_a2ui_tool(shared_skills_dir)
    if a2ui_func:
        tools.append(FunctionTool(func=a2ui_func))

    # Organizational Intelligence: Google Maps MCP tools for stakeholder
    # venue grounding and environmental pressure (optional).
    # Returns [] if GOOGLE_MAPS_API_KEY is not configured.
    from agents.planner.adk_tools import get_maps_tools

    tools.extend(get_maps_tools())

    return tools
