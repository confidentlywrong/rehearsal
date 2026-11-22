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

"""Unit tests for leadership_planner ADK tool definitions."""

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
from google.adk.tools.skill_toolset import SkillToolset

from agents.leadership_planner.adk_tools import get_tools


def test_leadership_planner_tools_registers_validate_and_emit_a2ui():
    """Base leadership planner must register validate_and_emit_a2ui as top-level tool."""
    tools = get_tools()
    tool_names = [t.name if hasattr(t, "name") else type(t).__name__ for t in tools]
    assert "validate_and_emit_a2ui" in tool_names, (
        f"validate_and_emit_a2ui missing from leadership planner tools: {tool_names}"
    )


def test_leadership_planner_skill_tools_configured():
    """SkillToolset in base leadership planner must be configured with code executor."""
    tools = get_tools()
    skill_toolsets = [t for t in tools if isinstance(t, SkillToolset)]
    assert len(skill_toolsets) == 1
    st = skill_toolsets[0]
    assert st._code_executor is not None
    assert "design_leadership_scenario" in st._provided_tools_by_name
    assert "map_stakeholders" in st._provided_tools_by_name


def test_leadership_planner_model_uses_canonical_default(monkeypatch):
    """leadership_planner.agent.MODEL must resolve DEFAULT_PLANNER_MODEL and skip blank env."""
    from agents.utils.models import DEFAULT_PLANNER_MODEL, get_model

    monkeypatch.setenv("PLANNER_MODEL", "   ")
    assert get_model("PLANNER_MODEL", DEFAULT_PLANNER_MODEL) == DEFAULT_PLANNER_MODEL


@pytest.mark.asyncio
async def test_report_leadership_scenario_does_not_leak_across_sessions():
    """report_leadership_scenario must not fall back to _last_scenario from another session."""
    tools_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "skills"
        / "scenario-design"
        / "scripts"
        / "tools.py"
    )
    spec = importlib.util.spec_from_file_location("sd_tools_isolation", tools_path)
    assert spec is not None and spec.loader is not None
    sd_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sd_tools)

    ctx_a = SimpleNamespace(state={})
    res_a = await sd_tools.design_leadership_scenario(tool_context=ctx_a)
    assert res_a["status"] == "success"
    assert res_a["stakeholder_count"] == 1000

    ctx_b = SimpleNamespace(state={})
    res_b = await sd_tools.report_leadership_scenario(tool_context=ctx_b)
    assert res_b["status"] == "error"
    assert "No leadership scenario found" in res_b["message"]
