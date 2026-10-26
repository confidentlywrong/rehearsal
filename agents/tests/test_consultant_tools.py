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

"""Integration tests for the consultant skill tool pipeline.

Verifies that emit_sim_results_a2ui and emit_action_plan_a2ui are:
1. Registered in the SkillToolset additional_tools pool
2. Callable with mocked Redis data and produce correct A2UI payloads
3. Correctly read persona_status_counts from Redis side-channel
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestConsultantToolRegistration:
    """Verify consultant tools are properly wired into SkillToolset."""

    def test_consultant_tools_in_skill_toolset(self):
        """emit_sim_results_a2ui and emit_action_plan_a2ui must be in SkillToolset additional_tools."""
        from google.adk.tools.skill_toolset import SkillToolset
        from agents.leadership_planner_with_eval.adk_tools import get_tools

        tools = get_tools()

        # Find the SkillToolset
        skill_toolsets = [t for t in tools if isinstance(t, SkillToolset)]
        assert len(skill_toolsets) == 1, "Expected exactly one SkillToolset"
        st = skill_toolsets[0]

        # Both consultant tools must be registered in the additional_tools pool
        assert "emit_sim_results_a2ui" in st._provided_tools_by_name, (
            "emit_sim_results_a2ui missing from SkillToolset additional_tools. "
            "Check adk_tools.py consultant tool registration."
        )
        assert "emit_action_plan_a2ui" in st._provided_tools_by_name, (
            "emit_action_plan_a2ui missing from SkillToolset additional_tools. "
            "Check adk_tools.py consultant tool registration."
        )

    def test_consultant_skill_has_adk_additional_tools_metadata(self):
        """SKILL.md must declare adk_additional_tools in YAML frontmatter."""
        import pathlib
        import yaml

        skill_path = (
            pathlib.Path(__file__).parent.parent / "leadership_planner_with_eval" / "skills" / "consultant" / "SKILL.md"
        )
        assert skill_path.exists(), f"Consultant SKILL.md not found at {skill_path}"

        content = skill_path.read_text()
        # Extract YAML frontmatter between --- delimiters
        parts = content.split("---")
        assert len(parts) >= 3, "SKILL.md must have YAML frontmatter between --- delimiters"
        frontmatter = yaml.safe_load(parts[1])

        assert "metadata" in frontmatter, "SKILL.md frontmatter must contain 'metadata' key"
        metadata = frontmatter["metadata"]
        assert "adk_additional_tools" in metadata, "SKILL.md metadata must declare 'adk_additional_tools'"
        tools_list = metadata["adk_additional_tools"]
        assert "emit_sim_results_a2ui" in tools_list
        assert "emit_action_plan_a2ui" in tools_list

    def test_consultant_tools_not_top_level(self):
        """Consultant tools should NOT be top-level tools — they gate behind load_skill."""
        from agents.leadership_planner_with_eval.adk_tools import get_tools

        tools = get_tools()
        top_level_names = [t.name if hasattr(t, "name") else type(t).__name__ for t in tools]

        assert "emit_sim_results_a2ui" not in top_level_names, (
            "emit_sim_results_a2ui should not be a top-level tool; "
            "it should only activate after load_skill('consultant')"
        )
        assert "emit_action_plan_a2ui" not in top_level_names, (
            "emit_action_plan_a2ui should not be a top-level tool; "
            "it should only activate after load_skill('consultant')"
        )


class TestEmitSimResultsA2UI:
    """Verify emit_sim_results_a2ui produces correct A2UI payloads."""

    @pytest.fixture
    def mock_tool_context(self):
        """ToolContext mock with simulation state."""
        ctx = MagicMock()
        ctx.session.id = "test-session-123"
        ctx.state = {
            "simulation_id": "sim-test-001",
            "expected_stakeholder_count": 10_000,
            "simulation_config": {"runner_count": 10},
        }
        return ctx

    @pytest.mark.asyncio
    async def test_with_full_persona_data(self, mock_tool_context):
        """When Redis has persona data, percentages should render correctly."""
        persona_data = {
            "Visionary": {"aligned": 3, "running": 1, "exhausted": 1},
            "Diplomat": {"aligned": 2, "running": 2},
            "Pragmatist": {"aligned": 1, "running": 3, "collapsed": 1},
            "Disruptor": {"aligned": 0, "running": 1, "collapsed": 1},
        }

        with (
            patch(
                "agents.utils.simdata.load_simulation_data",
                new_callable=AsyncMock,
                return_value={
                    "persona_status_counts": persona_data,
                    "runner_count": 10,
                },
            ),
            patch(
                "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
                new_callable=AsyncMock,
            ) as mock_emit,
        ):
            from agents.leadership_planner_with_eval.skills.consultant.tools import (
                emit_sim_results_a2ui,
            )

            result_string = await emit_sim_results_a2ui(
                tool_context=mock_tool_context,
                scenario_name="Test Scenario",
                overall_score=75,
                stakeholder_alignment=80,
                decision_quality=70,
                crisis_readiness=65,
                coaching_kotter_title="Create Urgency",
                coaching_kotter_desc="Build coalition quickly.",
                coaching_adkar_title="Awareness",
                coaching_adkar_desc="Communicate the need.",
                coaching_synthesis="Focus on quick wins.",
            )

            assert "SUCCESS" in result_string

            # Verify persona ScoreBars in the A2UI components.
            # With numeric persona data we now emit `{prefix}-bar` ScoreBars
            # carrying a literalNumber percentage (instead of the legacy
            # `{prefix}-val` Text "X% Aligned" rendering).
            first_call_data = mock_emit.call_args_list[0].kwargs.get(
                "data", mock_emit.call_args_list[0][1].get("data", {})
            )
            components = first_call_data["surfaceUpdate"]["components"]
            comp_by_id = {c["id"]: c for c in components}

            def _bar_pct(prefix: str) -> int:
                return comp_by_id[f"{prefix}-bar"]["component"]["ScoreBar"]["value"]["literalNumber"]

            # Visionary: 3 aligned / 5 total = 60%
            assert _bar_pct("vis") == 60
            # Diplomat: 2 aligned / 4 total = 50%
            assert _bar_pct("dip") == 50
            # Pragmatist: 1 aligned / 5 total = 20%
            assert _bar_pct("prg") == 20
            # Disruptor: 0 aligned / 2 total = 0%
            assert _bar_pct("dis") == 0

            # Legacy `{prefix}-val` Text rows must NOT be present when we have
            # numeric data — they only exist on the fallback path.
            for prefix in ("vis", "dip", "prg", "dis"):
                assert f"{prefix}-val" not in comp_by_id, (
                    f"{prefix}-val should not be emitted when persona data is present"
                )

    @pytest.mark.asyncio
    async def test_with_empty_persona_data(self, mock_tool_context):
        """When Redis has no persona data, all values should show '—'."""
        with (
            patch(
                "agents.utils.simdata.load_simulation_data",
                new_callable=AsyncMock,
                return_value={
                    "persona_status_counts": None,
                    "runner_count": None,
                },
            ),
            patch(
                "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
                new_callable=AsyncMock,
            ) as mock_emit,
        ):
            from agents.leadership_planner_with_eval.skills.consultant.tools import (
                emit_sim_results_a2ui,
            )

            result_string = await emit_sim_results_a2ui(
                tool_context=mock_tool_context,
                scenario_name="Empty Test",
                overall_score=50,
                stakeholder_alignment=50,
                decision_quality=50,
                crisis_readiness=50,
                coaching_kotter_title="Title",
                coaching_kotter_desc="Desc",
                coaching_adkar_title="Title",
                coaching_adkar_desc="Desc",
                coaching_synthesis="Synthesis",
            )

            assert "SUCCESS" in result_string
            first_call_data = mock_emit.call_args_list[0].kwargs.get(
                "data", mock_emit.call_args_list[0][1].get("data", {})
            )
            components = first_call_data["surfaceUpdate"]["components"]
            comp_by_id = {c["id"]: c for c in components}

            for persona_id in ["vis-val", "dip-val", "prg-val", "dis-val"]:
                val = comp_by_id[persona_id]["component"]["Text"]["text"]["literalString"]
                assert val == "—", f"Expected '—' for {persona_id} with no data, got '{val}'"

    @pytest.mark.asyncio
    async def test_transformation_status_from_simulator_side_channel(self, mock_tool_context):
        """Transformation Status KPIs render from the simulator's real data shape.

        The simulator stores resources as pools keyed lau/bt/tbu/owb, each
        {"current", "initial", ...}. Leadership capital is the LAU pool.
        """
        sim_data = {
            "persona_status_counts": {
                "diplomat": {"running": 3},
                "disruptor": {"collapsed": 2, "exhausted": 1},
                "visionary": {"exhausted": 2, "collapsed": 1},
                "pragmatist": {"running": 2, "finished": 1},
            },
            "runner_count": 12,
            "vitals_trend": [{"tick": i} for i in range(12)],
            "alert_history": [{"tick": 3}],
            "triggered_black_swans": ["competitor launch"],
            "resources": {
                "lau": {"current": 64.0, "initial": 100.0, "regen_per_tick": 5.0, "critical_threshold": 20.0},
                "bt": {"current": 500.0, "initial": 500.0},
            },
        }
        with (
            patch(
                "agents.utils.simdata.load_simulation_data",
                new_callable=AsyncMock,
                return_value=sim_data,
            ),
            patch(
                "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
                new_callable=AsyncMock,
            ) as mock_emit,
        ):
            from agents.leadership_planner_with_eval.skills.consultant.tools import (
                emit_sim_results_a2ui,
            )

            result_string = await emit_sim_results_a2ui(
                tool_context=mock_tool_context,
                scenario_name="Status Test",
                overall_score=70,
                stakeholder_alignment=70,
                decision_quality=70,
                crisis_readiness=70,
                coaching_kotter_title="T",
                coaching_kotter_desc="D",
                coaching_adkar_title="T",
                coaching_adkar_desc="D",
                coaching_synthesis="S",
            )

            assert "SUCCESS" in result_string
            comp_by_id = {}
            for call in mock_emit.call_args_list:
                data = call.kwargs.get("data") or {}
                for c in (data.get("surfaceUpdate") or {}).get("components", []):
                    comp_by_id[c["id"]] = c

            def _text(cid: str) -> str:
                return comp_by_id[cid]["component"]["Text"]["text"]["literalString"]

            assert _text("status-capital-val") == "64 / 100 LAU"
            assert _text("status-ticks-val") == "12"
            assert _text("status-bs-val") == "1"
            assert _text("status-alert-val") == "1"
            # 1 finished out of 12 stakeholders
            assert _text("status-align-val") == "8%"

    @pytest.mark.asyncio
    async def test_zero_progress_renders_as_scorebar_not_dropped(self, mock_tool_context):
        """Regression: persona with measured 0.0% progress must render as a 0%
        ScoreBar, not be silently dropped to the "no data" fallback.

        Previously, `norm_progress` coerced None → 0.0 and the condition
        `progress_pct > 0` then treated measured-zero identically to no-data,
        causing the most diagnostically important persona (the one that
        totally failed) to disappear from the dashboard.
        """
        with (
            patch(
                "agents.utils.simdata.load_simulation_data",
                new_callable=AsyncMock,
                return_value={
                    "persona_status_counts": None,
                    "runner_count": 10,
                    # All four personas have a measured average progress; the
                    # Disruptor measured exactly 0.0 (the bug-triggering value).
                    "persona_avg_progress": {
                        "Visionary": 75.0,
                        "Diplomat": 40.0,
                        "Pragmatist": 10.0,
                        "Disruptor": 0.0,
                    },
                },
            ),
            patch(
                "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
                new_callable=AsyncMock,
            ) as mock_emit,
        ):
            from agents.leadership_planner_with_eval.skills.consultant.tools import (
                emit_sim_results_a2ui,
            )

            await emit_sim_results_a2ui(
                tool_context=mock_tool_context,
                scenario_name="Zero Progress Test",
                overall_score=20,
                stakeholder_alignment=30,
                decision_quality=20,
                crisis_readiness=10,
                coaching_kotter_title="K",
                coaching_kotter_desc="K",
                coaching_adkar_title="A",
                coaching_adkar_desc="A",
                coaching_synthesis="S",
            )

            first_call_data = mock_emit.call_args_list[0].kwargs.get(
                "data", mock_emit.call_args_list[0][1].get("data", {})
            )
            comp_by_id = {c["id"]: c for c in first_call_data["surfaceUpdate"]["components"]}

            # Disruptor must be present as a ScoreBar with literalNumber 0,
            # NOT as a `dis-val` Text "—" or omitted entirely.
            assert "dis-bar" in comp_by_id, (
                "Persona with 0.0 progress must emit a ScoreBar (`dis-bar`), not fall through to the no-data branch."
            )
            assert "dis-val" not in comp_by_id, (
                "0.0 is measured data; must not render as the `dis-val` Text '—' fallback."
            )
            assert comp_by_id["dis-bar"]["component"]["ScoreBar"]["value"]["literalNumber"] == 0

    @pytest.mark.asyncio
    async def test_missing_simulation_id(self):
        """When simulation_id is missing, should not crash."""
        ctx = MagicMock()
        ctx.session.id = "test-session"
        ctx.state = {}  # No simulation_id

        with (
            patch(
                "agents.utils.simdata.load_simulation_data",
                new_callable=AsyncMock,
            ) as mock_load,
            patch(
                "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
                new_callable=AsyncMock,
            ),
        ):
            from agents.leadership_planner_with_eval.skills.consultant.tools import (
                emit_sim_results_a2ui,
            )

            result_string = await emit_sim_results_a2ui(
                tool_context=ctx,
                scenario_name="No ID Test",
                overall_score=0,
                stakeholder_alignment=0,
                decision_quality=0,
                crisis_readiness=0,
                coaching_kotter_title="N/A",
                coaching_kotter_desc="N/A",
                coaching_adkar_title="N/A",
                coaching_adkar_desc="N/A",
                coaching_synthesis="N/A",
            )

            assert "SUCCESS" in result_string
            # load_simulation_data should NOT be called with missing ID
            mock_load.assert_not_called()


class TestEmitActionPlanA2UI:
    """Verify emit_action_plan_a2ui produces correct A2UI payloads."""

    @pytest.mark.asyncio
    async def test_action_plan_renders_markdown(self):
        """Action plan should parse markdown headers into h2 components."""
        ctx = MagicMock()
        ctx.session.id = "test-session"
        ctx.state = {"simulation_id": "sim-001"}

        plan_md = """# Days 1-30: Immediate Actions
Build stakeholder coalition.
Secure executive sponsorship.

# Days 31-60: Capability Building
Train change champions.
Implement feedback loops.

# Days 61-90: Sustainability
Embed governance framework.
Measure transformation KPIs."""

        with patch(
            "agents.leadership_planner_with_eval.skills.consultant.tools.emit_gateway_message",
            new_callable=AsyncMock,
        ) as mock_emit:
            from agents.leadership_planner_with_eval.skills.consultant.tools import (
                emit_action_plan_a2ui,
            )

            result_string = await emit_action_plan_a2ui(
                tool_context=ctx,
                plan_markdown=plan_md,
            )

            assert "SUCCESS" in result_string

            # Should have called emit_gateway_message twice
            # (surfaceUpdate + beginRendering)
            assert mock_emit.call_count == 2

            # First call should be surfaceUpdate
            first_call_data = mock_emit.call_args_list[0].kwargs.get(
                "data", mock_emit.call_args_list[0][1].get("data", {})
            )
            assert "surfaceUpdate" in first_call_data

            # Second call should be beginRendering
            second_call_data = mock_emit.call_args_list[1].kwargs.get(
                "data", mock_emit.call_args_list[1][1].get("data", {})
            )
            assert "beginRendering" in second_call_data


class TestPromptCompleteness:
    """Verify execution prompts include all required tool calls."""

    def test_eval_planner_execution_mentions_action_plan(self):
        """EVAL_EXECUTION must instruct LLM to call emit_action_plan_a2ui."""
        from agents.leadership_planner_with_eval.prompts import EVAL_EXECUTION

        assert "emit_action_plan_a2ui" in EVAL_EXECUTION, (
            "EVAL_EXECUTION prompt must mention emit_action_plan_a2ui "
            "to ensure the LLM calls it during simulation execution."
        )
        assert "90-Day Action Plan" in EVAL_EXECUTION, (
            "EVAL_EXECUTION prompt must mention '90-Day Action Plan' to instruct the LLM what content to generate."
        )

    def test_memory_planner_execution_mentions_action_plan(self):
        """Memory planner execution section must instruct LLM to call emit_action_plan_a2ui."""
        from agents.leadership_planner_with_memory.prompts import MEMORY_EXECUTION

        assert "emit_action_plan_a2ui" in MEMORY_EXECUTION, (
            "MEMORY_EXECUTION prompt must mention "
            "emit_action_plan_a2ui to ensure the LLM calls it during execution."
        )
        assert "90-Day Action Plan" in MEMORY_EXECUTION, (
            "MEMORY_EXECUTION prompt must mention '90-Day Action Plan' to instruct the LLM what content to generate."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
