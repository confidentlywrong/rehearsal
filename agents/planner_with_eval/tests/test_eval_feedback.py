# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

"""Tests for the evaluate_plan FunctionTool and LLM feedback generation."""

import json
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from agents.planner_with_eval.evaluator.config import CRITERION_WEIGHTS, EVALUATION_CRITERIA, PASS_THRESHOLD


def test_pass_threshold_in_config():
    """PASS_THRESHOLD must be defined in config.py and set to 75."""
    assert PASS_THRESHOLD == 75


def test_evaluation_criteria_in_config():
    """EVALUATION_CRITERIA must list all 7 criteria in order."""
    assert EVALUATION_CRITERIA == [
        "safety_compliance",
        "logistics_completeness",
        "participant_experience",
        "community_impact",
        "financial_viability",
        "intent_alignment",
        "distance_compliance",
    ]


def test_criterion_weights_sum_to_one():
    """Weights must sum to 1.0."""
    assert abs(sum(CRITERION_WEIGHTS.values()) - 1.0) < 1e-09


def test_criterion_weights_has_seven_entries():
    """Expanded model has exactly 7 equally-weighted criteria."""
    expected_keys = {
        "safety_compliance",
        "logistics_completeness",
        "participant_experience",
        "community_impact",
        "financial_viability",
        "intent_alignment",
        "distance_compliance",
    }
    assert set(CRITERION_WEIGHTS.keys()) == expected_keys


def test_criterion_weights_are_equal():
    """All 7 criteria must have equal weight (1/7 each)."""
    expected_weight = 1.0 / 7
    for criterion, weight in CRITERION_WEIGHTS.items():
        assert abs(weight - expected_weight) < 1e-09, f"{criterion} weight {weight} != expected {expected_weight}"


@pytest.mark.asyncio
async def test_generate_feedback_returns_suggestions_and_summary():
    """_generate_feedback makes one GenerateContent call and returns suggestions + summary."""
    from agents.planner_with_eval.evaluator.tools import _generate_feedback

    mock_response = MagicMock()
    mock_response.text = '{"suggestions": ["Add emergency crossings"], "summary": "Plan needs work"}'
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response
    with patch("agents.planner_with_eval.evaluator.tools.genai") as mock_genai:
        mock_genai.Client.return_value = mock_client
        (suggestions, summary) = await _generate_feedback(
            scores={"safety_compliance": 60, "logistics_completeness": 80, "distance_compliance": 100},
            details={"safety_compliance": "Safety gaps"},
            user_intent="Plan a marathon in NYC",
            proposed_plan="A 26.2 mile route",
        )
    assert isinstance(suggestions, list)
    assert len(suggestions) > 0
    assert isinstance(summary, str)
    assert len(summary) > 0
    mock_client.models.generate_content.assert_called_once()


@pytest.mark.asyncio
async def test_evaluate_plan_includes_llm_suggestions():
    """evaluate_plan result must include LLM-generated suggestions and summary."""
    from agents.planner_with_eval.evaluator.tools import evaluate_plan

    fake_scores = {"safety_compliance": 80}
    fake_details = {"safety_compliance": "stub"}

    with patch(
        "agents.planner_with_eval.evaluator.tools._run_custom_eval",
        new=AsyncMock(return_value=(fake_scores, fake_details)),
    ):
        with patch(
            "agents.planner_with_eval.evaluator.tools._generate_feedback",
            new=AsyncMock(return_value=(["LLM suggestion"], "LLM summary")),
        ):
            with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "test-project"}, clear=True):
                result = await evaluate_plan(
                    json.dumps(
                        {
                            "user_intent": "Plan a marathon in NYC",
                            "proposed_plan": "A 26.2 mile route through NYC",
                        }
                    ),
                )

    assert result["improvement_suggestions"] == ["LLM suggestion"]
    assert result["summary"] == "LLM summary"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_realistic_plan_passes_vertex_ai_evaluation():
    """A realistic plan must pass Vertex AI Eval API when credentials are available.

    This test is skipped when GOOGLE_CLOUD_PROJECT is not set. When run,
    it proves the LLM judge scoring and planner output are in sync.
    """
    import os

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        pytest.skip("GOOGLE_CLOUD_PROJECT not set -- skipping live API test")
    from agents.planner_with_eval.evaluator.tools import evaluate_plan

    realistic_plan = "Las Vegas Marathon Plan\n\nA 26.2 mile marathon route through Las Vegas starting at the Las Vegas Sign, sweeping through the Arts District and residential neighborhoods, finishing near Michelob Ultra Arena.\n\nSafety: Emergency vehicle crossings at miles 5, 10, 15, 20. Emergency corridor access to University Medical Center. Evacuation routes every 2 miles. Medical tents at halfway and finish.\n\nCommunity: 30-day resident notification. Business access corridors. Equitable routing through diverse neighborhoods. Cheer zones at miles 6, 13, 20.\n\nScale: 50,000 participants, 5 waves starting at 6 AM. Budget: $2.5M with $4M projected revenue."
    result = await evaluate_plan(
        json.dumps(
            {
                "user_intent": "Plan a scenic marathon in Las Vegas for 50000 participants",
                "proposed_plan": realistic_plan,
            }
        )
    )
    assert result["eval_method"] in ("vertex_ai_eval", "heuristic"), f"Unexpected eval method: {result['eval_method']}"
    assert result["passed"] is True, (
        f"Realistic plan should pass evaluation (method={result['eval_method']}). Scores: {result['scores']}, Overall: {result['overall_score']}, Findings: {result['findings']}"
    )


@pytest.mark.asyncio
async def test_evaluate_plan_invalid_json():
    """Invalid JSON input returns error with parse_error criterion."""
    from agents.planner_with_eval.evaluator.tools import evaluate_plan

    result = await evaluate_plan("not valid json")
    assert result["eval_method"] == "error"
    assert result["findings"][0]["criterion"] == "parse_error"
    assert result["overall_score"] == 0


def test_planner_tools_contain_evaluate_plan_not_evaluator_agent():
    """evaluate_plan must be a direct FunctionTool, not an AgentTool."""
    from agents.planner_with_eval.adk_tools import get_tools
    from google.adk.tools.function_tool import FunctionTool

    tools = get_tools()
    tool_names = [t.name if hasattr(t, "name") else type(t).__name__ for t in tools]
    assert "evaluate_plan" in tool_names, "evaluate_plan must be in tools"
    assert "evaluator_agent" not in tool_names, "evaluator_agent must NOT be in tools"
    eval_tool = [t for t in tools if getattr(t, "name", None) == "evaluate_plan"][0]
    assert isinstance(eval_tool, FunctionTool)


def test_evaluator_package_exports_evaluate_plan():
    """evaluator package must export evaluate_plan, not evaluator_agent."""
    from agents.planner_with_eval.evaluator import evaluate_plan as fn

    assert callable(fn)
    assert fn.__name__ == "evaluate_plan"


def test_rubric_score_5_is_achievable():
    """Score 5 rubric should say 'comprehensive' not 'excellent with strong provisions'."""
    from agents.planner_with_eval.evaluator.tools import _create_combined_llm_metric

    metric = _create_combined_llm_metric()
    prompt = str(metric.prompt_template)
    assert "strong provisions" not in prompt
    assert "comprehensive" in prompt.lower() or "well-addressed" in prompt.lower()


def test_combined_metric_covers_all_criteria():
    """Combined LLM metric prompt must mention all 6 criterion names."""
    from agents.planner_with_eval.evaluator.tools import _create_combined_llm_metric, _LLM_CRITERION_SPECS

    metric = _create_combined_llm_metric()
    assert metric.name == "combined_criteria"
    prompt = metric.prompt_template
    for criterion_name in _LLM_CRITERION_SPECS:
        assert criterion_name in prompt, f"Criterion '{criterion_name}' missing from combined prompt"


class TestScoreNormalization:
    """Score normalization must handle both 0-1 and 1-5 ranges, outputting 0-100."""

    def test_score_on_1_to_5_scale_is_normalized(self):
        """A score of 4.0 (on 1-5 scale) should normalize to 80."""
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        assert _normalize_score(4.0) == 80

    def test_score_on_0_to_1_scale_is_normalized(self):
        """A score of 0.8 (on 0-1 scale) should normalize to 80."""
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        assert _normalize_score(0.8) == 80

    def test_score_zero_stays_zero(self):
        """A score of 0.0 should normalize to 0."""
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        assert _normalize_score(0.0) == 0

    def test_score_one_normalizes_to_100(self):
        """A score of 1.0 (could be either scale) should normalize to 100.

        Note: 1.0 is ambiguous -- it could be 1/5 (worst on 1-5 scale) or
        1.0 (perfect on 0-1 scale). We treat it as already-normalized.
        See _normalize_score docstring for rationale.
        """
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        assert _normalize_score(1.0) == 100

    def test_score_five_normalizes_to_100(self):
        """A score of 5.0 should normalize to 100."""
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        assert _normalize_score(5.0) == 100

    def test_score_clamped_to_max_100(self):
        """Scores above 5.0 should be clamped to 100."""
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        assert _normalize_score(6.0) == 100

    def test_negative_score_clamped_to_zero(self):
        """Negative scores should be clamped to 0."""
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        assert _normalize_score(-1.0) == 0

    def test_returns_int(self):
        """_normalize_score must return an int, not a float."""
        from agents.planner_with_eval.evaluator.tools import _normalize_score

        result = _normalize_score(3.5)
        assert isinstance(result, int)
        assert result == 70
