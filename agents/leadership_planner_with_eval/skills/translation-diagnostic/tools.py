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

"""Translation Diagnostic tool for Leadership Planner with Eval.

Analyses the runner/pitcher's rollout communication at sentiment inflection
points and identifies where the leader's System language collides with each
stakeholder's Lifeworld.

Same lightweight return-dict pattern as ``emit_stakeholder_reactions``:
the ADK plugin auto-broadcasts the tool_end event; no manual
``emit_gateway_message`` call needed.
"""

import json
import logging
import os
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import Client, types

from agents.utils.retry import resilient_http_options

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GenAI client (lazy singleton — same pattern as evaluator/tools.py)
# ---------------------------------------------------------------------------

_client: Client | None = None
_MODEL = "gemini-3.5-flash-lite"


def _get_client() -> Client:
    """Lazy-singleton GenAI client with project config and retry options."""
    global _client
    if _client is None:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
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
# Inflection-point detection
# ---------------------------------------------------------------------------

def _find_inflection_points(
    echo_voice_log: list[dict[str, Any]],
    top_n: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Identify the ticks with the largest sentiment swings per stakeholder.

    Returns a dict mapping role → list of log entries at inflection ticks,
    sorted by absolute delta (largest first), capped at *top_n* per role.
    """
    # Group by role, ordered by tick.
    by_role: dict[str, list[dict[str, Any]]] = {}
    for entry in echo_voice_log:
        role = entry.get("role", "")
        if not role:
            continue
        by_role.setdefault(role, []).append(entry)

    inflections: dict[str, list[dict[str, Any]]] = {}
    for role, entries in by_role.items():
        entries.sort(key=lambda e: e.get("tick", 0))
        deltas: list[tuple[float, dict[str, Any]]] = []
        for i in range(1, len(entries)):
            prev_s = entries[i - 1].get("sentiment", 0.0)
            curr_s = entries[i].get("sentiment", 0.0)
            delta = abs(curr_s - prev_s)
            if delta > 0.01:  # ignore noise
                deltas.append((delta, entries[i]))
        # Sort by largest delta first, take top_n.
        deltas.sort(key=lambda t: t[0], reverse=True)
        if deltas:
            inflections[role] = [entry for _, entry in deltas[:top_n]]

    return inflections


# ---------------------------------------------------------------------------
# LLM analysis prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a communication analyst specialising in Habermas's System vs. Lifeworld
framework applied to organisational change leadership.

You will receive:
- A leader's decision text (the "pitch") from specific simulation ticks.
- The stakeholder's role, persona description, and inner thoughts at those ticks.
- The sentiment shift that occurred.

For EACH stakeholder at EACH inflection tick, identify:
1. **trigger_phrase**: The exact phrase(s) from the leader's decision_text that
   the stakeholder is likely decoding differently than the leader intended.
   Quote the phrase verbatim from the decision_text.
2. **leader_intent**: What the leader meant by that phrase (1 sentence).
3. **stakeholder_decoded_as**: How the stakeholder experienced/interpreted it
   through their Lifeworld lens (1 sentence).
4. **gap_type**: A concise label for the translation gap using the format
   "X → experienced as Y" (e.g., "efficiency → dispensability",
   "modernization → obsolescence", "alignment → conformity").
5. **severity**: "high", "medium", or "low" based on the sentiment delta.
6. **suggested_reframing**: An alternative phrasing that preserves the leader's
   strategic intent while bridging the translation gap (1-2 sentences).

Respond with ONLY valid JSON matching this schema:
{
  "gaps": [
    {
      "stakeholder_id": "string",
      "stakeholder_label": "string",
      "trigger_phrases": [
        {
          "phrase": "string (verbatim from decision_text)",
          "tick": number,
          "leader_intent": "string",
          "stakeholder_decoded_as": "string",
          "gap_type": "string (X → experienced as Y)",
          "severity": "high" | "medium" | "low"
        }
      ],
      "suggested_reframing": "string",
      "overall_gap_severity": "high" | "medium" | "low"
    }
  ],
  "summary": "string (2-3 sentence overall assessment)"
}
"""


def _build_analysis_prompt(
    inflections: dict[str, list[dict[str, Any]]],
    decision_log: list[dict[str, Any]],
    stakeholder_map: list[dict[str, Any]] | None,
) -> str:
    """Build the user prompt for the LLM analysis."""
    # Index decisions by tick for lookup.
    decisions_by_tick: dict[int, str] = {}
    for entry in decision_log:
        tick = entry.get("tick")
        text = entry.get("decision_text", "")
        if tick is not None and text:
            decisions_by_tick[int(tick)] = text

    # Index stakeholder personas by role.
    personas_by_role: dict[str, str] = {}
    if stakeholder_map:
        for s in stakeholder_map:
            role = s.get("role", s.get("id", ""))
            persona = s.get("persona", s.get("description", ""))
            if role and persona:
                personas_by_role[role] = persona

    sections: list[str] = []
    for role, entries in inflections.items():
        persona_desc = personas_by_role.get(role, "No persona description available.")
        lines = [f"## Stakeholder: {role}", f"Persona: {persona_desc}", ""]
        for entry in entries:
            tick = entry.get("tick", "?")
            sentiment = entry.get("sentiment", "?")
            stance = entry.get("stance", "?")
            thought = entry.get("inner_thought", "")
            decision_text = decisions_by_tick.get(int(tick), "(decision text not available)")
            lines.append(f"### Tick {tick}")
            lines.append(f"- Sentiment: {sentiment}, Stance: {stance}")
            lines.append(f"- Inner thought: \"{thought}\"")
            lines.append(f"- Leader's decision text: \"{decision_text}\"")
            lines.append("")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Heuristic fallback (offline / test environments)
# ---------------------------------------------------------------------------

def _heuristic_fallback(
    inflections: dict[str, list[dict[str, Any]]],
    decision_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic fallback when the LLM is unavailable."""
    decisions_by_tick: dict[int, str] = {}
    for entry in decision_log:
        tick = entry.get("tick")
        text = entry.get("decision_text", "")
        if tick is not None and text:
            decisions_by_tick[int(tick)] = text

    gaps: list[dict[str, Any]] = []
    for role, entries in inflections.items():
        trigger_phrases: list[dict[str, Any]] = []
        for entry in entries:
            tick = entry.get("tick", 0)
            thought = entry.get("inner_thought", "")
            decision_text = decisions_by_tick.get(int(tick), "")
            # Simple heuristic: use inner_thought as the decoded meaning.
            trigger_phrases.append({
                "phrase": decision_text[:80] + ("..." if len(decision_text) > 80 else ""),
                "tick": tick,
                "leader_intent": "Strategic communication (heuristic — LLM unavailable)",
                "stakeholder_decoded_as": thought or "Unknown reaction",
                "gap_type": "system language → lifeworld friction",
                "severity": "medium",
            })
        gaps.append({
            "stakeholder_id": role,
            "stakeholder_label": role.replace("_", " ").title(),
            "trigger_phrases": trigger_phrases,
            "suggested_reframing": "(LLM unavailable — reframing requires manual analysis)",
            "overall_gap_severity": "medium",
        })

    return {
        "gaps": gaps,
        "summary": (
            f"Heuristic analysis identified inflection points for "
            f"{len(gaps)} stakeholder(s). LLM-powered deep analysis was "
            f"unavailable; results are approximate."
        ),
    }


_TIER_BEHAVIORAL_DEFAULTS: dict[int, dict[str, str]] = {
    1: {
        "decision_style": "data-driven",
        "communication": "direct",
        "risk_tolerance": "moderate",
    },
    2: {
        "decision_style": "consensus-seeking",
        "communication": "diplomatic",
        "risk_tolerance": "low",
    },
    3: {
        "decision_style": "pragmatic",
        "communication": "structured",
        "risk_tolerance": "moderate",
    },
}


def _default_behavioral_for_tier(tier: int) -> dict[str, str]:
    """Return tier-appropriate default behavioral patterns when not enriched."""
    return dict(_TIER_BEHAVIORAL_DEFAULTS.get(tier, _TIER_BEHAVIORAL_DEFAULTS[3]))


def _resolve_stakeholder_map(tool_context: ToolContext) -> list[dict[str, Any]]:
    """Resolve stakeholder personas from state, falling back to leadership_scenario."""
    try:
        stakeholder_map = tool_context.state.get("stakeholder_map", None)
    except (AttributeError, TypeError):
        stakeholder_map = None

    if isinstance(stakeholder_map, str):
        try:
            stakeholder_map = json.loads(stakeholder_map)
        except (json.JSONDecodeError, TypeError):
            stakeholder_map = None

    if isinstance(stakeholder_map, list):
        valid = [s for s in stakeholder_map if isinstance(s, dict)]
        if valid:
            return valid

    try:
        scenario = tool_context.state.get("leadership_scenario", {})
    except (AttributeError, TypeError):
        scenario = {}

    if isinstance(scenario, str):
        try:
            scenario = json.loads(scenario)
        except (json.JSONDecodeError, TypeError):
            scenario = {}

    if isinstance(scenario, dict):
        fallback = scenario.get("stakeholders") or scenario.get("echoes") or []
        if isinstance(fallback, str):
            try:
                fallback = json.loads(fallback)
            except (json.JSONDecodeError, TypeError):
                fallback = []
        if isinstance(fallback, list):
            return [s for s in fallback if isinstance(s, dict)]

    return []


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------

async def emit_translation_diagnostic(
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Analyse the leader's communication for System–Lifeworld translation gaps.

    Reads ``echo_voice_log`` and ``decision_log`` from session state,
    identifies sentiment inflection points per stakeholder, and uses Gemini
    to diagnose where the leader's System language collided with each
    stakeholder's Lifeworld.

    Returns:
        On success: ``{"status": "success", "translation_diagnostic": {...}}``.
        On no data: ``{"status": "skipped", "reason": "..."}``.
    """
    # 1. Read state data.
    try:
        echo_voice_log = tool_context.state.get("echo_voice_log", [])
        decision_log = tool_context.state.get("decision_log", [])
    except (AttributeError, TypeError):
        echo_voice_log = []
        decision_log = []

    if not echo_voice_log:
        return {"status": "skipped", "reason": "No echo_voice_log in session state."}
    if not decision_log:
        return {"status": "skipped", "reason": "No decision_log in session state."}

    # 2. Optional: stakeholder personas for richer context (with scenario fallback).
    stakeholder_map = _resolve_stakeholder_map(tool_context) or None

    # 3. Find inflection points (top 3 per stakeholder).
    inflections = _find_inflection_points(echo_voice_log, top_n=3)
    if not inflections:
        return {
            "status": "skipped",
            "reason": "No significant sentiment shifts detected in echo_voice_log.",
        }

    # 4. Build analysis prompt and call Gemini.
    user_prompt = _build_analysis_prompt(inflections, decision_log, stakeholder_map)
    diagnostic: dict[str, Any] | None = None
    eval_method = "llm"

    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=[user_prompt],
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.3,
            ),
        )
        raw_text = response.text or ""
        # Strip markdown fences if present.
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
        diagnostic = json.loads(raw_text)
    except Exception:
        logger.warning(
            "emit_translation_diagnostic: LLM call failed, using heuristic fallback",
            exc_info=True,
        )
        eval_method = "heuristic"

    if diagnostic is None:
        diagnostic = _heuristic_fallback(inflections, decision_log)

    # 5. Enrich with metadata.
    diagnostic["inferred"] = True
    diagnostic["eval_method"] = eval_method

    # 6. Stash in state for downstream consumers.
    try:
        tool_context.state["translation_diagnostic"] = diagnostic
    except (AttributeError, TypeError):
        logger.warning(
            "emit_translation_diagnostic: could not write to session state"
        )

    logger.info(
        "emit_translation_diagnostic: %d stakeholder(s), method=%s",
        len(diagnostic.get("gaps", [])),
        eval_method,
    )
    return {"status": "success", "translation_diagnostic": diagnostic}


# ---------------------------------------------------------------------------
# Stakeholder Psychology — deterministic analysis (no LLM call)
# ---------------------------------------------------------------------------

def _compute_sentiment_trajectory(
    echo_voice_log: list[dict[str, Any]],
    role: str,
) -> dict[str, Any]:
    """Compute sentiment trajectory stats for a single stakeholder."""
    entries = [e for e in echo_voice_log if e.get("role") == role]
    entries.sort(key=lambda e: e.get("tick", 0))
    if not entries:
        return {
            "initial": 0.0, "final": 0.0, "min": 0.0, "max": 0.0,
            "max_delta_tick": 0, "trend": "flat",
        }

    sentiments = [e.get("sentiment", 0.0) for e in entries]
    initial = sentiments[0]
    final = sentiments[-1]

    # Find the tick with the largest single-step delta.
    max_delta = 0.0
    max_delta_tick = entries[0].get("tick", 0)
    for i in range(1, len(sentiments)):
        delta = abs(sentiments[i] - sentiments[i - 1])
        if delta > max_delta:
            max_delta = delta
            max_delta_tick = entries[i].get("tick", 0)

    diff = final - initial
    if diff > 0.1:
        trend = "rising"
    elif diff < -0.1:
        trend = "declining"
    else:
        trend = "flat"

    return {
        "initial": round(initial, 3),
        "final": round(final, 3),
        "min": round(min(sentiments), 3),
        "max": round(max(sentiments), 3),
        "max_delta_tick": max_delta_tick,
        "trend": trend,
    }


def _compute_wtt(
    final_sentiment: float,
    final_resistance: float,
    domain_engagement_ratio: float,
    contradiction_impact: float,
) -> tuple[int, dict[str, int]]:
    """Compute the Willingness to Transform score (0–100).

    Weighted composite:
      - Trust level (sentiment normalized to 0–1): 30%
      - Absence of resistance: 25%
      - Domain principle engagement ratio: 25%
      - Trust integrity (absence of contradiction impact): 20%
    """
    trust_level = max(0.0, min(1.0, (final_sentiment + 1.0) / 2.0))
    resistance_absence = max(0.0, min(1.0, 1.0 - final_resistance))
    engagement = max(0.0, min(1.0, domain_engagement_ratio))
    integrity = max(0.0, min(1.0, 1.0 - contradiction_impact))

    score = (
        trust_level * 30
        + resistance_absence * 25
        + engagement * 25
        + integrity * 20
    )
    return round(score), {
        "trust_level": round(trust_level * 100),
        "resistance_absence": round(resistance_absence * 100),
        "principle_engagement": round(engagement * 100),
        "trust_integrity": round(integrity * 100),
    }


def _assess_malicious_compliance(
    final_stance: str,
    wall_severity: float,
    avg_inner_sentiment: float,
) -> tuple[str, dict[str, Any]]:
    """Detect malicious compliance risk from displayed vs. operative behavior."""
    displayed_cooperative = final_stance in ("neutral", "supportive", "receptive", "champion")
    operative_friction = wall_severity > 5.0
    inner_negativity = avg_inner_sentiment < -0.2

    if displayed_cooperative and operative_friction and inner_negativity:
        risk = "high"
    elif displayed_cooperative and (operative_friction or inner_negativity):
        risk = "medium"
    else:
        risk = "low"

    return risk, {
        "displayed_stance": final_stance,
        "wall_severity": round(wall_severity, 1),
        "inner_sentiment_avg": round(avg_inner_sentiment, 2),
    }


def _avg_inner_sentiment_for_role(
    echo_voice_log: list[dict[str, Any]],
    role: str,
) -> float:
    """Estimate average inner sentiment from stance history.

    We don't have numeric inner_thought sentiment, so we map stance to
    approximate values and average them.
    """
    stance_to_val = {
        "hostile": -0.8, "resistant": -0.3, "skeptical": -0.1,
        "neutral": 0.1, "receptive": 0.4, "supportive": 0.5, "champion": 0.8,
        "defected": -1.0, "aligned": 1.0,
    }
    entries = [e for e in echo_voice_log if e.get("role") == role]
    if not entries:
        return 0.0
    vals = [stance_to_val.get(e.get("stance", "neutral"), 0.0) for e in entries]
    return sum(vals) / len(vals)


async def emit_stakeholder_psychology(
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Analyse each stakeholder's psychological profile after a simulation.

    Computes Willingness to Transform (WTT) scores, malicious compliance
    risk, sentiment trajectories, and trust architecture from session state.
    Fully deterministic — no LLM call needed.

    Returns:
        On success: ``{"status": "success", "stakeholder_psychology": {...}}``.
        On no data: ``{"status": "skipped", "reason": "..."}``.
    """
    # 1. Read state data.
    try:
        echo_voice_log = tool_context.state.get("echo_voice_log", [])
        route_friction_map = tool_context.state.get("route_friction_map", {})
        contradiction_log = tool_context.state.get("contradiction_log", [])
        defected_ids = tool_context.state.get("defected_echo_ids", [])
        aligned_ids = tool_context.state.get("aligned_echo_ids", [])
        decision_log = tool_context.state.get("decision_log", [])
    except (AttributeError, TypeError):
        return {"status": "skipped", "reason": "Cannot read session state."}

    if not echo_voice_log:
        return {"status": "skipped", "reason": "No echo_voice_log in session state."}

    stakeholder_map = _resolve_stakeholder_map(tool_context)

    # 2. Build role → last tick entry lookup.
    last_entry_by_role: dict[str, dict[str, Any]] = {}
    for entry in echo_voice_log:
        role = entry.get("role", "")
        if role:
            prev = last_entry_by_role.get(role)
            if prev is None or entry.get("tick", 0) > prev.get("tick", 0):
                last_entry_by_role[role] = entry

    # 3. Index stakeholder_map by role/id/label/name for persona data.
    persona_by_role: dict[str, dict[str, Any]] = {}
    canonical_personas: list[tuple[str, dict[str, Any]]] = []
    if isinstance(stakeholder_map, list):
        for s in stakeholder_map:
            if isinstance(s, dict):
                primary_key = (
                    s.get("role")
                    or s.get("id")
                    or s.get("name")
                    or s.get("label")
                    or ""
                )
                if primary_key:
                    canonical_personas.append((str(primary_key), s))
                for candidate in (
                    s.get("role"),
                    s.get("id"),
                    s.get("name"),
                    s.get("label"),
                ):
                    if isinstance(candidate, str) and candidate.strip():
                        persona_by_role[candidate] = s
                        slug = candidate.strip().lower().replace(" ", "_")
                        persona_by_role.setdefault(slug, s)

    # 4. Count decision ticks that engaged each stakeholder's domain principles.
    #    We check decision_tags.principles_applied against the stakeholder's
    #    domain_principles to compute engagement ratio.
    total_ticks = max(len(decision_log), 1)
    domain_engagement_counts: dict[str, int] = {}
    for primary_key, persona in canonical_personas:
        dp = set(persona.get("domain_principles", []))
        if not dp:
            continue
        count = 0
        for d in decision_log:
            tags = d.get("decision_tags", {})
            applied = set(tags.get("principles_applied", []))
            if dp & applied:
                count += 1
        domain_engagement_counts[primary_key] = count
        for alias in (persona.get("role"), persona.get("id")):
            if isinstance(alias, str) and alias:
                domain_engagement_counts[alias] = count

    # 5. Count contradictions per stakeholder's domain.
    contradiction_counts: dict[str, int] = {}
    for c in contradiction_log:
        violated_principle = c.get("violated_principle", "")
        for primary_key, persona in canonical_personas:
            dp = persona.get("domain_principles", [])
            if violated_principle in dp:
                new_val = contradiction_counts.get(primary_key, 0) + 1
                contradiction_counts[primary_key] = new_val
                for alias in (persona.get("role"), persona.get("id")):
                    if isinstance(alias, str) and alias:
                        contradiction_counts[alias] = new_val

    # 6. Build per-stakeholder profiles.
    profiles: list[dict[str, Any]] = []
    high_risk_count = 0
    declining_count = 0

    all_roles = set(last_entry_by_role.keys())
    for placeholder in ("unknown", "Echo"):
        if (
            placeholder in all_roles
            and len(all_roles) > 1
            and placeholder not in persona_by_role
        ):
            all_roles.discard(placeholder)

    matched_persona_ids = {
        id(persona_by_role.get(r) or persona_by_role.get(r.strip().lower().replace(" ", "_")))
        for r in all_roles
        if (persona_by_role.get(r) or persona_by_role.get(r.strip().lower().replace(" ", "_"))) is not None
    }
    unmatched_personas: list[dict[str, Any]] = [
        s for _, s in canonical_personas if id(s) not in matched_persona_ids
    ]

    for role in sorted(all_roles):
        last = last_entry_by_role[role]
        slug = role.strip().lower().replace(" ", "_")
        persona = persona_by_role.get(role) or persona_by_role.get(slug) or {}
        if not persona and unmatched_personas:
            persona = unmatched_personas.pop(0)
        elif not persona and slug in ("unknown", "echo") and stakeholder_map:
            persona = stakeholder_map[0]

        resolved_role = (
            persona.get("role")
            or persona.get("id")
            or persona.get("name")
            or role
        ) if persona else role
        resolved_tier = int(
            persona.get("tier")
            or last.get("tier")
            or (1 if persona else 3)
        )

        raw_behavioral = persona.get("behavioral_patterns")
        if isinstance(raw_behavioral, dict) and raw_behavioral:
            behavioral = {**_default_behavioral_for_tier(resolved_tier), **raw_behavioral}
        else:
            behavioral = _default_behavioral_for_tier(resolved_tier)

        final_sentiment = last.get("sentiment", 0.0)
        final_resistance = last.get("resistance", 0.5)
        final_stance = last.get("stance", "neutral")
        wall_severity = route_friction_map.get(resolved_role, route_friction_map.get(role, 0.0))

        # Sentiment trajectory.
        trajectory = _compute_sentiment_trajectory(echo_voice_log, role)

        # Domain engagement ratio.
        engagement_count = domain_engagement_counts.get(
            resolved_role, domain_engagement_counts.get(role, 0)
        )
        engagement_ratio = engagement_count / total_ticks

        # Contradiction impact (normalized to 0–1, capped at 5).
        c_count = contradiction_counts.get(
            resolved_role, contradiction_counts.get(role, 0)
        )
        contradiction_impact = min(c_count / 5.0, 1.0)

        # WTT score.
        wtt_score, wtt_breakdown = _compute_wtt(
            final_sentiment, final_resistance, engagement_ratio, contradiction_impact,
        )

        # Malicious compliance risk.
        avg_inner = _avg_inner_sentiment_for_role(echo_voice_log, role)
        mc_risk, mc_signals = _assess_malicious_compliance(
            final_stance, wall_severity, avg_inner,
        )

        # Terminal state.
        terminal = None
        if resolved_role in (defected_ids or []) or role in (defected_ids or []):
            terminal = "defected"
        elif resolved_role in (aligned_ids or []) or role in (aligned_ids or []):
            terminal = "aligned"

        # Escalation events (from echo_voice_log notable_events).
        escalation_events: list[str] = []
        for entry in echo_voice_log:
            if entry.get("role") == role:
                event = entry.get("notable_event") or entry.get("autonomous_action")
                if event and isinstance(event, str) and event.strip():
                    escalation_events.append(event.strip())

        # Coalition allies.
        coalition = persona.get("coalition_affinity", [])
        allies: list[str] = []
        for c in (coalition if isinstance(coalition, list) else []):
            if isinstance(c, str):
                ally_name = c.split(" (")[0].strip()
                if ally_name:
                    allies.append(ally_name)

        if mc_risk == "high":
            high_risk_count += 1
        if trajectory["trend"] == "declining":
            declining_count += 1

        default_label = (
            "Stakeholder Representative"
            if str(resolved_role).strip().lower() in ("unknown", "echo", "")
            else str(resolved_role).replace("_", " ").title()
        )
        stakeholder_label = (
            persona.get("label")
            or persona.get("name")
            or default_label
        )

        profile: dict[str, Any] = {
            "stakeholder_id": resolved_role,
            "stakeholder_label": stakeholder_label,
            "tier": resolved_tier,
            "persona_summary": (persona.get("persona", ""))[:200],
            "decision_style": behavioral.get("decision_style", "pragmatic"),
            "communication_style": behavioral.get("communication", "structured"),
            "risk_tolerance": behavioral.get("risk_tolerance", "moderate"),
            "domain_principles": persona.get("domain_principles", []),
            "alignment_drivers": persona.get("alignment_drivers", []),
            "resistance_triggers": persona.get("resistance_triggers", []),
            "coalition_allies": allies,
            "wtt_score": wtt_score,
            "wtt_breakdown": wtt_breakdown,
            "malicious_compliance_risk": mc_risk,
            "malicious_compliance_signals": mc_signals,
            "sentiment_trajectory": trajectory,
            "trust_breaches": c_count,
            "terminal_state": terminal,
            "escalation_events": escalation_events[:5],  # cap at 5
        }
        profiles.append(profile)

    # 7. Sort by WTT score (lowest first — most at-risk stakeholders on top).
    profiles.sort(key=lambda p: p["wtt_score"])

    # 8. Build summary.
    total = len(profiles)
    summary_parts: list[str] = []
    if declining_count:
        summary_parts.append(
            f"{declining_count} of {total} stakeholder(s) show declining trust trajectory"
        )
    if high_risk_count:
        high_risk_names = [
            p["stakeholder_label"] for p in profiles
            if p["malicious_compliance_risk"] == "high"
        ]
        summary_parts.append(
            f"{', '.join(high_risk_names)} flagged for malicious compliance risk"
        )
    avg_wtt = round(sum(p["wtt_score"] for p in profiles) / max(total, 1))
    summary_parts.append(f"Average WTT score: {avg_wtt}/100")
    summary = ". ".join(summary_parts) + "."

    result: dict[str, Any] = {
        "profiles": profiles,
        "summary": summary,
        "inferred": True,
    }

    # 9. Stash in state.
    try:
        tool_context.state["stakeholder_psychology"] = result
    except (AttributeError, TypeError):
        logger.warning(
            "emit_stakeholder_psychology: could not write to session state"
        )

    logger.info(
        "emit_stakeholder_psychology: %d profile(s), avg_wtt=%d, high_risk=%d",
        total, avg_wtt, high_risk_count,
    )
    return {"status": "success", "stakeholder_psychology": result}
