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

"""Simulation after-action report tool (Artifact 3, MiroFish-style).

This is the **after-action report** layer of the
leadership_planner_with_memory workflow — the missing layer between a
multi-tick simulation timeline (Artifact 2) and a stakeholder
interview (future Artifact 4). Modelled after MiroFish's InsightForge
ReACT Report Agent that wraps an OASIS simulation run with a written
report, but condensed into a single agent-supplied structured payload:

* The agent has already seen the graph (Step 0), personas (Step 0.25),
  reactions (Step 0.5), and timeline (Step 0.75) by this point.
* The report distills that material into a one-line headline + outcome
  verdict + executive summary + 2–6 key findings + optional per-
  stakeholder journey summary + 0–5 recommendations.

Design choices (locked 2026-06-28 by the operator):

* **Same "agent IS the report-builder" pattern.** No nested LLM call
  inside the tool. The agent reads `state["simulation_timeline"]` +
  `state["stakeholder_personas"]` + the user's plan and supplies the
  structured report as one JSON payload. The tool only validates +
  stashes.
* **No persistence yet.** Report lives in `tool_context.state`.
  Postgres rows come later when the user starts wanting to look back
  across past simulations.
* **Invented data is allowed here** — simulation layer. Default
  `inferred: true` and the UI tags the card as a synthetic report.
* **Stakeholder ids match personas when available.** When personas
  are in state, any `stakeholder_summaries[].stakeholder_id` must
  appear in the persona set; unknown ids are rejected. (No "missing-
  coverage" check — unlike the timeline, the report is allowed to
  call out only the stakeholders that matter to the narrative.)

Downstream consumers:
* Artifact 4's interview tool will read this report + the personas to
  ground its Q&A on the report's findings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext


logger = logging.getLogger(__name__)

# Allowed outcome verdicts.
ALLOWED_OUTCOMES: frozenset[str] = frozenset({"success", "partial", "contested", "failed"})

# Allowed finding severities.
ALLOWED_SEVERITIES: frozenset[str] = frozenset({"high", "medium", "low"})

# Allowed recommendation priorities. p0 = urgent / blocking, p2 = nice-to-have.
ALLOWED_PRIORITIES: frozenset[str] = frozenset({"p0", "p1", "p2"})

# Allowed stances — MUST stay aligned with the other Step-0.x tools.
ALLOWED_STANCES: frozenset[str] = frozenset({"supportive", "neutral", "resistant"})

# Sanity ceilings.
MIN_FINDINGS = 2
MAX_FINDINGS = 6
MAX_RECOMMENDATIONS = 5
MAX_STAKEHOLDER_SUMMARIES = 20

MAX_HEADLINE_CHARS = 200
MAX_EXEC_SUMMARY_CHARS = 800
MAX_FINDING_TITLE_CHARS = 100
MAX_FINDING_BODY_CHARS = 400
MAX_REC_TITLE_CHARS = 100
MAX_REC_BODY_CHARS = 300
MAX_JOURNEY_CHARS = 200
MAX_RATIONALE_CHARS = 500

# Session-state keys.
STATE_KEY = "simulation_report"
PERSONAS_STATE_KEY = "stakeholder_personas"


def _err(message: str, *, field: str = "report_json") -> dict[str, Any]:
    """Build a structured error response."""
    return {
        "status": "error",
        "field": field,
        "message": message,
    }


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a structured success response."""
    return {
        "status": "success",
        "simulation_report": payload,
    }


def _validate_short_string(label: str, value: Any, max_chars: int) -> tuple[str | None, str | None]:
    """Validate a non-empty short string. Returns (cleaned, error)."""
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty string"
    s = value.strip()
    if len(s) > max_chars:
        return None, f"{label} length {len(s)} > {max_chars} chars"
    return s, None


def _validate_finding(idx: int, raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one key-finding entry."""
    if not isinstance(raw, dict):
        return None, f"key_findings[{idx}] must be an object"

    title, err = _validate_short_string(f"key_findings[{idx}].title", raw.get("title"), MAX_FINDING_TITLE_CHARS)
    if err:
        return None, err
    body, err = _validate_short_string(f"key_findings[{idx}].body", raw.get("body"), MAX_FINDING_BODY_CHARS)
    if err:
        return None, err

    severity = raw.get("severity")
    if severity not in ALLOWED_SEVERITIES:
        return None, (f"key_findings[{idx}].severity={severity!r} not in {sorted(ALLOWED_SEVERITIES)}")

    return {"title": title, "body": body, "severity": severity}, None


def _validate_stakeholder_summary(idx: int, raw: Any, known_ids: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one per-stakeholder summary entry."""
    if not isinstance(raw, dict):
        return None, f"stakeholder_summaries[{idx}] must be an object"

    sid = raw.get("stakeholder_id")
    if not isinstance(sid, str) or not sid.strip():
        return None, (f"stakeholder_summaries[{idx}].stakeholder_id must be a non-empty string")
    sid_clean = sid.strip()
    if known_ids and sid_clean not in known_ids:
        return None, (
            f"stakeholder_summaries[{idx}].stakeholder_id={sid_clean!r} "
            f"does not match any persona in state['{PERSONAS_STATE_KEY}']"
        )

    final_stance = raw.get("final_stance")
    if final_stance not in ALLOWED_STANCES:
        return None, (f"stakeholder_summaries[{idx}].final_stance={final_stance!r} not in {sorted(ALLOWED_STANCES)}")

    journey, err = _validate_short_string(
        f"stakeholder_summaries[{idx}].journey",
        raw.get("journey"),
        MAX_JOURNEY_CHARS,
    )
    if err:
        return None, err

    return {
        "stakeholder_id": sid_clean,
        "final_stance": final_stance,
        "journey": journey,
    }, None


def _validate_recommendation(
    idx: int,
    raw: Any,
    known_ids: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one recommendation entry.

    ``known_ids`` is the persona id set resolved from
    ``state[PERSONAS_STATE_KEY]``. When non-empty, an optional
    ``interview_with`` link must point to one of those ids so the
    frontend's "▶ Interview" jump button never targets a card that
    will not exist on screen.
    """
    if not isinstance(raw, dict):
        return None, f"recommendations[{idx}] must be an object"

    title, err = _validate_short_string(
        f"recommendations[{idx}].title",
        raw.get("title"),
        MAX_REC_TITLE_CHARS,
    )
    if err:
        return None, err
    body, err = _validate_short_string(
        f"recommendations[{idx}].body",
        raw.get("body"),
        MAX_REC_BODY_CHARS,
    )
    if err:
        return None, err

    priority = raw.get("priority")
    if priority not in ALLOWED_PRIORITIES:
        return None, (f"recommendations[{idx}].priority={priority!r} not in {sorted(ALLOWED_PRIORITIES)}")

    out: dict[str, Any] = {"title": title, "body": body, "priority": priority}

    # Optional jump-to-interview link. The agent attaches this when a
    # recommendation maps to a specific stakeholder whose interview
    # card (Artifact 4) will render below the report — the UI shows
    # a "▶ Interview" button that scrolls the user to that card.
    interview_with_raw = raw.get("interview_with")
    if interview_with_raw is not None:
        if not isinstance(interview_with_raw, str) or not interview_with_raw.strip():
            return None, (
                f"recommendations[{idx}].interview_with must be a non-empty "
                f"string when present"
            )
        iw = interview_with_raw.strip()
        if known_ids and iw not in known_ids:
            return None, (
                f"recommendations[{idx}].interview_with={iw!r} "
                f"does not match any persona in state['{PERSONAS_STATE_KEY}']"
            )
        out["interview_with"] = iw

    return out, None


async def emit_simulation_report(
    report_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate a simulation after-action report and stash it in session state.

    This is **Step 0.9** of the workflow, sitting between the Step-0.75
    multi-tick simulation timeline and the future interview tool. The
    agent supplies a structured after-action report distilling the
    timeline + reactions + personas into a one-line headline, an
    outcome verdict, an executive summary, 2–6 key findings, optional
    per-stakeholder journeys, and 0–5 actionable recommendations.

    Args:
        report_json: A JSON string with shape
            ``{"headline": "≤200 chars",
              "outcome": "success|partial|contested|failed",
              "executive_summary": "≤800 chars",
              "key_findings": [
                {"title": "≤100", "body": "≤400",
                 "severity": "high|medium|low"},
                ...2 to 6 entries...
              ],
              "stakeholder_summaries"?: [
                {"stakeholder_id", "final_stance":
                  "supportive|neutral|resistant",
                 "journey": "≤200 chars"}, ...
              ],
              "recommendations"?: [
                {"title": "≤100", "body": "≤300",
                 "priority": "p0|p1|p2",
                 "interview_with"?: "persona_id"}, ...up to 5...
              ],
              "rationale"?: "≤500",
              "inferred"?: true}``.
            Report is invented synthetic analysis belonging to the
            simulation layer.
        tool_context: ADK tool context. The validated payload is stored
            at ``tool_context.state["simulation_report"]``.

    Returns:
        On success: ``{"status": "success", "simulation_report": {...}}``.
        On failure: ``{"status": "error", "field": "...", "message": "..."}``.
    """
    # 1. Parse the JSON envelope (lenient: accept a dict directly).
    if isinstance(report_json, dict):
        data = report_json
    else:
        try:
            data = json.loads(report_json)
        except (TypeError, ValueError) as exc:
            return _err(f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        return _err("payload must be a JSON object")

    # 2. Required top-level fields.
    headline, err = _validate_short_string("headline", data.get("headline"), MAX_HEADLINE_CHARS)
    if err:
        return _err(err, field="headline")

    outcome = data.get("outcome")
    if outcome not in ALLOWED_OUTCOMES:
        return _err(
            f"outcome={outcome!r} not in {sorted(ALLOWED_OUTCOMES)}",
            field="outcome",
        )

    exec_summary, err = _validate_short_string(
        "executive_summary",
        data.get("executive_summary"),
        MAX_EXEC_SUMMARY_CHARS,
    )
    if err:
        return _err(err, field="executive_summary")

    # 3. Key findings (2-6 required).
    findings_raw = data.get("key_findings")
    if not isinstance(findings_raw, list):
        return _err("key_findings must be an array", field="key_findings")
    if len(findings_raw) < MIN_FINDINGS or len(findings_raw) > MAX_FINDINGS:
        return _err(
            f"key_findings count {len(findings_raw)} outside [{MIN_FINDINGS}, {MAX_FINDINGS}]",
            field="key_findings",
        )
    findings: list[dict[str, Any]] = []
    for i, raw in enumerate(findings_raw):
        normalized, err = _validate_finding(i, raw)
        if err:
            return _err(err, field="key_findings")
        assert normalized is not None
        findings.append(normalized)

    # 4. Resolve known persona ids (permissive when absent).
    known_ids: set[str] = set()
    try:
        personas_payload = tool_context.state.get(PERSONAS_STATE_KEY)
    except (AttributeError, TypeError):
        personas_payload = None
    if isinstance(personas_payload, dict):
        personas_list = personas_payload.get("personas")
        if isinstance(personas_list, list):
            for p in personas_list:
                if isinstance(p, dict):
                    pid = p.get("stakeholder_id")
                    if isinstance(pid, str) and pid.strip():
                        known_ids.add(pid.strip())

    # 5. Optional stakeholder summaries.
    summaries_raw = data.get("stakeholder_summaries")
    summaries: list[dict[str, Any]] = []
    if summaries_raw is not None:
        if not isinstance(summaries_raw, list):
            return _err(
                "stakeholder_summaries must be an array",
                field="stakeholder_summaries",
            )
        if len(summaries_raw) > MAX_STAKEHOLDER_SUMMARIES:
            return _err(
                f"too many stakeholder_summaries ({len(summaries_raw)} > {MAX_STAKEHOLDER_SUMMARIES})",
                field="stakeholder_summaries",
            )
        seen_sids: set[str] = set()
        for i, raw in enumerate(summaries_raw):
            normalized, err = _validate_stakeholder_summary(i, raw, known_ids)
            if err:
                return _err(err, field="stakeholder_summaries")
            assert normalized is not None
            sid = normalized["stakeholder_id"]
            if sid in seen_sids:
                return _err(
                    f"stakeholder_summaries[{i}].stakeholder_id={sid!r} duplicates an earlier entry",
                    field="stakeholder_summaries",
                )
            seen_sids.add(sid)
            summaries.append(normalized)

    # 6. Optional recommendations.
    recs_raw = data.get("recommendations")
    recs: list[dict[str, Any]] = []
    if recs_raw is not None:
        if not isinstance(recs_raw, list):
            return _err("recommendations must be an array", field="recommendations")
        if len(recs_raw) > MAX_RECOMMENDATIONS:
            return _err(
                f"too many recommendations ({len(recs_raw)} > {MAX_RECOMMENDATIONS})",
                field="recommendations",
            )
        for i, raw in enumerate(recs_raw):
            normalized, err = _validate_recommendation(i, raw, known_ids)
            if err:
                return _err(err, field="recommendations")
            assert normalized is not None
            recs.append(normalized)


    # 8. Optional rationale + inferred flag.
    rationale = data.get("rationale")
    rationale_clean: str | None = None
    if isinstance(rationale, str) and rationale.strip():
        rationale_clean = rationale.strip()[:MAX_RATIONALE_CHARS]

    inferred = data.get("inferred", True)
    if not isinstance(inferred, bool):
        return _err("inferred must be a boolean", field="inferred")

    payload: dict[str, Any] = {
        "headline": headline,
        "outcome": outcome,
        "executive_summary": exec_summary,
        "key_findings": findings,
        "inferred": inferred,
    }
    if summaries:
        payload["stakeholder_summaries"] = summaries
    if recs:
        payload["recommendations"] = recs
    if rationale_clean:
        payload["rationale"] = rationale_clean
    if "citations" in data:
        payload["citations"] = data["citations"]

    # 9. Stash for downstream interview tool.
    try:
        tool_context.state[STATE_KEY] = payload
    except (AttributeError, TypeError):
        logger.warning(
            "emit_simulation_report: could not write %s to session state",
            STATE_KEY,
        )

    return _ok(payload)
