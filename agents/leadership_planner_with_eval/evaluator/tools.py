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

"""LLM-as-Judge evaluator for leadership simulation scenarios.

Mirrors agents/planner_with_eval/evaluator/tools.py but evaluates
leadership scenarios against 6 enterprise-grade criteria instead of
7 marathon criteria.  Includes session-state enrichment, weighted
scoring via config, and keyword-based heuristic fallback.
"""

import hashlib
import json
import logging
import os
from typing import Any, Dict

from google.genai import Client, types

from agents.utils.retry import resilient_http_options

from agents.leadership_planner_with_eval.evaluator.config import (
    CRITERION_WEIGHTS,
    EVALUATION_CRITERIA,
    MODEL,
    PASS_THRESHOLD,
    SEVERITY_THRESHOLDS,
)

logger = logging.getLogger(__name__)

EVALUATOR_MODEL = MODEL

# Module-level client singleton — reuses connections across evaluations.
# Mirrors the marathon evaluator pattern with project/location config.
_client: Client | None = None


def _get_client() -> Client:
    """Lazy-singleton GenAI client with project config and retry options."""
    global _client
    if _client is None:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        # M10 fix: Default to "global" to match the rest of the codebase.
        # "us-central1" doesn't support all Gemini model variants and caused
        # silent fallback to heuristic scoring.
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        kwargs: dict = {
            "http_options": resilient_http_options(api_version="v1beta1"),
        }
        if project_id:
            kwargs["project"] = project_id
        if location:
            kwargs["location"] = location
        _client = Client(**kwargs)
    return _client


# ---------------------------------------------------------------------------
# Evaluation criteria
# ---------------------------------------------------------------------------

LEADERSHIP_CRITERIA = [
    {
        "key": "stakeholder_alignment",
        "name": "Stakeholder Alignment",
        "description": (
            "Evaluates persona diversity, sentiment balance, power dynamics, "
            "and coalition potential across the stakeholder topology."
        ),
    },
    {
        "key": "decision_quality",
        "name": "Decision Quality",
        "description": (
            "Assesses dilemma complexity, trade-off clarity, consequence "
            "cascading, and absence of obviously 'correct' answers."
        ),
    },
    {
        "key": "resource_stewardship",
        "name": "Resource Stewardship",
        "description": (
            "Evaluates budget allocation sustainability, scarcity pressure, "
            "regeneration mechanics, and cost-benefit rationale."
        ),
    },
    {
        "key": "crisis_readiness",
        "name": "Crisis Readiness",
        "description": (
            "Assesses black swan preparedness, escalation paths, resilience testing, and contingency planning."
        ),
    },
    {
        "key": "strategic_coherence",
        "name": "Strategic Coherence",
        "description": (
            "Evaluates goal alignment, theme consistency, progressive complexity, and stakeholder motivation logic."
        ),
    },
    {
        "key": "ethical_alignment",
        "name": "Ethical Alignment",
        "description": (
            "Assesses ethical boundary testing, policy compliance, fairness metrics, and transparency requirements."
        ),
    },
]

# ---------------------------------------------------------------------------
# Evaluator prompt
# ---------------------------------------------------------------------------

EVALUATOR_PROMPT = """\
You are the Leadership Scenario Quality Evaluator (LLM-as-Judge).

Evaluate the following leadership simulation scenario against 6 criteria.
For each criterion, assign a score from 0 to 100 (integer only) and provide
a brief justification.

## Criteria

{criteria_block}

## Scenario to Evaluate

{plan_text}

## Output Format
Return a JSON object with this exact structure:
```json
{{
  "overall_score": <int 0-100>,
  "scores": {{
    "stakeholder_alignment": <int 0-100>,
    "decision_quality": <int 0-100>,
    "resource_stewardship": <int 0-100>,
    "crisis_readiness": <int 0-100>,
    "strategic_coherence": <int 0-100>,
    "ethical_alignment": <int 0-100>
  }},
  "findings": [
    {{"title": "<finding title>", "severity": "MAJOR|MODERATE|MINOR", "description": "<details>"}}
  ]
}}
```

Rules:
- overall_score is the weighted average (equal weights).
- Each criterion score MUST reflect the SPECIFIC content of the scenario.
  Different scenarios MUST receive meaningfully different scores.
- USE THE FULL 0-100 RANGE. A score of 50 means "adequate". 70 means "good".
  90+ means "exceptional with comprehensive detail". Below 40 means "poor
  with significant gaps". Distribute your scores across the full spectrum.
- findings should list 2-4 actionable coaching tips (drawing on change management
  frameworks like Kotter's 8-Step Model or ADKAR) to help the user better
  articulate and design their transformation strategy for success.
- ALL scores must be integers 0-100.
- Output exclusively the JSON object.
"""


def _build_criteria_block() -> str:
    """Build the criteria description block for the evaluator prompt."""
    lines = []
    for i, c in enumerate(LEADERSHIP_CRITERIA, 1):
        lines.append(f"{i}. **{c['name']}** ({c['key']}): {c['description']}")
    return "\n".join(lines)


def _normalize_score(raw_score: float) -> int:
    """Normalize an LLM-returned score to 0-100 integer range.

    Mirrors the Marathon evaluator's _normalize_score guard.
    LLMs occasionally return scores on a 1-5 scale despite being asked
    for 0-100.  This function detects and rescales those.

    Args:
        raw_score: Raw score from LLM (possibly on 0-1, 1-5, or 0-100 scale).

    Returns:
        Integer score clamped to [0, 100].
    """
    if raw_score <= 1.0:
        normalized = raw_score * 100  # 0-1 fractional scale → 0-100
    elif raw_score <= 5.0:
        normalized = raw_score * 20  # 1-5 scale → 0-100
    else:
        normalized = raw_score  # Already 0-100
    return min(max(round(normalized), 0), 100)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LLM feedback generation (mirrors marathon _generate_feedback)
# ---------------------------------------------------------------------------

_FEEDBACK_PROMPT_TEMPLATE = """You are an expert leadership simulation evaluator. Given these evaluation scores
and details for a leadership scenario, generate specific, actionable improvement suggestions.

## Evaluation Scores
{scores_text}

## Score Details
{details_text}

## User's Original Intent
{user_intent}

## Scenario Summary (first 2000 chars)
{plan_excerpt}

## Instructions
1. For any score below 80, generate 1-2 specific improvement suggestions.
2. Suggestions must be actionable (e.g., "Add a dissenting stakeholder with veto power
   to the budget committee", providing specific constructive alternatives).
3. Generate a brief 1-2 sentence summary of the overall evaluation.
4. Output exclusively valid JSON in this exact format:

{{"suggestions": ["suggestion 1", "suggestion 2", ...], "summary": "Brief evaluation summary."}}"""


_DETERMINISTIC_SUGGESTIONS = {
    "stakeholder_alignment": (
        "Diversify the stakeholder topology by adding roles with conflicting "
        "priorities, varied seniority levels, and cross-departmental dependencies."
    ),
    "decision_quality": (
        "Increase dilemma complexity by removing obviously correct choices, adding "
        "time pressure, and introducing cascading consequences across departments."
    ),
    "resource_stewardship": (
        "Add budget constraints with competing allocation demands, introduce "
        "scarcity mechanics, and require cost-benefit justification for key decisions."
    ),
    "crisis_readiness": (
        "Include black swan events with escalation paths, contingency planning "
        "requirements, and resilience testing under cascading failure scenarios."
    ),
    "strategic_coherence": (
        "Ensure all scenario phases align with stated strategic goals, maintain "
        "progressive complexity, and connect stakeholder motivations to outcomes."
    ),
    "ethical_alignment": (
        "Introduce ethical boundary-testing dilemmas, policy compliance checks, "
        "fairness metrics, and transparency requirements across decision rounds."
    ),
}


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
    client = _get_client()

    scores_text = "\n".join(f"- {k}: {v}" for k, v in scores.items())
    details_text = "\n".join(f"- {k}: {v}" for k, v in details.items())
    plan_excerpt = proposed_plan[:2000]

    prompt = _FEEDBACK_PROMPT_TEMPLATE.format(
        scores_text=scores_text,
        details_text=details_text,
        user_intent=user_intent,
        plan_excerpt=plan_excerpt,
    )

    response = await client.aio.models.generate_content(
        model=EVALUATOR_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
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


# ---------------------------------------------------------------------------
# Result builder (mirrors marathon _build_result)
# ---------------------------------------------------------------------------


def _extract_response_reason(response: Any) -> str:
    """Return a short human-readable reason describing why ``response.text`` is empty.

    Reads (in order of precedence):

    1. ``response.prompt_feedback.block_reason`` — whole-prompt block (safety,
       recitation, other).
    2. ``response.candidates[0].finish_reason`` — SAFETY, MAX_TOKENS,
       PROHIBITED_CONTENT, RECITATION, etc.
    3. ``response.candidates`` missing entirely — model produced no output.

    Everything is best-effort; any attribute access failure returns
    ``"unknown"`` so a diagnostic path can never itself crash the tool.
    Kept as a plain helper (not tied to any specific SDK type) so the
    return value can be unit-tested with SimpleNamespace stubs.
    """
    try:
        feedback = getattr(response, "prompt_feedback", None)
        block = getattr(feedback, "block_reason", None) if feedback is not None else None
        if block:
            return f"prompt_blocked:{block}"
    except Exception:  # noqa: BLE001 - diagnostic must never re-raise
        pass

    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return "no_candidates"
        first = candidates[0]
        finish = getattr(first, "finish_reason", None)
        if finish is None:
            return "no_finish_reason"
        finish_str = getattr(finish, "name", None) or str(finish)
        return f"finish_reason:{finish_str}"
    except Exception:  # noqa: BLE001
        return "unknown"


def _empty_response_result(reason: str, input_hash: str) -> Dict[str, Any]:
    """Build a graceful "the LLM returned no text" result.

    Same shape as ``_build_result`` so downstream tools (``store_scenario``,
    the planner workflow) can treat this identically to a real evaluation
    that scored zero. ``eval_method="empty_llm_response"`` lets ops filter
    these out in logs and lets the LLM planner detect the failure mode
    via ``result["eval_method"]`` and choose to retry with a tightened
    plan. Downstream will not crash on this shape — it is the same
    contract every other evaluation return honours.
    """
    zero_scores = {c: 0 for c in EVALUATION_CRITERIA}
    finding = {
        "title": "Evaluator produced no scorable output",
        "severity": "MAJOR",
        "description": (
            f"The LLM evaluator returned an empty response ({reason}). "
            "The scenario was NOT scored. This usually means the prompt was "
            "blocked by safety filters, exceeded the output token budget, "
            "or the model returned no candidates. Retry with a shorter "
            "plan_text, fewer stakeholders in the enrichment blob, or a "
            "less sensitive framing."
        ),
    }
    return {
        "passed": False,
        "scores": zero_scores,
        "findings": [finding],
        "improvement_suggestions": [
            "Evaluator returned no text; retry with a smaller plan input or "
            "verify safety filter settings. See finding for details."
        ],
        "overall_score": 0,
        "summary": f"Evaluator empty response ({reason}); scenario NOT scored (input_hash={input_hash}).",
        "eval_method": "empty_llm_response",
    }


def _build_result(
    scores: dict[str, int],
    details: dict[str, str],
    eval_method: str,
    findings: list[dict] | None = None,
    summary: str = "",
    improvement_suggestions: list[str] | None = None,
) -> dict[str, Any]:
    """Build the final evaluation result from scores, details, and findings."""
    if findings is None:
        findings = []
        for criterion, score in scores.items():
            if score < 80:
                if score < SEVERITY_THRESHOLDS["high"]:
                    severity = "MAJOR"
                elif score < SEVERITY_THRESHOLDS["medium"]:
                    severity = "MODERATE"
                else:
                    severity = "MINOR"
                findings.append(
                    {
                        "title": f"Improve {criterion.replace('_', ' ').title()}",
                        "severity": severity,
                        "description": details.get(criterion, f"Score below threshold: {score}"),
                    }
                )

    # Weighted overall score
    overall_score = 0
    for criterion, weight in CRITERION_WEIGHTS.items():
        overall_score += scores.get(criterion, 50) * weight
    overall_score = round(overall_score)

    passed = overall_score >= PASS_THRESHOLD and not any(f.get("severity") == "MAJOR" for f in findings)

    return {
        "passed": passed,
        "scores": scores,
        "findings": findings,
        "improvement_suggestions": improvement_suggestions or [],
        "overall_score": overall_score,
        "summary": summary,
        "eval_method": eval_method,
    }


# ---------------------------------------------------------------------------
# Tool: evaluate_leadership_plan
# ---------------------------------------------------------------------------


async def evaluate_leadership_plan(
    evaluation_request: str,
    tool_context=None,
) -> Dict[str, Any]:
    """Evaluate a leadership scenario using LLM-as-Judge scoring.

    Scores the scenario against 6 leadership criteria:
    stakeholder_alignment, decision_quality, resource_stewardship,
    crisis_readiness, strategic_coherence, ethical_alignment.

    Args:
        evaluation_request: JSON string with ``user_intent`` and
            ``proposed_plan`` keys describing the scenario to evaluate.
            Falls back to treating the entire string as plan text if
            not valid JSON.
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with overall_score (0-100), scores dict (6 criteria),
        and findings list.
    """
    # --- Parse structured input (mirror marathon pattern) ---
    try:
        request_data = json.loads(evaluation_request)
        user_intent = request_data.get("user_intent", "Leadership scenario evaluation")
        proposed_plan = request_data.get("proposed_plan", evaluation_request)
    except (json.JSONDecodeError, TypeError):
        # Graceful fallback: treat entire input as plan text
        user_intent = "Leadership scenario evaluation"
        proposed_plan = str(evaluation_request)

    # --- Enrich from session state (the key missing piece) ---
    if tool_context:
        scenario = tool_context.state.get("leadership_scenario")
        if isinstance(scenario, dict):
            scenario_meta = {
                "name": scenario.get("name", "Unknown"),
                "scenario_type": scenario.get("scenario_type", "unknown"),
                "stakeholder_count": scenario.get("stakeholder_count", 0),
                "complexity": scenario.get("complexity", "unknown"),
                "summary": scenario.get("summary", ""),
            }
            proposed_plan = f"{proposed_plan}\n\n[Scenario Metadata (from session state)]: {json.dumps(scenario_meta)}"

        pathway = tool_context.state.get("consensus_pathway_active")
        if isinstance(pathway, dict):
            pathway_summary = {
                "persona": pathway.get("persona", "pragmatist"),
                "milestones": len(pathway.get("milestones", [])),
                "waypoints": len(pathway.get("waypoints", [])),
            }
            proposed_plan = (
                f"{proposed_plan}\n\n[Consensus Pathway (from session state)]: {json.dumps(pathway_summary)}"
            )

        stakeholders = tool_context.state.get("stakeholder_map")
        if isinstance(stakeholders, (dict, list)):
            # Cap at ~1200 chars: nothing about the evaluation task benefits
            # from a giant stakeholder blob, and larger enrichments push the
            # model toward MAX_TOKENS finish-reason with empty visible output
            # (see 2026-07-02 evaluator empty-response fix). Previously 2000.
            proposed_plan = (
                f"{proposed_plan}\n\n[Stakeholder Map (from session state)]: {json.dumps(stakeholders)[:1200]}"
            )

    # --- Cap total prompt input to protect the output-token budget ---
    # The visible-output budget is finite (see max_output_tokens below).
    # Overly large prompts push Gemini 2.5's thinking traces to consume the
    # entire budget, leaving ``response.text`` as ``None`` and causing the
    # tool to fall through to the empty-response guard. Truncating here at
    # ~8000 chars keeps prompts well within the safe operating band while
    # still preserving the plan's essential structure for scoring.
    _PROMPT_CHAR_CAP = 8000
    if len(proposed_plan) > _PROMPT_CHAR_CAP:
        overflow = len(proposed_plan) - _PROMPT_CHAR_CAP
        proposed_plan = proposed_plan[:_PROMPT_CHAR_CAP] + f"\n\n[... {overflow} chars truncated for evaluator budget ...]"

    # --- Diagnostic logging: hash to detect identical inputs ---
    input_hash = hashlib.md5(proposed_plan.encode()).hexdigest()[:12]
    logger.info(
        "evaluate_leadership_plan: input_hash=%s, plan_len=%d",
        input_hash,
        len(proposed_plan),
    )

    # --- Build LLM prompt ---
    criteria_block = _build_criteria_block()
    prompt = EVALUATOR_PROMPT.format(
        criteria_block=criteria_block,
        plan_text=proposed_plan,
    )

    client = _get_client()
    response = await client.aio.models.generate_content(
        model=EVALUATOR_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.5,  # Higher temperature for better score variance (Marathon uses Vertex AI Eval internally)
            # Bumped 2048 -> 4096 in the 2026-07-02 empty-response fix. Gemini
            # 2.5's thinking budget was consuming enough of the previous 2048
            # ceiling that the visible JSON output was truncated to ``None``,
            # crashing the tool with ``NoneType has no attribute 'strip'``.
            # Pairing with an explicit thinking_budget below preserves ~3000
            # tokens for the JSON response, which is 4-5x what the schema needs.
            max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
            response_mime_type="application/json",
        ),
    )

    # --- Defensive read: ``response.text`` is ``None`` (not empty string)
    # when the candidate has no text part (safety block, MAX_TOKENS with
    # thought-only output, PROHIBITED_CONTENT, RECITATION, or missing
    # candidates entirely). Calling .strip() on None was the original bug.
    raw_text = response.text
    if raw_text is None or not raw_text.strip():
        reason = _extract_response_reason(response)
        logger.warning(
            "evaluate_leadership_plan: LLM returned empty text (reason=%s, input_hash=%s). "
            "Returning graceful empty_llm_response result instead of raising.",
            reason,
            input_hash,
        )
        result = _empty_response_result(reason=reason, input_hash=input_hash)
        if tool_context:
            tool_context.state["leadership_evaluation"] = result
            tool_context.state["evaluation_result"] = result  # Marathon compatibility
        return result

    result_text = raw_text.strip()

    # Parse JSON response
    try:
        raw_result = json.loads(result_text)
    except json.JSONDecodeError:
        # Try extracting JSON from markdown code block
        if "```json" in result_text:
            try:
                json_str = result_text.split("```json")[1].split("```")[0].strip()
                raw_result = json.loads(json_str)
            except (IndexError, json.JSONDecodeError):
                return _empty_response_result(reason="json_decode_error", input_hash=input_hash)
        elif "```" in result_text:
            try:
                json_str = result_text.split("```")[1].split("```")[0].strip()
                raw_result = json.loads(json_str)
            except (IndexError, json.JSONDecodeError):
                return _empty_response_result(reason="json_decode_error", input_hash=input_hash)
        else:
            return _empty_response_result(reason="json_decode_error", input_hash=input_hash)

    # Extract scores and build proper result via _build_result
    llm_scores = raw_result.get("scores", {})
    llm_findings = raw_result.get("findings", [])
    llm_summary = raw_result.get("summary", "")

    # Normalize and validate all criteria scores (Marathon pattern)
    for c in EVALUATION_CRITERIA:
        if c in llm_scores:
            llm_scores[c] = _normalize_score(llm_scores[c])
        else:
            logger.warning("LLM evaluator missing criterion %r — defaulting to 0", c)
            llm_scores[c] = 0

    details = {c: f"LLM score: {llm_scores.get(c, '?')}" for c in EVALUATION_CRITERIA}

    # --- Generate actionable improvement suggestions ---
    # Prefer LLM-generated feedback (richer, contextual) via
    # _generate_feedback; fall back to deterministic extraction from
    # findings when the LLM call fails or returns empty.
    suggestions = []
    feedback_summary = ""
    try:
        suggestions, feedback_summary = await _generate_feedback(
            scores=llm_scores,
            details=details,
            user_intent=user_intent,
            proposed_plan=proposed_plan,
        )
    except Exception:
        logger.info(
            "evaluate_leadership_plan: _generate_feedback failed, "
            "falling back to deterministic suggestions (input_hash=%s)",
            input_hash,
        )

    if not suggestions:
        # Deterministic fallback: extract from findings
        if isinstance(llm_findings, list):
            for f in llm_findings:
                if isinstance(f, dict) and "title" in f and "description" in f:
                    suggestions.append(f"{f['title']}: {f['description']}")

    if not suggestions:
        suggestions = ["No major improvements needed based on current evaluation."]

    result = _build_result(
        scores=llm_scores,
        details=details,
        eval_method="llm_judge",
        findings=llm_findings if llm_findings else None,
        summary=feedback_summary or llm_summary or "Evaluation completed.",
        improvement_suggestions=suggestions,
    )

    # Store evaluation in session state for downstream tools
    if tool_context:
        tool_context.state["leadership_evaluation"] = result
        tool_context.state["evaluation_result"] = result  # Marathon compatibility

    logger.info(
        "evaluate_leadership_plan: overall_score=%s, method=%s, findings=%d, input_hash=%s",
        result.get("overall_score", "?"),
        result.get("eval_method", "?"),
        len(result.get("findings", [])),
        input_hash,
    )

    return result
