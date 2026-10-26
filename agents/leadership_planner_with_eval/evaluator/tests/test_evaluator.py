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

"""Unit tests for the leadership scenario evaluator.

Covers _normalize_score, _heuristic_eval, _build_result, and
_deterministic_feedback — all deterministic paths that require
no LLM calls or GCP credentials.
"""

from agents.leadership_planner_with_eval.evaluator.config import (
    CRITERION_WEIGHTS,
    EVALUATION_CRITERIA,
    PASS_THRESHOLD,
    SEVERITY_THRESHOLDS,
)
from agents.leadership_planner_with_eval.evaluator.tools import (
    _build_result,
    _normalize_score,
)


# ============================================================================
# _normalize_score
# ============================================================================


class TestNormalizeScore:
    """Verify three-tier normalization: 0-1 → 0-100, 1-5 → 0-100, >5 passthrough."""

    # --- 0-1 scale (multiply by 100) ---
    def test_zero(self):
        assert _normalize_score(0.0) == 0

    def test_half_on_0_1_scale(self):
        assert _normalize_score(0.5) == 50

    def test_one_point_zero_treated_as_perfect(self):
        """1.0 is ambiguous (could be 1/5 or 1.0/1.0). We treat it as 100."""
        assert _normalize_score(1.0) == 100

    def test_small_fraction(self):
        assert _normalize_score(0.72) == 72

    # --- 1-5 scale (multiply by 20) ---
    def test_score_of_3_on_1_5_scale(self):
        assert _normalize_score(3.0) == 60

    def test_score_of_4_on_1_5_scale(self):
        assert _normalize_score(4.0) == 80

    def test_score_of_4_5_on_1_5_scale(self):
        assert _normalize_score(4.5) == 90

    def test_score_of_5_on_1_5_scale(self):
        assert _normalize_score(5.0) == 100

    def test_score_of_1_5_on_1_5_scale(self):
        assert _normalize_score(1.5) == 30

    # --- 0-100 scale (passthrough, already >5) ---
    def test_passthrough_50(self):
        assert _normalize_score(50) == 50

    def test_passthrough_85(self):
        assert _normalize_score(85) == 85

    def test_passthrough_100(self):
        assert _normalize_score(100) == 100

    # --- Edge cases and clamping ---
    def test_negative_clamps_to_zero(self):
        assert _normalize_score(-5.0) == 0

    def test_above_100_clamps_to_100(self):
        assert _normalize_score(150) == 100

    def test_rounding(self):
        """0.755 * 100 = 75.5 → rounds to 76."""
        assert _normalize_score(0.755) == 76


# ============================================================================
# _build_result
# ============================================================================


class TestBuildResult:
    """Verify weighted scoring, finding generation, and pass/fail logic."""

    @staticmethod
    def _uniform_scores(value: int) -> dict[str, int]:
        """Create a scores dict with the same value for all criteria."""
        return {c: value for c in EVALUATION_CRITERIA}

    @staticmethod
    def _uniform_details(msg: str = "test") -> dict[str, str]:
        return {c: msg for c in EVALUATION_CRITERIA}

    def test_overall_score_is_weighted_average(self):
        """With equal weights and uniform scores, overall == per-criterion score."""
        scores = self._uniform_scores(80)
        result = _build_result(scores, self._uniform_details(), eval_method="test")
        assert result["overall_score"] == 80

    def test_passed_when_above_threshold(self):
        """Should pass when overall >= PASS_THRESHOLD and no MAJOR findings."""
        scores = self._uniform_scores(85)
        result = _build_result(scores, self._uniform_details(), eval_method="test")
        assert result["passed"] is True

    def test_failed_when_below_threshold(self):
        """Should fail when overall < PASS_THRESHOLD."""
        scores = self._uniform_scores(50)
        result = _build_result(scores, self._uniform_details(), eval_method="test")
        assert result["passed"] is False

    def test_failed_when_major_finding(self):
        """Should fail even with high overall if a MAJOR finding exists."""
        scores = self._uniform_scores(90)
        major_finding = [{"title": "Critical issue", "severity": "MAJOR", "description": "test"}]
        result = _build_result(
            scores,
            self._uniform_details(),
            eval_method="test",
            findings=major_finding,
        )
        assert result["passed"] is False

    def test_auto_generates_findings_for_low_scores(self):
        """When findings=None, auto-generates findings for scores < 80."""
        scores = self._uniform_scores(90)
        scores["crisis_readiness"] = 35  # Below high threshold
        scores["decision_quality"] = 55  # Below medium threshold
        result = _build_result(scores, self._uniform_details(), eval_method="test")
        # Should have at least 2 findings
        assert len(result["findings"]) >= 2
        severities = [f["severity"] for f in result["findings"]]
        assert "MAJOR" in severities  # 35 < 40 → MAJOR
        assert "MODERATE" in severities  # 55 < 60 → MODERATE

    def test_result_structure(self):
        """Result dict must contain all expected keys."""
        scores = self._uniform_scores(75)
        result = _build_result(scores, self._uniform_details(), eval_method="test")
        for key in [
            "passed",
            "scores",
            "findings",
            "improvement_suggestions",
            "overall_score",
            "summary",
            "eval_method",
        ]:
            assert key in result, f"Missing key: {key}"

    def test_eval_method_preserved(self):
        """The eval_method string should pass through unchanged."""
        result = _build_result(
            self._uniform_scores(80),
            self._uniform_details(),
            eval_method="heuristic",
        )
        assert result["eval_method"] == "heuristic"

    def test_mixed_scores_produce_weighted_average(self):
        """Non-uniform scores should produce a proper weighted average."""
        scores = {
            "stakeholder_alignment": 100,
            "decision_quality": 60,
            "resource_stewardship": 80,
            "crisis_readiness": 40,
            "strategic_coherence": 90,
            "ethical_alignment": 70,
        }
        result = _build_result(scores, self._uniform_details(), eval_method="test")
        # Equal weights: (100+60+80+40+90+70) / 6 = 440/6 = 73.33 → 73
        expected = round(sum(scores.values()) / len(scores))
        assert result["overall_score"] == expected


# ============================================================================
# Config consistency checks
# ============================================================================


class TestEvaluatorConfig:
    """Verify evaluator config integrity."""

    def test_weights_sum_to_one(self):
        """Criterion weights must sum to 1.0 (±epsilon)."""
        total = sum(CRITERION_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9, f"Weights sum to {total}, expected 1.0"

    def test_all_criteria_have_weights(self):
        """Every criterion in EVALUATION_CRITERIA must have a weight."""
        for c in EVALUATION_CRITERIA:
            assert c in CRITERION_WEIGHTS, f"Missing weight for: {c}"

    def test_pass_threshold_reasonable(self):
        """Pass threshold should be between 50 and 95."""
        assert 50 <= PASS_THRESHOLD <= 95

    def test_severity_thresholds_ordered(self):
        """Severity thresholds: high < medium < low."""
        assert SEVERITY_THRESHOLDS["high"] < SEVERITY_THRESHOLDS["medium"]
        assert SEVERITY_THRESHOLDS["medium"] < SEVERITY_THRESHOLDS["low"]


# ============================================================================
# Empty-LLM-response handling (2026-07-02 fix)
# ============================================================================
#
# The evaluator originally crashed with ``'NoneType' object has no attribute
# 'strip'`` whenever ``response.text`` was None (safety block, MAX_TOKENS,
# missing candidates, etc.). These tests exercise:
#
# * ``_extract_response_reason`` — the diagnostic helper that classifies
#   why text was empty. Kept as a pure helper so we can unit-test it with
#   ``SimpleNamespace`` stubs instead of a full ``GenerateContentResponse``.
# * ``_empty_response_result`` — the graceful-return dict-builder.
# * ``evaluate_leadership_plan`` end-to-end via a mocked client — asserts
#   the tool returns a well-formed result (``eval_method="empty_llm_response"``)
#   instead of raising, for every empty-text scenario the SDK can produce.
# ============================================================================


from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

from agents.leadership_planner_with_eval.evaluator.tools import (  # noqa: E402
    _empty_response_result,
    _extract_response_reason,
    evaluate_leadership_plan,
)


class TestExtractResponseReason:
    def test_prompt_blocked_takes_precedence(self):
        resp = SimpleNamespace(
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
            candidates=[SimpleNamespace(finish_reason="STOP")],
        )
        assert _extract_response_reason(resp) == "prompt_blocked:SAFETY"

    def test_no_candidates(self):
        resp = SimpleNamespace(prompt_feedback=None, candidates=[])
        assert _extract_response_reason(resp) == "no_candidates"

    def test_finish_reason_enum_with_name(self):
        # Real SDK returns an enum-like object with a .name attr; we accept both.
        finish = SimpleNamespace(name="MAX_TOKENS")
        resp = SimpleNamespace(
            prompt_feedback=None,
            candidates=[SimpleNamespace(finish_reason=finish)],
        )
        assert _extract_response_reason(resp) == "finish_reason:MAX_TOKENS"

    def test_finish_reason_bare_string(self):
        resp = SimpleNamespace(
            prompt_feedback=None,
            candidates=[SimpleNamespace(finish_reason="SAFETY")],
        )
        assert _extract_response_reason(resp) == "finish_reason:SAFETY"

    def test_all_missing_returns_unknown_but_does_not_raise(self):
        # Even a broken response object must not crash the diagnostic path.
        class _Explodes:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        assert _extract_response_reason(_Explodes()) in {"unknown", "no_candidates"}


class TestEmptyResponseResult:
    def test_shape_matches_regular_evaluation_result(self):
        result = _empty_response_result(reason="finish_reason:SAFETY", input_hash="abc123def456")
        # Every downstream tool assumes these keys exist.
        for key in ("passed", "scores", "findings", "improvement_suggestions", "overall_score", "summary", "eval_method"):
            assert key in result, f"missing key: {key}"
        assert result["passed"] is False
        assert result["overall_score"] == 0
        assert result["eval_method"] == "empty_llm_response"
        assert "SAFETY" in result["summary"]
        assert "abc123def456" in result["summary"]

    def test_findings_flag_a_major_severity(self):
        result = _empty_response_result(reason="no_candidates", input_hash="deadbeef")
        assert len(result["findings"]) == 1
        assert result["findings"][0]["severity"] == "MAJOR"

    def test_scores_have_all_criteria_at_zero(self):
        result = _empty_response_result(reason="finish_reason:MAX_TOKENS", input_hash="0000")
        for c in EVALUATION_CRITERIA:
            assert result["scores"][c] == 0


@pytest.mark.asyncio
class TestEvaluatorEmptyResponse:
    """End-to-end: evaluate_leadership_plan must NOT raise on empty LLM text.

    Uses ``patch("...._get_client")`` to inject a mock client whose
    ``aio.models.generate_content`` returns a response with ``text=None``,
    reproducing the exact failure mode captured by the 2026-07-02
    production tool_error ("'NoneType' object has no attribute 'strip'").
    """

    def _mock_client(self, response):
        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=AsyncMock(return_value=response),
                )
            )
        )
        return client

    async def test_none_text_from_safety_block_returns_graceful_result(self):
        response = SimpleNamespace(
            text=None,
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
            candidates=[SimpleNamespace(finish_reason="SAFETY")],
        )
        with patch(
            "agents.leadership_planner_with_eval.evaluator.tools._get_client",
            return_value=self._mock_client(response),
        ):
            result = await evaluate_leadership_plan(evaluation_request="{}")
        assert result["eval_method"] == "empty_llm_response"
        assert result["passed"] is False
        assert result["overall_score"] == 0
        assert "SAFETY" in result["summary"]

    async def test_none_text_from_max_tokens_returns_graceful_result(self):
        response = SimpleNamespace(
            text=None,
            prompt_feedback=None,
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="MAX_TOKENS"))],
        )
        with patch(
            "agents.leadership_planner_with_eval.evaluator.tools._get_client",
            return_value=self._mock_client(response),
        ):
            result = await evaluate_leadership_plan(evaluation_request="{}")
        assert result["eval_method"] == "empty_llm_response"
        assert "MAX_TOKENS" in result["summary"]

    async def test_no_candidates_returns_graceful_result(self):
        response = SimpleNamespace(text=None, prompt_feedback=None, candidates=[])
        with patch(
            "agents.leadership_planner_with_eval.evaluator.tools._get_client",
            return_value=self._mock_client(response),
        ):
            result = await evaluate_leadership_plan(evaluation_request="{}")
        assert result["eval_method"] == "empty_llm_response"
        assert "no_candidates" in result["summary"]

    async def test_whitespace_only_text_treated_as_empty(self):
        # ``response.text`` is a string of just whitespace — still no scorable output.
        response = SimpleNamespace(
            text="   \n\n\t  ",
            prompt_feedback=None,
            candidates=[SimpleNamespace(finish_reason="STOP")],
        )
        with patch(
            "agents.leadership_planner_with_eval.evaluator.tools._get_client",
            return_value=self._mock_client(response),
        ):
            result = await evaluate_leadership_plan(evaluation_request="{}")
        assert result["eval_method"] == "empty_llm_response"

    async def test_result_is_written_to_tool_context_state(self):
        # Downstream tools (``store_scenario``) read
        # ``state["leadership_evaluation"]`` and ``state["evaluation_result"]``.
        # The empty-response path must populate both, exactly like the
        # success path, so a failed evaluation does not silently vanish.
        response = SimpleNamespace(text=None, prompt_feedback=None, candidates=[])
        ctx = SimpleNamespace(state={})
        with patch(
            "agents.leadership_planner_with_eval.evaluator.tools._get_client",
            return_value=self._mock_client(response),
        ):
            result = await evaluate_leadership_plan(evaluation_request="{}", tool_context=ctx)
        assert ctx.state["leadership_evaluation"] is result
        assert ctx.state["evaluation_result"] is result

    async def test_normal_json_success_path_still_works(self):
        """Regression guard: valid LLM text still yields a real llm_judge result."""
        happy_payload = {
            "scores": {c: 80 for c in EVALUATION_CRITERIA},
            "findings": [{"title": "OK", "severity": "MINOR", "description": "ok"}],
            "summary": "All good.",
        }
        import json as _json

        response = SimpleNamespace(
            text=_json.dumps(happy_payload),
            prompt_feedback=None,
            candidates=[SimpleNamespace(finish_reason="STOP")],
        )
        with patch(
            "agents.leadership_planner_with_eval.evaluator.tools._get_client",
            return_value=self._mock_client(response),
        ):
            result = await evaluate_leadership_plan(evaluation_request="{}")
        assert result["eval_method"] == "llm_judge"
        assert result["overall_score"] == 80
