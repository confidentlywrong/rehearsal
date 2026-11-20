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
scoring via config, and local Gemma LLM-as-Judge execution via
generate_local_json_async when local Gemma mode is active.
"""

import hashlib
import json
import logging
import math
import os
from typing import Any, Dict

from google.genai import Client, types

import agents.utils.local_ai as local_ai
from agents.leadership_planner_with_eval.evaluator.config import (
    CRITERION_WEIGHTS,
    EVALUATION_CRITERIA,
    MODEL,
    PASS_THRESHOLD,
    SEVERITY_THRESHOLDS,
)
from agents.utils.retry import resilient_http_options


def is_local_gemma_active() -> bool:
    """Check if local Gemma is active, delegating to agents.utils.local_ai."""
    return local_ai.is_local_gemma_active()


async def generate_local_json_async(*args: Any, **kwargs: Any) -> Any:
    """Generate local JSON async, delegating to agents.utils.local_ai."""
    return await local_ai.generate_local_json_async(*args, **kwargs)


logger = logging.getLogger(__name__)


def _strip_nonfinite(obj: object, seen: set[int] | None = None) -> object:
    """Recursively replace non-finite floats (NaN/Inf) with None for RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (str, int, bool, type(None))):
        return obj
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return f"<CircularReference: {type(obj).__name__}>"
    seen.add(obj_id)
    try:
        if isinstance(obj, dict):
            return {
                (
                    k
                    if isinstance(k, (str, int, bool, type(None)))
                    or (isinstance(k, float) and math.isfinite(k))
                    else str(k)
                ): _strip_nonfinite(v, seen)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [_strip_nonfinite(v, seen) for v in obj]
        return obj
    finally:
        seen.remove(obj_id)


def _strict_json_dumps(obj: object) -> str:
    """Serialize *obj* to strict RFC-8259 JSON with default=str fallback."""
    try:
        return json.dumps(obj, allow_nan=False, default=str)
    except (ValueError, TypeError):
        return json.dumps(_strip_nonfinite(obj), allow_nan=False, default=str)

EVALUATOR_MODEL = MODEL

# Module-level client singleton — reuses connections across evaluations.
# Mirrors the marathon evaluator pattern with project/location config.
_client: Client | None = None
_client_key: tuple | None = None


def _get_client() -> Client:
    """Lazy-singleton GenAI client with project/API-key config and retry options."""
    global _client, _client_key
    project_id = (
        (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
        or (os.environ.get("PROJECT_ID") or "").strip()
    )
    # M10 fix: Default to "global" to match the rest of the codebase.
    # "us-central1" doesn't support all Gemini model variants and caused
    # silent fallback to heuristic scoring.
    location = (os.environ.get("GOOGLE_CLOUD_LOCATION") or "").strip() or "global"
    use_vertex = (os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") or "").strip().lower() not in ("0", "false", "no")
    api_key = (
        (os.environ.get("GEMINI_API_KEY") or "").strip()
        or (os.environ.get("GOOGLE_API_KEY") or "").strip()
    )
    vertex_active = bool(project_id and use_vertex)
    key = (
        vertex_active,
        project_id if vertex_active else "",
        location if vertex_active else "",
        "" if vertex_active else api_key,
    )
    if _client is None or (_client_key is not None and _client_key != key):
        kwargs: dict = {
            "http_options": resilient_http_options(api_version="v1beta1"),
        }
        if vertex_active:
            kwargs["vertexai"] = True
            kwargs["project"] = project_id
            if location:
                kwargs["location"] = location
        elif api_key:
            kwargs["vertexai"] = False
            kwargs["api_key"] = api_key
        _client = Client(**kwargs)
        _client_key = key
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

LOCAL_EVALUATOR_PROMPT = """\
Score this leadership scenario (0-100 ints) and output ONLY compact JSON:
{{
  "summary": "<1 short sentence max 10 words>",
  "improvement_suggestions": ["<1 actionable tip max 10 words>"],
  "scores": {{
    "stakeholder_alignment": <int>,
    "decision_quality": <int>,
    "resource_stewardship": <int>,
    "crisis_readiness": <int>,
    "strategic_coherence": <int>,
    "ethical_alignment": <int>
  }}
}}

Scenario:
{plan_text}
"""


def _build_criteria_block() -> str:
    """Build the criteria description block for the evaluator prompt."""
    lines = []
    for i, c in enumerate(LEADERSHIP_CRITERIA, 1):
        lines.append(f"{i}. **{c['name']}** ({c['key']}): {c['description']}")
    return "\n".join(lines)


def _normalize_score(raw_score: Any) -> int:
    """Normalize an LLM-returned score to 0-100 integer range.

    Mirrors the Marathon evaluator's _normalize_score guard.
    LLMs occasionally return scores on a 1-5 scale despite being asked
    for 0-100, or return numeric strings / nested dicts. This function
    detects, coerces, and rescales those safely.

    Args:
        raw_score: Raw score from LLM (possibly on 0-1, 1-5, or 0-100 scale).

    Returns:
        Integer score clamped to [0, 100].
    """
    if isinstance(raw_score, dict):
        raw_score = raw_score.get("score", raw_score.get("value", 0))
    if isinstance(raw_score, bool):
        return 0
    try:
        val = float(raw_score)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(val):
        return 0
    if val <= 0.0:
        return 0
    if val <= 1.0:
        normalized = val * 100  # 0-1 fractional scale → 0-100
    elif val <= 5.0:
        normalized = val * 20  # 1-5 scale → 0-100
    else:
        normalized = val  # Already 0-100
    if not math.isfinite(normalized) or normalized >= 100.0:
        return 100
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


async def _generate_feedback(
    scores: dict[str, int],
    details: dict[str, str],
    user_intent: str,
    proposed_plan: str,
) -> tuple[list[str], str]:
    """Generate LLM-powered improvement suggestions and summary.

    Makes a single GenerateContent call using EVALUATOR_MODEL in cloud mode
    or generate_local_json_async in local Gemma mode, and raises on LLM or
    JSON failure.
    """
    scores_text = "\n".join(f"- {k}: {v}" for k, v in scores.items())
    details_text = "\n".join(f"- {k}: {v}" for k, v in details.items())
    local_active = is_local_gemma_active()
    plan_excerpt = proposed_plan[:900] if local_active else proposed_plan[:2000]

    prompt = _FEEDBACK_PROMPT_TEMPLATE.format(
        scores_text=scores_text,
        details_text=details_text,
        user_intent=user_intent,
        plan_excerpt=plan_excerpt,
    )

    if local_active:
        response_text = (
            await generate_local_json_async(
                prompt + "\nBe concise: 1-2 short suggestions (<12 words each) and 1 concise sentence summary.",
                temperature=0.3,
                max_tokens=200,
                timeout=240.0,
            )
            or "{}"
        )
    else:
        client = _get_client()
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

    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    result = json.loads(cleaned)
    if not isinstance(result, dict):
        raise ValueError("Non-object JSON response from LLM")
    raw_suggestions = result.get("suggestions", [])
    suggestions: list[str] = []
    if isinstance(raw_suggestions, list):
        for item in raw_suggestions:
            if isinstance(item, str) and item.strip():
                suggestions.append(item.strip())
            elif isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                desc = str(
                    item.get("description")
                    or item.get("suggestion")
                    or item.get("text")
                    or ""
                ).strip()
                if title and desc:
                    suggestions.append(f"{title}: {desc}")
                elif desc or title:
                    suggestions.append(desc or title)
    summary = str(result.get("summary") or "").strip()

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


def _error_result(
    *,
    reason: str,
    eval_method: str,
    input_hash: str,
    title: str,
    description: str,
    suggestion: str,
) -> Dict[str, Any]:
    """Build a structured non-passing error result for failed evaluator paths."""
    zero_scores = {c: 0 for c in EVALUATION_CRITERIA}
    return {
        "status": "error",
        "passed": False,
        "scores": zero_scores,
        "findings": [
            {
                "title": title,
                "severity": "MAJOR",
                "description": description,
            }
        ],
        "improvement_suggestions": [suggestion],
        "overall_score": 0,
        "summary": f"{title} ({reason}); scenario NOT scored (input_hash={input_hash}).",
        "eval_method": eval_method,
    }


def _empty_response_result(reason: str, input_hash: str) -> Dict[str, Any]:
    """Build a structured error result when the LLM returns no text."""
    return _error_result(
        reason=reason,
        eval_method="empty_llm_response",
        input_hash=input_hash,
        title="Evaluator produced no scorable output",
        description=(
            f"The LLM evaluator returned an empty response ({reason}). "
            "The scenario was NOT scored. This usually means the prompt was "
            "blocked by safety filters, exceeded the output token budget, "
            "or the model returned no candidates. Retry with a shorter "
            "plan_text, fewer stakeholders in the enrichment blob, or a "
            "less sensitive framing."
        ),
        suggestion=(
            "Evaluator returned no text; retry with a smaller plan input or "
            "verify safety filter settings. See finding for details."
        ),
    )


def _is_memory_persistence_configured() -> bool:
    """Return True when AlloyDB env vars are set or a non-AlloyDB test store is active."""
    if os.environ.get("ALLOYDB_HOST", "").strip() or os.environ.get("ALLOYDB_INSTANCE_CONNECTION_NAME", "").strip():
        return True
    try:
        from agents.leadership_planner_with_memory.memory import tools as mem_tools
        from agents.leadership_planner_with_memory.memory.store_alloydb import AlloyDBScenarioStore

        return not isinstance(mem_tools._store, AlloyDBScenarioStore)
    except ImportError:
        return False


async def _auto_store_scenario_if_configured(tool_context: Any) -> None:
    """Persist evaluated scenario when memory persistence is configured, logging errors loudly."""
    if not _is_memory_persistence_configured():
        return
    try:
        from agents.leadership_planner_with_memory.memory.tools import store_scenario

        store_res = await store_scenario(tool_context)
        if isinstance(store_res, dict):
            if store_res.get("status") == "error":
                logger.error(
                    "evaluate_leadership_plan: auto store_scenario returned error: %s",
                    store_res.get("message") or store_res,
                )
            elif store_res.get("artifacts_autoflush_error"):
                logger.warning(
                    "evaluate_leadership_plan: auto store_scenario artifacts autoflush error: %s",
                    store_res.get("artifacts_autoflush_error"),
                )
    except Exception as exc:
        logger.error(
            "evaluate_leadership_plan: auto store_scenario failed: %s",
            exc,
            exc_info=True,
        )


def _normalize_finding(raw_finding: Any) -> dict[str, Any] | None:
    """Normalize a single LLM finding dict and canonicalize severity."""
    if not isinstance(raw_finding, dict):
        return None
    raw_sev = str(raw_finding.get("severity") or "MODERATE").strip().upper()
    sev_aliases = {
        "HIGH": "MAJOR",
        "CRITICAL": "MAJOR",
        "SEVERE": "MAJOR",
        "MAJOR": "MAJOR",
        "MEDIUM": "MODERATE",
        "MODERATE": "MODERATE",
        "LOW": "MINOR",
        "MINOR": "MINOR",
    }
    severity = sev_aliases.get(raw_sev, "MODERATE")
    normalized = dict(raw_finding)
    normalized["severity"] = severity
    normalized.setdefault("title", "Evaluation Finding")
    normalized.setdefault("description", "")
    return normalized


def _coerce_result_score(raw_score: Any) -> int:
    """Safely coerce an already-normalized score to an int in [0, 100].

    Unlike ``_normalize_score``, this does not rescale 1-5 values, so direct
    callers of ``_build_result`` with small 0-100 scores keep their exact values
    while non-numeric / bool / NaN / Inf / overflowing inputs default to 0.
    """
    if raw_score is None or isinstance(raw_score, bool):
        return 0
    if isinstance(raw_score, int):
        return max(0, min(100, raw_score))
    try:
        val = float(raw_score)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(val):
        return 0
    return max(0, min(100, round(val)))


def _build_result(
    scores: dict[str, int],
    details: dict[str, str],
    eval_method: str,
    findings: list[dict] | None = None,
    summary: str = "",
    improvement_suggestions: list[str] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build the final evaluation result from scores, details, and findings."""
    safe_scores: dict[str, int] = (
        {str(k): _coerce_result_score(v) for k, v in scores.items()}
        if isinstance(scores, dict)
        else {}
    )

    if findings is not None:
        if isinstance(findings, list):
            normalized_findings = [
                nf for nf in (_normalize_finding(item) for item in findings) if nf is not None
            ]
            # Fall back to score-based findings if a non-empty list held only non-dicts.
            findings = normalized_findings if (normalized_findings or not findings) else None
        else:
            findings = None

    if findings is None:
        findings = []
        for criterion, score in safe_scores.items():
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
    effective_weights = weights or CRITERION_WEIGHTS
    overall_score = 0
    for criterion, weight in effective_weights.items():
        overall_score += safe_scores.get(criterion, 0) * weight
    overall_score = round(overall_score)

    passed = overall_score >= PASS_THRESHOLD and not any(
        f.get("severity") == "MAJOR" for f in findings if isinstance(f, dict)
    )

    return {
        "status": "success",
        "passed": passed,
        "scores": safe_scores,
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
    evaluation_request: str = "",
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
    state = getattr(tool_context, "state", None) if tool_context else None
    if state is not None and not hasattr(state, "get"):
        state = None

    # --- Parse structured input (mirror marathon pattern) ---
    if isinstance(evaluation_request, dict):
        raw_intent = evaluation_request.get("user_intent")
        user_intent = str(raw_intent if raw_intent is not None else "Leadership scenario evaluation")
        raw_plan = evaluation_request.get("proposed_plan")
        proposed_plan = str(raw_plan if raw_plan is not None else _strict_json_dumps(evaluation_request))
    else:
        try:
            request_data = json.loads(evaluation_request) if evaluation_request else {}
            if isinstance(request_data, dict):
                raw_intent = request_data.get("user_intent")
                user_intent = str(raw_intent if raw_intent is not None else "Leadership scenario evaluation")
                proposed_plan = str(
                    request_data.get("proposed_plan")
                    or evaluation_request
                    or "Leadership scenario consensus pathway and stakeholder alignment plan"
                )
            else:
                user_intent = "Leadership scenario evaluation"
                proposed_plan = str(evaluation_request)
        except (json.JSONDecodeError, TypeError):
            # Graceful fallback: treat entire input as plan text
            user_intent = "Leadership scenario evaluation"
            proposed_plan = str(evaluation_request or "Leadership scenario consensus pathway and stakeholder alignment plan")

    local_active = is_local_gemma_active()

    # Keep CPU-bound local Gemma prompts compact so prompt processing and
    # single-pass JSON scoring stay responsive even for large user prompts.
    if local_active and len(proposed_plan) > 900:
        proposed_plan = proposed_plan[:900] + "\n[... truncated for local evaluator budget ...]"

    # --- Enrich from session state (the key missing piece) ---
    if state is not None:
        scenario = state.get("leadership_scenario")
        if isinstance(scenario, dict):
            scenario_meta = {
                "name": scenario.get("name", "Unknown"),
                "scenario_type": scenario.get("scenario_type", "unknown"),
                "stakeholder_count": scenario.get("stakeholder_count", 0),
                "complexity": scenario.get("complexity", "unknown"),
                "summary": scenario.get("summary", ""),
            }
            proposed_plan = f"{proposed_plan}\n\n[Scenario Metadata (from session state)]: {_strict_json_dumps(scenario_meta)}"

        pathway = state.get("consensus_pathway_active")
        if isinstance(pathway, dict):
            raw_milestones = pathway.get("milestones")
            raw_waypoints = pathway.get("waypoints")
            if not isinstance(raw_waypoints, list):
                raw_waypoints = pathway.get("pathway")
            pathway_summary = {
                "persona": pathway.get("persona", "pragmatist"),
                "milestones": len(raw_milestones) if isinstance(raw_milestones, list) else 0,
                "waypoints": len(raw_waypoints) if isinstance(raw_waypoints, list) else 0,
            }
            proposed_plan = (
                f"{proposed_plan}\n\n[Consensus Pathway (from session state)]: {_strict_json_dumps(pathway_summary)}"
            )

        stakeholders = state.get("stakeholder_map")
        if isinstance(stakeholders, (dict, list)):
            # Cap at ~1200 chars in cloud mode (~350 in local CPU mode): nothing
            # about evaluation benefits from a giant stakeholder blob.
            sh_cap = 350 if local_active else 1200
            proposed_plan = (
                f"{proposed_plan}\n\n[Stakeholder Map (from session state)]: {_strict_json_dumps(stakeholders)[:sh_cap]}"
            )

    # --- Cap total prompt input to protect the output-token budget ---
    _PROMPT_CHAR_CAP = 1500 if local_active else 8000
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
    prompt = (
        LOCAL_EVALUATOR_PROMPT.format(plan_text=proposed_plan)
        if local_active
        else EVALUATOR_PROMPT.format(
            criteria_block=criteria_block,
            plan_text=proposed_plan,
        )
    )

    response = None
    raw_text: Any = None
    if local_active:
        try:
            raw_text = await generate_local_json_async(
                prompt,
                temperature=0.3,
                max_tokens=200,
                timeout=480.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "evaluate_leadership_plan: generate_local_json_async call failed (%s, input_hash=%s)",
                exc,
                input_hash,
                exc_info=True,
            )
            result = _error_result(
                reason=f"local_llm_eval_{type(exc).__name__}",
                eval_method="local_llm_eval_error",
                input_hash=input_hash,
                title="Local LLM evaluator call failed",
                description=(
                    f"Local LLM evaluation request failed ({type(exc).__name__}: {exc}). "
                    "Scenario was NOT scored."
                ),
                suggestion="Ensure local Ollama / Gemma service is running and retry evaluation.",
            )
            if state is not None:
                state["leadership_evaluation"] = result
                state["evaluation_result"] = result
            return result
    else:
        client = _get_client()
        try:
            response = await client.aio.models.generate_content(
                model=EVALUATOR_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.5,  # Higher temperature for better score variance (Marathon uses Vertex AI Eval internally)
                    max_output_tokens=4096,
                    thinking_config=types.ThinkingConfig(thinking_budget=1024),
                    response_mime_type="application/json",
                ),
            )
            raw_text = response.text
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "evaluate_leadership_plan: generate_content call failed (%s, input_hash=%s)",
                exc,
                input_hash,
                exc_info=True,
            )
            result = _error_result(
                reason=f"llm_eval_{type(exc).__name__}",
                eval_method="llm_eval_error",
                input_hash=input_hash,
                title="LLM evaluator call failed",
                description=f"Cloud LLM evaluation request failed ({type(exc).__name__}: {exc}). Scenario was NOT scored.",
                suggestion="Verify evaluator model/API availability and retry evaluation.",
            )
            if state is not None:
                state["leadership_evaluation"] = result
                state["evaluation_result"] = result
            return result

    if not isinstance(raw_text, str) or not raw_text.strip():
        reason = "empty_local_response" if local_active else _extract_response_reason(response)
        logger.error(
            "evaluate_leadership_plan: LLM returned empty text (reason=%s, input_hash=%s).",
            reason,
            input_hash,
        )
        result = _empty_response_result(reason=reason, input_hash=input_hash)
        if state is not None:
            state["leadership_evaluation"] = result
            state["evaluation_result"] = result  # Marathon compatibility
        return result

    result_text = raw_text.strip()

    # Parse JSON response
    raw_result: Any = None
    parse_error: str | None = None
    try:
        raw_result = json.loads(result_text)
    except json.JSONDecodeError as exc:
        # Try extracting JSON from markdown code block
        if "```json" in result_text:
            try:
                json_str = result_text.split("```json")[1].split("```")[0].strip()
                raw_result = json.loads(json_str)
            except (IndexError, json.JSONDecodeError) as fence_exc:
                parse_error = f"invalid_fenced_json:{fence_exc}"
        elif "```" in result_text:
            try:
                json_str = result_text.split("```")[1].split("```")[0].strip()
                raw_result = json.loads(json_str)
            except (IndexError, json.JSONDecodeError) as fence_exc:
                parse_error = f"invalid_codeblock_json:{fence_exc}"
        else:
            parse_error = f"invalid_json:{exc}"

    if parse_error is not None or not isinstance(raw_result, dict):
        reason = parse_error or f"non_dict_json:{type(raw_result).__name__}"
        logger.error(
            "evaluate_leadership_plan: invalid LLM JSON output (reason=%s, input_hash=%s).",
            reason,
            input_hash,
        )
        result = _error_result(
            reason=reason,
            eval_method="invalid_llm_json",
            input_hash=input_hash,
            title="LLM evaluator returned invalid JSON",
            description=f"The LLM evaluator response could not be parsed as a JSON object ({reason}). Scenario was NOT scored.",
            suggestion="Retry evaluation so the evaluator emits valid JSON matching the scoring schema.",
        )
        if state is not None:
            state["leadership_evaluation"] = result
            state["evaluation_result"] = result
        return result

    # Extract scores and build proper result via _build_result
    raw_scores_obj = raw_result.get("scores")
    raw_scores: dict[str, Any] = raw_scores_obj if isinstance(raw_scores_obj, dict) else {}
    llm_scores: dict[str, int] = {}
    llm_findings = raw_result.get("findings", [])
    llm_summary = raw_result.get("summary", "")

    # Normalize and validate strictly against EVALUATION_CRITERIA (drop unexpected extra keys)
    for c in EVALUATION_CRITERIA:
        if c in raw_scores:
            llm_scores[c] = _normalize_score(raw_scores[c])
        elif c in raw_result:
            llm_scores[c] = _normalize_score(raw_result[c])
        else:
            logger.warning("LLM evaluator missing criterion %r — defaulting to 0", c)
            llm_scores[c] = 0

    details = {c: f"LLM score: {llm_scores.get(c, '?')}" for c in EVALUATION_CRITERIA}

    # Extract inline suggestions when single-pass local evaluation already provided them
    inline_suggestions: list[str] = []
    raw_sugg_obj = raw_result.get("suggestions") or raw_result.get("improvement_suggestions")
    if isinstance(raw_sugg_obj, str) and raw_sugg_obj.strip():
        inline_suggestions.append(raw_sugg_obj.strip())
    elif isinstance(raw_sugg_obj, list):
        for item in raw_sugg_obj:
            if isinstance(item, str) and item.strip():
                inline_suggestions.append(item.strip())
            elif isinstance(item, dict):
                t = str(item.get("title") or "").strip()
                d = str(item.get("description") or item.get("suggestion") or item.get("text") or "").strip()
                if t and d:
                    inline_suggestions.append(f"{t}: {d}")
                elif d or t:
                    inline_suggestions.append(d or t)

    # --- Generate actionable improvement suggestions ---
    if local_active and inline_suggestions and isinstance(llm_summary, str) and llm_summary.strip():
        suggestions = inline_suggestions
        feedback_summary = llm_summary.strip()
    else:
        try:
            suggestions, feedback_summary = await _generate_feedback(
                scores=llm_scores,
                details=details,
                user_intent=user_intent,
                proposed_plan=proposed_plan,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "evaluate_leadership_plan: _generate_feedback failed (input_hash=%s): %s",
                input_hash,
                exc,
                exc_info=True,
            )
            result = _error_result(
                reason=(
                    f"local_llm_feedback_{type(exc).__name__}"
                    if local_active
                    else f"llm_feedback_{type(exc).__name__}"
                ),
                eval_method="local_llm_feedback_error" if local_active else "llm_feedback_error",
                input_hash=input_hash,
                title="LLM feedback generation failed",
                description=(
                    f"{'Local' if local_active else 'Cloud'} LLM feedback generation failed "
                    f"({type(exc).__name__}: {exc}). Scenario evaluation could not be completed."
                ),
                suggestion=(
                    "Ensure local Ollama / Gemma service is running and retry evaluation."
                    if local_active
                    else "Verify evaluator model/API availability and retry evaluation."
                ),
            )
            if state is not None:
                state["leadership_evaluation"] = result
                state["evaluation_result"] = result
            return result

    result = _build_result(
        scores=llm_scores,
        details=details,
        eval_method="local_gemma_eval" if local_active else "llm_judge",
        findings=llm_findings if isinstance(llm_findings, list) and llm_findings else None,
        summary=feedback_summary or (llm_summary if isinstance(llm_summary, str) else "") or "Evaluation completed.",
        improvement_suggestions=suggestions,
    )

    # Store evaluation in session state for downstream tools
    if state is not None:
        state["leadership_evaluation"] = result
        state["evaluation_result"] = result  # Marathon compatibility
        if isinstance(state.get("leadership_scenario"), dict):
            await _auto_store_scenario_if_configured(tool_context)

    result["message"] = (
        'Evaluation complete and saved to state. NEXT STEP: Call validate_and_emit_a2ui(payload="dashboard")'
    )

    logger.info(
        "evaluate_leadership_plan: overall_score=%s, method=%s, findings=%d, input_hash=%s",
        result.get("overall_score", "?"),
        result.get("eval_method", "?"),
        len(result.get("findings", [])),
        input_hash,
    )

    return result
