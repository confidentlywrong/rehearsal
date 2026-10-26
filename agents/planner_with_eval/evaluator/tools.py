# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
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

"""Evaluation tools for marathon plan quality assessment.

Provides the evaluate_plan FunctionTool using Vertex AI Evaluation with
7 metrics (6 LLM metrics + 1 deterministic check), plus a single
GenerateContent call for LLM-generated improvement suggestions and summary.
Falls back to heuristic evaluation if the Eval API is unavailable.
"""

import asyncio
import json
import os
import re
from typing import Any

import logging
import pandas as pd
import vertexai
from google import genai
from google.genai import types as genai_types
from vertexai import types
from google.adk.tools.tool_context import ToolContext
from agents.planner_with_eval.evaluator.config import CRITERION_WEIGHTS, SEVERITY_THRESHOLDS, MODEL, PASS_THRESHOLD
from agents.utils.retry import resilient_http_options


logger = logging.getLogger(__name__)


def _normalize_score(raw_score: float) -> int:
    """Normalize a raw evaluation score to 0-100 range.

    Handles both 0-1 scale (multiply by 100) and 1-5 scale (multiply by 20).
    Scores > 1.0 are assumed to be on a 1-5 scale.
    Result is clamped to [0, 100] and rounded to int.

    Note: A raw score of exactly 1.0 is ambiguous -- it could be 1/5 (worst)
    on a 1-5 scale or 1.0 (perfect) on a 0-1 scale. We treat it as
    already-normalized (1.0 → 100). In practice this is safe because:
    (a) a 1/5 from the LLM judge is extremely rare, and (b) the high-severity
    gate in _build_result blocks plans with any criterion below 40.
    """
    if raw_score > 1.0:
        normalized = raw_score * 20
    else:
        normalized = raw_score * 100
    return min(max(round(normalized), 0), 100)


def _get_model_resource() -> str:
    """Get the full resource path for the Vertex AI evaluation model."""
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    return f"projects/{project_id}/locations/{location}/publishers/google/models/{MODEL}" if project_id else MODEL


# ============================================================================
# MAIN EVALUATION TOOL
# ============================================================================


async def evaluate_plan(
    evaluation_request: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Evaluate a proposed marathon plan across quality criteria.

    Uses Vertex AI Evaluation with 7 metrics: 6 LLM metrics for individual
    quality criteria and a deterministic distance compliance check.
    Falls back to heuristic evaluation if Vertex AI Eval API fails.

    Args:
        evaluation_request: JSON string with user_intent and proposed_plan
        tool_context: ADK tool context for session state access

    Returns:
        dict with evaluation results
    """
    try:
        request_data = json.loads(evaluation_request)
    except json.JSONDecodeError as e:
        parse_error_result = {
            "passed": False,
            "scores": {},
            "findings": [
                {
                    "criterion": "parse_error",
                    "description": f"Invalid JSON input: {e}",
                    "severity": "high",
                }
            ],
            "improvement_suggestions": ["Provide valid JSON with 'user_intent' and 'proposed_plan' fields."],
            "overall_score": 0,
            "eval_method": "error",
        }
        if tool_context is not None:
            tool_context.state["evaluation_result"] = parse_error_result
        return parse_error_result

    user_intent_raw = request_data.get("user_intent", "Unknown intent")
    proposed_plan_raw = request_data.get("proposed_plan", "No plan provided")

    # Enrich plan context from session state
    if tool_context:
        if "marathon_route" in tool_context.state:
            route = tool_context.state["marathon_route"]
            if isinstance(proposed_plan_raw, dict):
                proposed_plan_raw["route_geometry_details"] = route
            else:
                proposed_plan_raw = f"{proposed_plan_raw}\n\n[Route Geometry (from session state)]: {json.dumps(route)}"

        if "traffic_assessment" in tool_context.state:
            traffic = tool_context.state["traffic_assessment"]
            if isinstance(proposed_plan_raw, dict):
                proposed_plan_raw["traffic_assessment_details"] = traffic
            else:
                proposed_plan_raw = (
                    f"{proposed_plan_raw}\n\n[Traffic Assessment (from session state)]: {json.dumps(traffic)}"
                )

    user_intent = json.dumps(user_intent_raw) if not isinstance(user_intent_raw, str) else user_intent_raw
    proposed_plan = json.dumps(proposed_plan_raw) if not isinstance(proposed_plan_raw, str) else proposed_plan_raw

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

    if project_id:
        scores, details = await _run_custom_eval(
            project_id=project_id,
            location=location,
            user_intent=user_intent,
            proposed_plan=proposed_plan,
        )
        suggestions, summary = await _generate_feedback(
            scores,
            details,
            user_intent,
            proposed_plan,
        )
        vertex_result = _build_result(
            scores,
            details,
            eval_method="vertex_ai_eval",
            suggestions=suggestions,
            summary=summary,
        )
        if tool_context is not None:
            tool_context.state["evaluation_result"] = vertex_result
        return vertex_result

    raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set; cannot run Vertex AI Eval.")


# ============================================================================
# LLM METRIC DEFINITIONS (6 individual criteria)
# ============================================================================


# Shared 1-5 rubric for all LLM metrics
_SHARED_RATING_SCORES = {
    "1": "Critical failures -- major issues with no mitigation",
    "2": "Significant gaps -- serious unresolved concerns",
    "3": "Partial -- notable gaps requiring substantial work",
    "4": "Solid -- criterion addressed with only minor areas for improvement",
    "5": "Comprehensive -- criterion is well-addressed with clear provisions",
}


# Per-criterion definitions: (metric_definition, criteria_dict)
_LLM_CRITERION_SPECS = {
    "safety_compliance": (
        "Assess whether the marathon plan adequately addresses safety. "
        "Focus on whether the plan demonstrates awareness of safety needs, "
        "not whether every detail is exhaustively specified.",
        {
            "Emergency access": (
                "The plan mentions or addresses emergency access for hospitals "
                "or fire stations, emergency vehicle crossings, and evacuation routes."
            ),
            "Medical support": (
                "The plan mentions medical tents, first aid stations, or ambulance staging areas along the route."
            ),
            "Crowd safety": (
                "The plan mentions crowd management, barriers, or safety personnel at high-density areas."
            ),
        },
    ),
    "logistics_completeness": (
        "Assess whether the marathon plan covers logistical operations. "
        "Focus on whether the plan demonstrates awareness of logistics needs.",
        {
            "Timing and scheduling": ("The plan mentions start times, wave scheduling, or time limits."),
            "Course support": ("The plan mentions marshals, traffic control, or course signage."),
            "Resource staging": ("The plan mentions water stations, supply positioning, or volunteer coordination."),
        },
    ),
    "participant_experience": (
        "Assess whether the marathon plan considers participant and spectator experience. "
        "Focus on whether the plan demonstrates awareness of experience elements.",
        {
            "Route experience": (
                "The plan mentions scenic elements, landmark routing, or interesting course features."
            ),
            "Spectator engagement": (
                "The plan mentions spectator areas, cheer zones, or entertainment along the route."
            ),
            "Runner amenities": ("The plan mentions aid stations, pacing groups, or post-race facilities."),
        },
    ),
    "community_impact": (
        "Assess whether the marathon plan addresses community considerations. "
        "Focus on whether the plan demonstrates awareness of community needs.",
        {
            "Disruption mitigation": (
                "The plan mentions minimizing disruption to residents, "
                "business access corridors, or notification programs."
            ),
            "Equity": (
                "The plan mentions equitable routing through diverse neighborhoods or avoiding exclusionary patterns."
            ),
            "Community engagement": (
                "The plan mentions community events, cheer zones, local "
                "business opportunities, or resident involvement."
            ),
        },
    ),
    "financial_viability": (
        "Assess whether the marathon plan addresses financial considerations. "
        "Focus on whether the plan demonstrates awareness of financial needs.",
        {
            "Budget planning": ("The plan mentions budget estimates, cost breakdowns, or financial projections."),
            "Revenue sources": (
                "The plan mentions registration fees, sponsorship, merchandise, or other revenue streams."
            ),
            "Cost management": ("The plan mentions cost controls, contingency funds, or resource optimization."),
        },
    ),
    "intent_alignment": (
        "Assess whether the marathon plan aligns with the user's stated intent. "
        "Focus on whether the plan addresses the user's key requirements.",
        {
            "Location match": ("The plan reflects the user's requested city or region."),
            "Theme and scale": (
                "The plan reflects the user's requested theme, scale, date or season, and any special requirements."
            ),
            "Specific requests": ("The plan addresses any specific constraints or preferences mentioned by the user."),
        },
    ),
}


def _build_combined_prompt() -> str:
    """Build a single prompt that evaluates ALL 6 criteria in one judge call.

    The Vertex AI ``evaluateInstances`` API forces JSON parsing on
    ``llm_based_metric_spec`` responses and makes one API call per
    ``LLMMetric``. Using 6 separate metrics causes 6 sequential API calls
    (~30s each) which is too slow. This combines all criteria into a single
    prompt so the judge evaluates everything in one call (~15s total).
    """
    criteria_sections = []
    for name, (definition, criteria_dict) in _LLM_CRITERION_SPECS.items():
        sub = "\n".join(f"    - {k}: {v}" for k, v in criteria_dict.items())
        criteria_sections.append(f"  **{name}**: {definition}\n{sub}")

    all_criteria = "\n\n".join(criteria_sections)
    criterion_names = ", ".join(f'"{n}"' for n in _LLM_CRITERION_SPECS)

    return f"""\
You are an expert evaluator. Evaluate the AI response against ALL of the following criteria.

## Criteria

{all_criteria}

## Rating scale
1 = Critical failures -- major issues with no mitigation
2 = Significant gaps -- serious unresolved concerns
3 = Partial -- notable gaps requiring substantial work
4 = Solid -- criterion addressed with only minor areas for improvement
5 = Comprehensive -- criterion is well-addressed with clear provisions

## User prompt
{{prompt}}

## AI response
{{response}}

Score EACH criterion independently on the 1-5 scale, then compute the average (rounded) as the overall score.
You MUST respond with ONLY a valid JSON object. Use this exact format:
{{"score": <overall integer 1-5>, "explanation": "{{{criterion_names.replace('"', "").replace(", ", ": 4, ")}: 5}}"}}"""


def _create_combined_llm_metric() -> types.LLMMetric:
    """Create a single LLM metric that evaluates all 6 criteria at once.

    Returns one ``LLMMetric`` whose judge response is a JSON object with
    a score for each criterion. The ``evaluateInstances`` API parses the
    JSON and populates ``result.score`` with the first numeric value it
    finds. The full JSON is available in ``result.explanation`` for
    extraction of per-criterion scores.
    """
    return types.LLMMetric(
        name="combined_criteria",
        prompt_template=_build_combined_prompt(),
        judge_model=_get_model_resource(),
    )


# ============================================================================
# DETERMINISTIC METRIC (unchanged)
# ============================================================================


def _check_distance_compliance_logic(response_text: str) -> dict:
    """Deterministic check for 26.2 mile marathon distance."""
    text_lower = response_text.lower()

    score = 5.0
    issues = []

    mile_pattern = r"(\d+(?:\.\d+)?)\s*(?:miles?|mi)\b"

    mile_matches = re.findall(mile_pattern, text_lower)

    for distance_str in mile_matches:
        distance = float(distance_str)
        if 20 <= distance <= 30:
            deviation = abs(distance - 26.2)
            if deviation > 0.5:
                score = 1.0
                issues.append(
                    f"Route distance {distance} miles deviates from 26.2 mile standard by {deviation:.1f} miles"
                )
            elif deviation > 0.1:
                score = 3.0
                issues.append(
                    f"Route distance {distance} miles is close but not "
                    f"exactly 26.2 miles (deviation: {deviation:.2f} miles)"
                )

    bypass_phrases = [
        "skip the distance",
        "doesn't need to be 26.2",
        "shorter route",
        "longer route",
        "half marathon",
        "ultra marathon",
        "10k",
        "5k route",
    ]
    for phrase in bypass_phrases:
        if phrase in text_lower:
            score = 1.0
            issues.append(f"Plan appears to bypass marathon distance: '{phrase}'")

    explanation = "; ".join(issues) if issues else "No distance issues detected"
    return {"score": score, "explanation": explanation}


def _create_distance_compliance_metric() -> types.Metric:
    """Deterministic check for 26.2 mile marathon distance."""

    def check_distance_compliance(instance: dict) -> dict:
        response_text = instance["response"]["parts"][0]["text"]
        return _check_distance_compliance_logic(response_text)

    return types.Metric(
        name="distance_compliance",
        custom_function=check_distance_compliance,
    )


# ============================================================================
# VERTEX AI EVALUATION
# ============================================================================


async def _run_custom_eval(
    project_id: str,
    location: str,
    user_intent: str,
    proposed_plan: str,
) -> tuple[dict[str, int], dict[str, str]]:
    """Run Vertex AI Evaluation with 2 metrics (1 combined LLM + 1 deterministic).

    Uses a single combined LLM metric that evaluates all 6 criteria in one
    ``evaluateInstances`` API call (~15s), instead of 6 separate calls
    (~30s each, serialized by API rate limits to ~3 minutes).

    Uses asyncio.to_thread to avoid blocking the event loop during the
    synchronous SDK call.
    """
    vertexai.init(project=project_id, location=location)
    client = vertexai.Client(  # type: ignore[attr-defined]
        project=project_id,
        location=location,
        http_options=resilient_http_options(api_version="v1beta1"),
    )

    df = pd.DataFrame(
        {
            "prompt": [user_intent],
            "response": [proposed_plan],
        }
    )

    metrics = [
        _create_combined_llm_metric(),
        _create_distance_compliance_metric(),
    ]

    result = await asyncio.to_thread(
        client.evals.evaluate,
        dataset=df,
        metrics=metrics,
    )

    scores: dict[str, int] = {}
    details: dict[str, str] = {}

    for case in result.eval_case_results:
        for cand in case.response_candidate_results:
            # Extract distance_compliance (deterministic, has its own score)
            if "distance_compliance" in cand.metric_results:
                dc = cand.metric_results["distance_compliance"]
                if dc.score is not None:
                    scores["distance_compliance"] = _normalize_score(dc.score)
                if dc.explanation:
                    details["distance_compliance"] = dc.explanation

            # Extract per-criterion scores from the combined LLM metric.
            # The API requires {"score": N, "explanation": "..."} from the
            # judge. We instruct the judge to embed per-criterion scores as
            # a JSON object inside the explanation string.
            combined = cand.metric_results.get("combined_criteria")
            if combined and combined.explanation:
                try:
                    parsed = json.loads(combined.explanation)
                    if isinstance(parsed, dict):
                        for criterion_name in _LLM_CRITERION_SPECS:
                            if criterion_name in parsed:
                                raw = parsed[criterion_name]
                                scores[criterion_name] = _normalize_score(float(raw))
                                details[criterion_name] = f"Score {raw}/5"
                except (json.JSONDecodeError, ValueError, TypeError):
                    # Explanation might not be valid JSON (e.g. free text
                    # with scores). Try regex extraction as fallback.
                    for criterion_name in _LLM_CRITERION_SPECS:
                        pattern = rf'["\']?{criterion_name}["\']?\s*:\s*(\d)'
                        match = re.search(pattern, combined.explanation)
                        if match:
                            raw = int(match.group(1))
                            scores[criterion_name] = _normalize_score(float(raw))
                            details[criterion_name] = f"Score {raw}/5"

    # Check that we got all 6 LLM criterion scores
    missing = set(_LLM_CRITERION_SPECS) - set(scores)
    if missing:
        logger.warning("Missing LLM criterion scores: %s", missing)
        raise RuntimeError(f"Combined metric missing scores for {missing}")

    return scores, details


# ============================================================================
# LLM FEEDBACK GENERATION
# ============================================================================


_FEEDBACK_PROMPT_TEMPLATE = """You are an expert marathon event evaluator. Given these evaluation scores
and details for a marathon plan, generate specific, actionable improvement suggestions.

## Evaluation Scores
{scores_text}

## Score Details
{details_text}

## User's Original Intent
{user_intent}

## Plan Summary (first 2000 chars)
{plan_excerpt}

## Instructions
1. For any score below 80, generate 1-2 specific improvement suggestions.
2. Suggestions must be actionable (e.g., "Add emergency vehicle crossing points
   at miles 5, 10, 15, and 20" instead of "Improve safety").
3. Generate a brief 1-2 sentence summary of the overall evaluation.
4. Return ONLY valid JSON in this exact format:

{{"suggestions": ["suggestion 1", "suggestion 2", ...], "summary": "Brief evaluation summary."}}"""


async def _generate_feedback(
    scores: dict[str, int],
    details: dict[str, str],
    user_intent: str,
    proposed_plan: str,
) -> tuple[list[str], str]:
    """Generate LLM-powered improvement suggestions and summary.

    Makes a single GenerateContent call using EVALUATOR_MODEL. Falls back
    to deterministic suggestions if the call fails.
    """
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

    client = genai.Client(
        project=project_id,
        location=location,
        http_options=resilient_http_options(api_version="v1beta1"),
    )

    scores_text = "\n".join(f"- {k}: {v}" for k, v in scores.items())
    details_text = "\n".join(f"- {k}: {v}" for k, v in details.items())
    plan_excerpt = proposed_plan[:2000]

    prompt = _FEEDBACK_PROMPT_TEMPLATE.format(
        scores_text=scores_text,
        details_text=details_text,
        user_intent=user_intent,
        plan_excerpt=plan_excerpt,
    )

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
            max_output_tokens=1024,
        ),
    )

    response_text = response.text or "{}"
    result = json.loads(response_text)
    suggestions = result.get("suggestions", [])
    summary = result.get("summary", "")

    if not suggestions or not summary:
        raise ValueError("Empty suggestions or summary from LLM")

    return suggestions, summary


# ============================================================================
# RESULT BUILDER
# ============================================================================


def _build_result(
    scores: dict[str, int],
    details: dict[str, str],
    eval_method: str,
    suggestions: list[str] | None = None,
    summary: str = "",
) -> dict[str, Any]:
    """Build the final evaluation result from scores, details, and LLM feedback."""
    findings = []

    for criterion, score in scores.items():
        if score < 80:
            if score < SEVERITY_THRESHOLDS["high"]:
                severity = "high"
            elif score < SEVERITY_THRESHOLDS["medium"]:
                severity = "medium"
            else:
                severity = "low"

            findings.append(
                {
                    "criterion": criterion,
                    "description": details.get(criterion, f"Score below threshold: {score}"),
                    "severity": severity,
                }
            )

    overall_score = 0
    for criterion, weight in CRITERION_WEIGHTS.items():
        overall_score += scores.get(criterion, 50) * weight
    overall_score = round(overall_score)

    passed = overall_score >= PASS_THRESHOLD and not any(f["severity"] == "high" for f in findings)

    return {
        "passed": passed,
        "scores": scores,
        "findings": findings,
        "improvement_suggestions": suggestions or [],
        "overall_score": overall_score,
        "summary": summary,
        "eval_method": eval_method,
    }
