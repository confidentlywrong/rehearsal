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

from datetime import datetime, timezone
import json
import logging
import math
import os
import re
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import Client, types

from agents.ai_echo.process_decision_round import format_role_display
from agents.utils.models import DEFAULT_FLASH_LITE_MODEL, get_model
from agents.utils.retry import resilient_http_options

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GenAI client (lazy singleton — same pattern as evaluator/tools.py)
# ---------------------------------------------------------------------------

_client: Client | None = None
_MODEL = get_model(
    "TRANSLATION_DIAGNOSTIC_MODEL",
    DEFAULT_FLASH_LITE_MODEL,
    fallback_env_vars=("EVALUATOR_MODEL",),
)


_client_key: tuple | None = None


def _get_client() -> Client:
    """Lazy-singleton GenAI client with project/API-key config and retry options."""
    global _client, _client_key
    project_id = (
        (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
        or (os.environ.get("PROJECT_ID") or "").strip()
    )
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
# Inflection-point detection
# ---------------------------------------------------------------------------

def _safe_int(val: Any, default: int = 0) -> int:
    """Safely coerce a value to int, rejecting bools and non-finite floats."""
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        try:
            fv = float(val)
        except OverflowError:
            return default
        return val if math.isfinite(fv) else default
    if isinstance(val, float):
        return int(round(val)) if math.isfinite(val) else default
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return default
        try:
            fv = float(s)
        except (ValueError, OverflowError):
            return default
        return int(round(fv)) if math.isfinite(fv) else default
    return default


def _safe_finite_float(val: Any, default: float = 0.0) -> float:
    """Safely coerce a value to a finite float, rejecting bools and NaN/Inf."""
    if val is None or isinstance(val, bool):
        return default
    try:
        fv = float(val)
    except (TypeError, ValueError, OverflowError):
        return default
    return fv if math.isfinite(fv) else default


def _coerce_state_list(raw: Any) -> list[Any]:
    """Parse list or JSON-encoded list from session state safely."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as exc:
            logger.warning("Failed to parse JSON list from session state: %s", exc)
            return []
    return list(raw) if isinstance(raw, list) else []


def _coerce_state_dict(raw: Any) -> dict[str, Any]:
    """Parse dict or JSON-encoded dict from session state safely."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as exc:
            logger.warning("Failed to parse JSON dict from session state: %s", exc)
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _coerce_str_list(val: Any) -> list[str]:
    """Coerce a sequence or set to a list of non-empty strings without splitting strings."""
    if isinstance(val, (list, tuple, set)):
        out: list[str] = []
        for x in val:
            if x is not None and not isinstance(x, bool):
                s = str(x).strip()
                if s:
                    out.append(s)
        return out
    return []


def _normalize_gap_severity(raw_sev: Any) -> str:
    """Normalize severity strings to 'high' | 'medium' | 'low' for frontend cards."""
    s = str(raw_sev or "medium").strip().lower()
    if s in ("high", "major", "critical", "severe"):
        return "high"
    if s in ("low", "minor", "minimal"):
        return "low"
    return "medium"


def _normalize_llm_diagnostic(parsed_diag: dict[str, Any]) -> dict[str, Any]:
    """Sanitize LLM-returned translation diagnostic JSON to match card schema."""
    raw_gaps = parsed_diag.get("gaps")
    if not isinstance(raw_gaps, list):
        raise ValueError("Translation diagnostic JSON must contain a 'gaps' list")

    clean_gaps: list[dict[str, Any]] = []
    for raw_gap in raw_gaps:
        if not isinstance(raw_gap, dict):
            continue
        sid = str(raw_gap.get("stakeholder_id") or raw_gap.get("role") or "stakeholder").strip()
        slabel = str(
            raw_gap.get("stakeholder_label")
            or format_role_display(sid)
        ).strip()
        raw_tps = raw_gap.get("trigger_phrases")
        clean_tps: list[dict[str, Any]] = []
        if isinstance(raw_tps, list):
            for tp in raw_tps:
                if not isinstance(tp, dict):
                    continue
                clean_tps.append(
                    {
                        "phrase": str(tp.get("phrase") or "").strip(),
                        "tick": _safe_int(tp.get("tick"), 1),
                        "leader_intent": str(tp.get("leader_intent") or "").strip(),
                        "stakeholder_decoded_as": str(tp.get("stakeholder_decoded_as") or "").strip(),
                        "gap_type": str(tp.get("gap_type") or "system directive → lifeworld friction").strip(),
                        "severity": _normalize_gap_severity(tp.get("severity")),
                    }
                )
        clean_gaps.append(
            {
                "stakeholder_id": sid,
                "stakeholder_label": slabel,
                "trigger_phrases": clean_tps,
                "suggested_reframing": str(raw_gap.get("suggested_reframing") or "").strip(),
                "overall_gap_severity": _normalize_gap_severity(raw_gap.get("overall_gap_severity")),
            }
        )

    return {
        "gaps": clean_gaps,
        "summary": str(parsed_diag.get("summary") or "").strip(),
    }


def _find_inflection_points(
    echo_voice_log: list[dict[str, Any]],
    top_n: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Identify the ticks with the largest sentiment swings per stakeholder.

    Returns a dict mapping role → list of log entries at inflection ticks,
    sorted by absolute delta (largest first), capped at *top_n* per role.
    """
    if not isinstance(echo_voice_log, list):
        return {}
    n = max(_safe_int(top_n, 3), 0)
    if n <= 0:
        return {}

    # Group by role, ordered by tick.
    by_role: dict[str, list[dict[str, Any]]] = {}
    for entry in echo_voice_log:
        if not isinstance(entry, dict):
            continue
        raw_role = entry.get("role", "")
        if not isinstance(raw_role, str):
            continue
        role = raw_role.strip()
        if not role:
            continue
        by_role.setdefault(role, []).append(entry)

    inflections: dict[str, list[dict[str, Any]]] = {}
    for role, entries in by_role.items():
        entries.sort(key=lambda e: _safe_int(e.get("tick"), 0))
        deltas: list[tuple[float, dict[str, Any]]] = []
        for i in range(1, len(entries)):
            prev_s = _safe_finite_float(entries[i - 1].get("sentiment"), 0.0)
            curr_s = _safe_finite_float(entries[i].get("sentiment"), 0.0)
            delta = abs(curr_s - prev_s)
            if delta > 0.01:  # ignore noise
                deltas.append((delta, entries[i]))
        # Sort by largest delta first, take top_n.
        deltas.sort(key=lambda t: t[0], reverse=True)
        if deltas and n > 0:
            inflections[role] = [entry for _, entry in deltas[:n]]
        elif entries and n > 0:
            inflections[role] = [entries[-1]]

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
    for entry in (decision_log if isinstance(decision_log, list) else []):
        if not isinstance(entry, dict):
            continue
        tick = entry.get("tick")
        text = entry.get("decision_text", "")
        if tick is not None and text:
            tick_int = _safe_int(tick, -1)
            if tick_int >= 0:
                decisions_by_tick[tick_int] = str(text)

    # Index stakeholder personas by role.
    personas_by_role: dict[str, str] = {}
    if isinstance(stakeholder_map, list):
        for s in stakeholder_map:
            if not isinstance(s, dict):
                continue
            raw_role = s.get("role") or s.get("id")
            if not isinstance(raw_role, str) or not raw_role.strip():
                continue
            role_key = raw_role.strip()
            raw_persona = s.get("persona") or s.get("description")
            persona = str(raw_persona).strip() if raw_persona is not None else ""
            if persona:
                personas_by_role[role_key] = persona
                personas_by_role.setdefault(role_key.lower(), persona)

    if not isinstance(inflections, dict):
        inflections = {}

    sections: list[str] = []
    for role, entries in inflections.items():
        if not isinstance(role, str) or not role.strip():
            continue
        if not isinstance(entries, list):
            continue
        persona_desc = (
            personas_by_role.get(role)
            or personas_by_role.get(role.lower())
            or "No persona description available."
        )
        lines = [f"## Stakeholder: {role}", f"Persona: {persona_desc}", ""]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            tick = entry.get("tick", "?")
            sentiment = entry.get("sentiment", "?")
            stance = entry.get("stance", "?")
            thought = entry.get("inner_thought", "")
            tick_int = _safe_int(tick, -1)
            decision_text = decisions_by_tick.get(tick_int, "(decision text not available)")
            lines.append(f"### Tick {tick}")
            lines.append(f"- Sentiment: {sentiment}, Stance: {stance}")
            lines.append(f"- Inner thought: \"{thought}\"")
            lines.append(f"- Leader's decision text: \"{decision_text}\"")
            lines.append("")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Habermas translation diagnostic (local_gemma deterministic mode)
# ---------------------------------------------------------------------------

def _matches_any(text: str, *keywords: str) -> bool:
    """Return True if any keyword or stem matches text using word boundaries."""
    for kw in keywords:
        pattern = r"\b" + re.escape(kw)
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _classify_habermas_gap(
    principles: list[str] | str | None,
    role: str,
    thought: str,
    sentiment: float,
) -> str:
    """Classify the translation gap into Habermas System vs. Lifeworld archetypes."""
    t_low = (
        thought.lower()
        if isinstance(thought, str)
        else ("" if thought is None or isinstance(thought, bool) else str(thought).lower())
    )
    normalized = (
        [principles.strip()]
        if isinstance(principles, str) and principles.strip()
        else _coerce_str_list(principles)
    )
    p_low = " ".join(normalized).lower()
    r_low = (
        role.lower()
        if isinstance(role, str)
        else ("" if role is None or isinstance(role, bool) else str(role).lower())
    )

    # 1. Stakeholder's observed lifeworld experience (fine-grained matching)
    if _matches_any(t_low, "autonomy", "micromanag", "mandat", "freedom", "inflexib", "prescriptiv"):
        return "operational alignment → autonomy erosion"
    if _matches_any(t_low, "technical debt", "tech debt", "architectural debt", "architectural compromise"):
        return "delivery acceleration → technical debt & burnout"
    if _matches_any(t_low, "bandwidth", "overload", "exhaust", "burnout", "strain", "capacity", "fatigue"):
        return "efficiency benchmarks → cognitive overload"
    if _matches_any(t_low, "distrust", "skeptic", "cynic", "doubt", "suspicio"):
        return "governance rigor → institutional distrust"
    if _matches_any(t_low, "timeline", "deadline", "fast", "rush", "speed", "pace", "pressure"):
        return "delivery acceleration → burnout & quality sacrifice"
    if _matches_any(t_low, "quota", "output quota", "instrumental", "dehumaniz"):
        return "instrumental metrics → empathy deficit"
    if _matches_any(t_low, "fear", "reprisal", "punish", "blame", "retaliat", "intimidation", "conformity", "psychological saf"):
        return "mandated alignment → psychological conformity"
    if _matches_any(t_low, "capital", "expenditure", "margin", "roi", "budget", "fiduciary", "financial"):
        return "capital discipline → resource constriction"

    # 2. Leader's steering media / applied principles
    if _matches_any(p_low, "value", "growth", "capital", "cost", "financial", "optimization"):
        if _matches_any(r_low, "cfo", "finance", "sponsor", "investor"):
            return "capital discipline → resource constriction"
        return "strategic optimization → workforce dispensability"
    if _matches_any(p_low, "governance", "compliance", "policy", "milestone"):
        return "governance rigor → institutional distrust"
    if _matches_any(p_low, "reliability", "delivery", "cadence", "workflow", "handoff"):
        if _matches_any(r_low, "eng", "tech", "architect", "dev"):
            return "delivery acceleration → technical debt & burnout"
        return "process standardization → loss of professional agency"
    if _matches_any(p_low, "bandwidth", "safety", "empowerment"):
        return "mandated alignment → psychological conformity"

    # 3. Stakeholder domain / role context
    if _matches_any(r_low, "cfo", "finance", "sponsor", "investor"):
        return "capital discipline → resource constriction"
    if _matches_any(r_low, "eng", "tech", "dev", "architect"):
        return "delivery acceleration → technical debt & burnout"
    if _matches_any(r_low, "ops", "operation", "logistics"):
        return "process standardization → frontline friction"
    if _matches_any(r_low, "union", "team", "frontline", "employee", "staff"):
        return "systemic efficiency → workforce disenfranchisement"

    return "system language → lifeworld friction"


def _build_suggested_reframing(
    role: str,
    label: str,
    tier: int,
    persona: str,
    domain_principles: list[str],
    principles_applied: list[str],
    thought: str,
) -> str:
    """Generate an actionable, stakeholder-specific Habermas reframing."""
    dp_candidates = _coerce_str_list(domain_principles) or _coerce_str_list(principles_applied)
    dp_focus = dp_candidates[0] if dp_candidates else None
    t_low = str(thought or "").lower()

    if tier == 1:
        if _matches_any(t_low, "capital", "roi", "expenditure", "margin", "cost", "budget", "fiduciary"):
            return (
                f"Anchor strategic commitments in fiduciary rigor and capital governance: "
                f"'Partner directly with {label} to establish collaborative milestone reviews, ensuring strategic resource "
                f"allocations and capital safeguards directly protect enterprise value and margin thresholds without compromising velocity.'"
            )
        if dp_focus:
            return (
                f"Reframe operational commitments around {dp_focus.lower()} and executive governance: "
                f"'Partner directly with {label} to establish collaborative milestone reviews, ensuring strategic resource "
                f"allocations directly safeguard enterprise value without compromising operational velocity.'"
            )
        return (
            f"Reframe around shared strategic governance: "
            f"'Establish collaborative milestone reviews with {label} to ensure delivery targets "
            f"directly safeguard enterprise stability and strategic value creation.'"
        )
    elif tier == 2:
        if _matches_any(t_low, "overload", "debt", "bandwidth", "burnout", "exhaust", "strain", "capacity"):
            return (
                f"Anchor delivery commitments in sustainable engineering capacity and technical integrity: "
                f"'Validate workflow pacing directly with {label} and establish explicit capacity buffers, "
                f"ensuring sprint quotas maintain architectural integrity and prevent burnout across systems.'"
            )
        if dp_focus:
            return (
                f"Anchor operational commitments in {dp_focus.lower()} and buffer protection: "
                f"'Validate workflow pacing directly with {label} and establish explicit capacity buffers, "
                f"ensuring cross-functional handoffs maintain system reliability without overloading teams.'"
            )
        return (
            f"Acknowledge cross-functional dependencies and co-design capacity protections: "
            f"'Validate operational pacing with {label} and embed protected buffer windows, "
            f"ensuring delivery milestones protect workflow continuity and team stability.'"
        )
    else:  # Tier 3 / frontline / general
        if _matches_any(t_low, "overload", "bandwidth", "safety", "fear", "blame", "burnout"):
            return (
                f"Translate system directives into communicative consensus with explicit safety protections: "
                f"'Invite {label} to co-design sprint pacing and establish two-way feedback channels, "
                f"protecting frontline psychological safety and workload sustainability.'"
            )
        if dp_focus:
            return (
                f"Translate system directives into communicative consensus around {dp_focus.lower()}: "
                f"'Invite {label} to co-design sprint pacing and establish two-way feedback channels, "
                f"protecting frontline psychological safety and workload sustainability.'"
            )
        return (
            f"Shift from top-down directive to communicative consensus: "
            f"'Invite {label} to co-design sprint pacing and establish two-way feedback channels, "
            f"protecting frontline psychological safety and workload sustainability.'"
        )


def _generate_habermas_diagnostic(
    inflections: dict[str, list[dict[str, Any]]],
    decision_log: list[dict[str, Any]],
    stakeholder_map: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate deterministic Habermas translation diagnostic (<10ms).

    Analyses inflection points where the leader's System language (steering media:
    administrative power, efficiency metrics, governance directives) collided with
    each stakeholder's Lifeworld (shared background understandings, autonomy,
    psychological safety, craftsmanship).
    """
    if not isinstance(inflections, dict):
        inflections = {}
    if not isinstance(decision_log, list):
        decision_log = []
    if not isinstance(stakeholder_map, list):
        stakeholder_map = []

    decisions_by_tick: dict[int, dict[str, Any]] = {}
    for entry in decision_log:
        if not isinstance(entry, dict):
            continue
        tick_int = _safe_int(entry.get("tick"), -1)
        if tick_int >= 0:
            decisions_by_tick[tick_int] = entry

    stakeholders_by_key: dict[str, dict[str, Any]] = {}
    if stakeholder_map:
        for s in stakeholder_map:
            if not isinstance(s, dict):
                continue
            for k in (s.get("id"), s.get("role"), s.get("label")):
                if k and isinstance(k, str):
                    stakeholders_by_key[k] = s
                    stakeholders_by_key[k.lower()] = s
                    stakeholders_by_key[k.lower().replace(" ", "_")] = s

    gaps: list[dict[str, Any]] = []

    for role, entries in inflections.items():
        if not isinstance(role, str) or not role.strip():
            continue
        clean_entries = [e for e in (entries if isinstance(entries, list) else []) if isinstance(e, dict)]
        if not clean_entries:
            continue
        st = (
            stakeholders_by_key.get(role)
            or stakeholders_by_key.get(role.lower())
            or stakeholders_by_key.get(role.lower().replace(" ", "_"))
            or {}
        )
        raw_label = st.get("label")
        label = (
            str(raw_label).strip()
            if raw_label is not None and str(raw_label).strip()
            else role.replace("_", " ").title()
        )
        raw_persona = st.get("persona") if st.get("persona") is not None else st.get("description", "")
        persona = str(raw_persona or "")
        tier = _safe_int(st.get("tier"), 2)
        domain_principles = _coerce_str_list(st.get("domain_principles"))

        trigger_phrases: list[dict[str, Any]] = []
        all_principles_for_stakeholder: list[str] = []

        entries_sorted = sorted(clean_entries, key=lambda e: _safe_int(e.get("tick"), 0))
        for entry in entries_sorted:
            tick = _safe_int(entry.get("tick"), 1)
            sentiment = _safe_finite_float(entry.get("sentiment"), 0.0)
            stance = str(entry.get("stance") or "neutral").strip().lower()
            thought = str(entry.get("inner_thought") or "").strip()

            d_entry = decisions_by_tick.get(tick, {})
            decision_text = str(d_entry.get("decision_text") or "").strip()
            tags = d_entry.get("decision_tags") or {}
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception as exc:
                    logger.warning("Failed to parse decision_tags JSON in diagnostic: %s", exc)
                    tags = {}
            if not isinstance(tags, dict):
                tags = {}
            raw_pa = tags.get("principles_applied")
            if isinstance(raw_pa, str):
                stripped_pa = raw_pa.strip()
                principles_applied = [stripped_pa] if stripped_pa else []
            else:
                principles_applied = _coerce_str_list(raw_pa)
            if principles_applied:
                all_principles_for_stakeholder.extend(principles_applied)

            # 1. Trigger phrase
            if decision_text:
                first_sent = decision_text.split(".")[0].strip()
                phrase = first_sent if first_sent else decision_text[:90].strip()
            else:
                if principles_applied:
                    phrase = f"Implement operational priorities aligned with {principles_applied[0]}"
                else:
                    phrase = "(No decision text recorded)"

            # 2. Leader intent
            if principles_applied:
                if len(principles_applied) == 1:
                    leader_intent = (
                        f"Drive strategic operational realignment and milestone accountability "
                        f"through {principles_applied[0]}."
                    )
                else:
                    leader_intent = (
                        f"Establish operational predictability and cross-functional alignment "
                        f"via {principles_applied[0]} and {principles_applied[1]}."
                    )
            elif decision_text:
                leader_intent = (
                    "Strategic operational realignment, milestone accountability, and delivery predictability."
                )
            else:
                leader_intent = "Strategic operational realignment and structured milestone execution."

            # 3. Stakeholder decoded as
            if thought:
                stakeholder_decoded_as = thought
            else:
                if tier == 1:
                    stakeholder_decoded_as = (
                        "Concerned that operational execution risks might compromise strategic "
                        "governance milestones and capital efficiency."
                    )
                elif tier == 2:
                    stakeholder_decoded_as = (
                        "Perceived as top-down scheduling pressure that threatens cross-functional "
                        "handoff stability and team capacity."
                    )
                else:
                    stakeholder_decoded_as = (
                        "Experienced as instrumental metrics and delivery acceleration encroaching "
                        "upon team autonomy and psychological safety."
                    )

            # 4. Gap type
            gap_type = _classify_habermas_gap(
                principles=principles_applied or domain_principles,
                role=role,
                thought=thought,
                sentiment=sentiment,
            )

            # 5. Severity
            if sentiment < -0.25 or stance in ("hostile", "resistant"):
                severity = "high"
            elif sentiment < 0.15 or stance in ("skeptical", "concerned", "neutral"):
                severity = "medium"
            else:
                severity = "low"

            trigger_phrases.append({
                "phrase": phrase,
                "tick": tick,
                "leader_intent": leader_intent,
                "stakeholder_decoded_as": stakeholder_decoded_as,
                "gap_type": gap_type,
                "severity": severity,
            })

        # Calculate overall gap severity for this stakeholder
        severities = [tp["severity"] for tp in trigger_phrases]
        if "high" in severities:
            overall_severity = "high"
        elif "medium" in severities:
            overall_severity = "medium"
        else:
            overall_severity = "low"

        # Suggested reframing: pass combined thoughts observed across inflection entries
        combined_thoughts = " ".join(
            str(e.get("inner_thought") or "") for e in clean_entries
        ).strip()
        suggested_reframing = _build_suggested_reframing(
            role=role,
            label=label,
            tier=tier,
            persona=persona,
            domain_principles=domain_principles,
            principles_applied=all_principles_for_stakeholder,
            thought=combined_thoughts,
        )

        gaps.append({
            "stakeholder_id": role,
            "stakeholder_label": label,
            "trigger_phrases": trigger_phrases,
            "suggested_reframing": suggested_reframing,
            "overall_gap_severity": overall_severity,
        })

    summary = (
        f"Habermasian translation analysis identified {len(gaps)} stakeholder domain(s) "
        "where strategic system directives diverged from lifeworld expectations. "
        "Operational friction primarily stems from top-down pacing and governance mandates "
        "colliding with frontline autonomy and capacity constraints. "
        "Achieving communicative action requires establishing co-designed milestones "
        "with explicit psychological safety and bandwidth protections."
    )

    return {
        "gaps": gaps,
        "summary": summary,
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
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse stakeholder_map JSON from session state: %s", exc)
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
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse leadership_scenario JSON from session state: %s", exc)
            scenario = {}

    if isinstance(scenario, dict):
        fallback = scenario.get("stakeholders") or scenario.get("echoes") or []
        if isinstance(fallback, str):
            try:
                fallback = json.loads(fallback)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("Failed to parse scenario stakeholders JSON from session state: %s", exc)
                fallback = []
        if isinstance(fallback, list):
            return [s for s in fallback if isinstance(s, dict)]

    return []


def _ensure_telemetry_in_state(
    tool_context: ToolContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retrieve echo_voice_log and decision_log from session state without injecting synthetic data."""
    echo_voice_log: list[dict[str, Any]] = []
    decision_log: list[dict[str, Any]] = []

    try:
        raw_vl = tool_context.state.get("echo_voice_log")
        if isinstance(raw_vl, str):
            try:
                raw_vl = json.loads(raw_vl)
            except Exception as exc:
                logger.warning("Failed to parse echo_voice_log JSON from session state: %s", exc)
                raw_vl = []
        if isinstance(raw_vl, list) and raw_vl:
            echo_voice_log = [e for e in raw_vl if isinstance(e, dict)]

        raw_dl = tool_context.state.get("decision_log")
        if isinstance(raw_dl, str):
            try:
                raw_dl = json.loads(raw_dl)
            except Exception as exc:
                logger.warning("Failed to parse decision_log JSON from session state: %s", exc)
                raw_dl = []
        if isinstance(raw_dl, list) and raw_dl:
            decision_log = [d for d in raw_dl if isinstance(d, dict)]
    except (AttributeError, TypeError):
        pass

    return echo_voice_log, decision_log


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------

async def emit_translation_diagnostic(
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Analyse the leader's communication for System–Lifeworld translation gaps.

    Reads ``echo_voice_log`` and ``decision_log`` from session state, identifies
    sentiment inflection points per stakeholder, and uses Gemini (or the
    deterministic Habermas analyzer in ``local_gemma`` mode) to diagnose where
    the leader's System language collided with each stakeholder's Lifeworld.

    Returns:
        On success: ``{"status": "success", "translation_diagnostic": {...}}``.
        On error: ``{"status": "error", "error": "...", "message": "..."}``.
    """
    # 1. Read telemetry from state. Never synthesize fake telemetry into state.
    echo_voice_log, decision_log = _ensure_telemetry_in_state(tool_context)
    if not echo_voice_log or not decision_log:
        return {
            "status": "error",
            "error": "no_telemetry",
            "message": "No simulation telemetry available. Run the simulation to record stakeholder decisions and sentiment before requesting translation diagnostic.",
            "translation_diagnostic": {
                "gaps": [],
                "summary": "No simulation telemetry available. Run the simulation to record stakeholder decisions and sentiment before requesting translation diagnostic.",
                "actionable_corrections": [],
            },
        }

    # 2. Stakeholder personas for richer context (empty list if absent; never fabricate fake personas).
    stakeholder_map = _resolve_stakeholder_map(tool_context)

    # 3. Find inflection points (top 3 per stakeholder).
    inflections = _find_inflection_points(echo_voice_log, top_n=3)
    if not inflections:
        return {
            "status": "error",
            "error": "no_inflections",
            "message": "No sentiment inflection points could be extracted from echo_voice_log.",
            "translation_diagnostic": {
                "gaps": [],
                "summary": "No sentiment inflection points could be extracted from echo_voice_log.",
            },
        }

    # 4. Generate diagnostic: deterministic Habermas engine in explicit local_gemma
    #    mode (<10ms), or Gemini call in cloud mode (failing explicitly on error).
    diagnostic: dict[str, Any] | None = None
    eval_method = "llm"

    from agents.utils.local_ai import is_local_gemma_active

    if is_local_gemma_active():
        # In local Gemma mode, generating a full 2,000-token multi-stakeholder
        # translation diagnostic on CPU causes multi-minute timeouts.
        # Generate rich Habermas translation diagnostic deterministically (<10ms).
        eval_method = "deterministic_habermas"
        diagnostic = _generate_habermas_diagnostic(
            inflections=inflections,
            decision_log=decision_log,
            stakeholder_map=stakeholder_map,
        )
    else:
        user_prompt = _build_analysis_prompt(inflections, decision_log, stakeholder_map)
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
            raw_text = (response.text or "").strip()
            # Strip markdown fences if present (including trailing whitespace/newlines).
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
                raw_text = raw_text.strip()
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3].strip()
            parsed_diag = json.loads(raw_text)
            if not isinstance(parsed_diag, dict):
                raise ValueError("Translation diagnostic JSON must be an object")
            normalized_diag = _normalize_llm_diagnostic(parsed_diag)
            if inflections and not normalized_diag["gaps"]:
                raise ValueError("LLM returned empty gaps despite active inflection points")
            diagnostic = normalized_diag
        except Exception as exc:
            logger.error(
                "emit_translation_diagnostic: LLM analysis failed: %s",
                exc,
                exc_info=True,
            )
            return {
                "status": "error",
                "error": "llm_failed",
                "message": f"Translation diagnostic LLM analysis failed: {exc}",
                "translation_diagnostic": {
                    "gaps": [],
                    "summary": f"Translation diagnostic LLM analysis failed: {exc}",
                },
            }

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
    role: str | set[str] | list[str],
) -> dict[str, Any]:
    """Compute sentiment trajectory stats for a single stakeholder."""
    if not isinstance(echo_voice_log, list):
        return {
            "initial": 0.0, "final": 0.0, "min": 0.0, "max": 0.0,
            "max_delta_tick": 0, "trend": "flat",
        }
    if isinstance(role, (set, list, tuple)):
        role_set = {
            str(r).strip().lower()
            for r in role
            if r is not None and not isinstance(r, bool) and str(r).strip()
        }
        entries = [
            e for e in echo_voice_log
            if isinstance(e, dict) and isinstance(e.get("role"), str) and (
                e["role"].strip().lower() in role_set
                or e["role"].strip().lower().replace(" ", "_") in role_set
            )
        ]
    else:
        role_low = role.strip().lower() if isinstance(role, str) else ""
        if not role_low:
            entries = []
        else:
            entries = [
                e for e in echo_voice_log
                if isinstance(e, dict) and isinstance(e.get("role"), str) and (
                    e["role"].strip().lower() == role_low
                    or e["role"].strip().lower().replace(" ", "_") == role_low
                )
            ]
    entries.sort(key=lambda e: _safe_int(e.get("tick"), 0))
    if not entries:
        return {
            "initial": 0.0, "final": 0.0, "min": 0.0, "max": 0.0,
            "max_delta_tick": 0, "trend": "flat",
        }

    sentiments = [_safe_finite_float(e.get("sentiment"), 0.0) for e in entries]
    initial = sentiments[0]
    final = sentiments[-1]

    # Find the tick with the largest single-step delta.
    max_delta = 0.0
    max_delta_tick = _safe_int(entries[0].get("tick"), 0)
    for i in range(1, len(sentiments)):
        delta = abs(sentiments[i] - sentiments[i - 1])
        if delta > max_delta:
            max_delta = delta
            max_delta_tick = _safe_int(entries[i].get("tick"), 0)

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
    final_sentiment = _safe_finite_float(final_sentiment, 0.0)
    final_resistance = _safe_finite_float(final_resistance, 0.5)
    domain_engagement_ratio = _safe_finite_float(domain_engagement_ratio, 0.0)
    contradiction_impact = _safe_finite_float(contradiction_impact, 0.0)

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
    wall_severity = _safe_finite_float(wall_severity, 0.0)
    avg_inner_sentiment = _safe_finite_float(avg_inner_sentiment, 0.0)

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
    role: str | set[str] | list[str],
) -> float:
    """Estimate average inner sentiment from stance history.

    We don't have numeric inner_thought sentiment, so we map stance to
    approximate values and average them.
    """
    if not isinstance(echo_voice_log, list):
        return 0.0
    stance_to_val = {
        "hostile": -0.8, "resistant": -0.3, "skeptical": -0.1,
        "neutral": 0.1, "receptive": 0.4, "supportive": 0.5, "champion": 0.8,
        "defected": -1.0, "aligned": 1.0,
    }
    if isinstance(role, (set, list, tuple)):
        role_set = {
            str(r).strip().lower()
            for r in role
            if r is not None and not isinstance(r, bool) and str(r).strip()
        }
        entries = [
            e for e in echo_voice_log
            if isinstance(e, dict) and isinstance(e.get("role"), str) and (
                e["role"].strip().lower() in role_set
                or e["role"].strip().lower().replace(" ", "_") in role_set
            )
        ]
    else:
        role_low = role.strip().lower() if isinstance(role, str) else ""
        if not role_low:
            entries = []
        else:
            entries = [
                e for e in echo_voice_log
                if isinstance(e, dict) and isinstance(e.get("role"), str) and (
                    e["role"].strip().lower() == role_low
                    or e["role"].strip().lower().replace(" ", "_") == role_low
                )
            ]
    if not entries:
        return 0.0
    vals: list[float] = []
    for e in entries:
        raw_stance = e.get("stance")
        stance_key = (
            raw_stance.strip().lower()
            if isinstance(raw_stance, str)
            else ("neutral" if raw_stance is None else str(raw_stance))
        )
        vals.append(stance_to_val.get(stance_key, 0.0))
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
        On no data: ``{"status": "error", "error": "no_telemetry", ...}``.
    """
    # 1. Read telemetry from session state.
    state_obj: Any = None
    try:
        state_obj = getattr(tool_context, "state", None) if tool_context else None
        if state_obj is not None and hasattr(state_obj, "get"):
            echo_voice_log = [
                e for e in _coerce_state_list(state_obj.get("echo_voice_log")) if isinstance(e, dict)
            ]
            route_friction_map = _coerce_state_dict(state_obj.get("route_friction_map"))
            contradiction_log = [
                c for c in _coerce_state_list(state_obj.get("contradiction_log")) if isinstance(c, dict)
            ]
            defected_ids = _coerce_state_list(state_obj.get("defected_echo_ids"))
            aligned_ids = _coerce_state_list(state_obj.get("aligned_echo_ids"))
            decision_log = [
                d for d in _coerce_state_list(state_obj.get("decision_log")) if isinstance(d, dict)
            ]
        else:
            echo_voice_log = []
            route_friction_map = {}
            contradiction_log = []
            defected_ids = []
            aligned_ids = []
            decision_log = []
    except (AttributeError, TypeError):
        echo_voice_log = []
        route_friction_map = {}
        contradiction_log = []
        defected_ids = []
        aligned_ids = []
        decision_log = []

    if not echo_voice_log:
        raw_vl, raw_dl = _ensure_telemetry_in_state(tool_context)
        echo_voice_log = [e for e in raw_vl if isinstance(e, dict)]
        decision_log = [d for d in raw_dl if isinstance(d, dict)]

    raw_sh_map = None
    try:
        if state_obj is not None and hasattr(state_obj, "get"):
            raw_sh_map = state_obj.get("stakeholder_map")
    except (AttributeError, TypeError):
        raw_sh_map = None

    if isinstance(raw_sh_map, str):
        try:
            raw_sh_map = json.loads(raw_sh_map)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse stakeholder_map JSON in psychology tool: %s", exc)
            raw_sh_map = None

    stakeholder_map = (
        [s for s in raw_sh_map if isinstance(s, dict)]
        if isinstance(raw_sh_map, list)
        else []
    )

    if not echo_voice_log:
        if not stakeholder_map:
            return {
                "status": "error",
                "error": "no_telemetry",
                "message": "No telemetry or stakeholder data available to compute psychology.",
                "stakeholder_psychology": {
                    "profiles": [],
                    "summary": "No simulation telemetry available. Run the simulation to record stakeholder decisions and sentiment before requesting stakeholder psychology.",
                },
            }

        # Baseline stakeholder psychological profiles from stakeholder_map
        profiles: list[dict[str, Any]] = []
        for sh in stakeholder_map:
            if not isinstance(sh, dict):
                continue
            role = str(sh.get("role") or sh.get("id") or "stakeholder").strip() or "stakeholder"
            sh_id = str(sh.get("stakeholder_id") or sh.get("id") or sh.get("role") or "stakeholder").strip() or "stakeholder"
            resolved_tier = _safe_int(sh.get("tier"), 2)
            label = str(sh.get("label") or sh.get("name") or format_role_display(role)).strip()
            persona_summary = str(sh.get("persona") or sh.get("description") or "")[:200]
            archetype = str(sh.get("archetype") or format_role_display(role)).strip()
            department = str(sh.get("department") or sh.get("department_name") or "").strip()

            power_level = _safe_finite_float(
                sh.get("power_level") if sh.get("power_level") is not None
                else sh.get("initial_influence"),
                0.5,
            )
            interest_level = _safe_finite_float(
                sh.get("interest_level"),
                0.5,
            )
            s_res = _safe_finite_float(
                sh.get("resistance_score") if sh.get("resistance_score") is not None
                else (sh.get("resistance") if sh.get("resistance") is not None
                      else sh.get("initial_resistance")),
                0.5,
            )

            raw_behavioral = sh.get("behavioral_patterns")
            if isinstance(raw_behavioral, dict) and raw_behavioral:
                dec_from_bp = raw_behavioral.get("decision_style")
                comm_from_bp = raw_behavioral.get("communication_style") or raw_behavioral.get("communication")
                risk_from_bp = raw_behavioral.get("risk_tolerance")
            else:
                dec_from_bp = None
                comm_from_bp = None
                risk_from_bp = None

            tier_defaults = _default_behavioral_for_tier(resolved_tier)
            decision_style = str(
                dec_from_bp or sh.get("decision_style") or tier_defaults.get("decision_style", "pragmatic")
            )
            communication_style = str(
                comm_from_bp
                or sh.get("communication_style")
                or sh.get("communication")
                or tier_defaults.get("communication_style")
                or tier_defaults.get("communication", "structured")
            )
            risk_tolerance = str(
                risk_from_bp or sh.get("risk_tolerance") or tier_defaults.get("risk_tolerance", "moderate")
            )

            coalition = sh.get("coalition_affinity", sh.get("coalition_allies", []))
            allies: list[str] = []
            for c in (coalition if isinstance(coalition, list) else []):
                if isinstance(c, str):
                    ally_name = c.split(" (")[0].strip()
                    if ally_name:
                        allies.append(format_role_display(ally_name))

            wtt_score, wtt_breakdown = _compute_wtt(0.0, s_res, 0.0, 0.0)

            profiles.append({
                "stakeholder_id": sh_id,
                "role": role,
                "stakeholder_label": label,
                "tier": resolved_tier,
                "archetype": archetype,
                "department": department,
                "power_level": power_level,
                "interest_level": interest_level,
                "resistance_score": s_res,
                "persona_summary": persona_summary,
                "decision_style": decision_style,
                "communication_style": communication_style,
                "risk_tolerance": risk_tolerance,
                "domain_principles": _coerce_str_list(sh.get("domain_principles")) or _coerce_str_list(sh.get("principles")),
                "alignment_drivers": _coerce_str_list(sh.get("alignment_drivers")),
                "resistance_triggers": _coerce_str_list(sh.get("resistance_triggers")),
                "coalition_allies": allies,
                "wtt_score": wtt_score,
                "wtt_breakdown": wtt_breakdown,
                "malicious_compliance_risk": "low",
                "malicious_compliance_signals": {
                    "displayed_stance": "neutral",
                    "wall_severity": 0.0,
                    "inner_sentiment_avg": 0.0,
                },
                "sentiment_trajectory": {
                    "initial": 0.0,
                    "final": 0.0,
                    "min": 0.0,
                    "max": 0.0,
                    "max_delta_tick": 0,
                    "trend": "flat",
                },
                "trust_breaches": 0,
                "terminal_state": None,
                "advocacy_actions": [],
                "escalation_events": [],
            })

        if not profiles:
            return {
                "status": "error",
                "error": "no_telemetry",
                "message": "No telemetry or stakeholder data available to compute psychology.",
                "stakeholder_psychology": {
                    "profiles": [],
                    "summary": "No simulation telemetry available. Run the simulation to record stakeholder decisions and sentiment before requesting stakeholder psychology.",
                },
            }

        profiles.sort(key=lambda p: p["wtt_score"])
        total = len(profiles)
        avg_wtt = round(sum(p["wtt_score"] for p in profiles) / max(total, 1))
        summary = (
            f"Baseline psychological profiles established for {total} stakeholder(s). "
            f"Average baseline WTT score: {avg_wtt}/100. Simulation telemetry pending."
        )

        archetypes = [
            str(p.get("archetype") or p.get("role") or "").strip()
            for p in profiles
            if (p.get("archetype") or p.get("role"))
        ]
        dominant = max(set(archetypes), key=archetypes.count) if archetypes else "Pragmatic Executor"

        result: dict[str, Any] = {
            "profiles": profiles,
            "coalitions": [],
            "polarization_index": 0.0,
            "dominant_archetype": dominant,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "inferred": False,
        }

        try:
            tool_context.state["stakeholder_psychology"] = result
        except (AttributeError, TypeError):
            logger.warning("emit_stakeholder_psychology: could not write to session state")

        logger.info(
            "emit_stakeholder_psychology: %d baseline profile(s), avg_wtt=%d",
            total, avg_wtt,
        )
        return {"status": "success", "stakeholder_psychology": result}

    if not stakeholder_map:
        stakeholder_map = _resolve_stakeholder_map(tool_context)

    # Sort voice log chronologically by tick so trajectory and event extraction are deterministic.
    echo_voice_log.sort(key=lambda e: _safe_int(e.get("tick"), 0))

    # 2. Build role → last tick entry lookup.
    last_entry_by_role: dict[str, dict[str, Any]] = {}
    for entry in echo_voice_log:
        raw_role = entry.get("role")
        role = raw_role.strip() if isinstance(raw_role, str) else ""
        if role:
            prev = last_entry_by_role.get(role)
            if prev is None or _safe_int(entry.get("tick"), 0) >= _safe_int(prev.get("tick"), 0):
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
        dp = set(_coerce_str_list(persona.get("domain_principles")))
        if not dp:
            continue
        count = 0
        for d in decision_log:
            tags = d.get("decision_tags", {})
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception as exc:
                    logger.warning("Failed to parse decision_tags JSON in psychology tool: %s", exc)
                    tags = {}
            if not isinstance(tags, dict):
                tags = {}
            applied = set(_coerce_str_list(tags.get("principles_applied")))
            if dp & applied:
                count += 1
        domain_engagement_counts[primary_key] = count
        for alias in (persona.get("role"), persona.get("id"), persona.get("name"), persona.get("label")):
            if isinstance(alias, str) and alias:
                domain_engagement_counts[alias] = count
                domain_engagement_counts[alias.strip().lower()] = count
                domain_engagement_counts[alias.strip().lower().replace(" ", "_")] = count

    # 5. Count contradictions per stakeholder's domain.
    contradiction_counts: dict[str, int] = {}
    for c in contradiction_log:
        violated_principle = str(c.get("violated_principle") or "")
        for primary_key, persona in canonical_personas:
            dp = set(_coerce_str_list(persona.get("domain_principles")))
            if violated_principle and violated_principle in dp:
                new_val = contradiction_counts.get(primary_key, 0) + 1
                contradiction_counts[primary_key] = new_val
                for alias in (persona.get("role"), persona.get("id"), persona.get("name"), persona.get("label")):
                    if isinstance(alias, str) and alias:
                        contradiction_counts[alias] = new_val
                        contradiction_counts[alias.strip().lower()] = new_val
                        contradiction_counts[alias.strip().lower().replace(" ", "_")] = new_val

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

        resolved_role = str(
            (
                persona.get("role")
                or persona.get("id")
                or persona.get("name")
                or role
            ) if persona else role
        )
        resolved_tier = _safe_int(
            persona.get("tier") or last.get("tier"),
            1 if persona else 3,
        )

        default_label = (
            "Stakeholder Representative"
            if resolved_role.strip().lower() in ("unknown", "echo", "")
            else format_role_display(resolved_role)
        )
        stakeholder_label = str(
            persona.get("label")
            or persona.get("name")
            or default_label
        )

        # Collect all recognized aliases for this stakeholder
        persona_aliases = {
            str(role).strip().lower(),
            str(resolved_role).strip().lower(),
            str(stakeholder_label).strip().lower(),
            str(persona.get("id") or "").strip().lower(),
            str(persona.get("role") or "").strip().lower(),
            str(persona.get("name") or "").strip().lower(),
            slug,
        }
        persona_aliases.discard("")

        raw_behavioral = persona.get("behavioral_patterns")
        if isinstance(raw_behavioral, dict) and raw_behavioral:
            behavioral = {**_default_behavioral_for_tier(resolved_tier), **raw_behavioral}
        else:
            behavioral = _default_behavioral_for_tier(resolved_tier)

        final_sentiment = _safe_finite_float(last.get("sentiment"), 0.0)
        final_resistance = _safe_finite_float(
            last.get("resistance") if last.get("resistance") is not None else 0.5,
            0.5,
        )
        final_stance = str(last.get("stance") or "neutral")
        wall_severity = _safe_finite_float(
            route_friction_map.get(resolved_role, route_friction_map.get(role, 0.0)),
            0.0,
        )

        # Sentiment trajectory.
        trajectory = _compute_sentiment_trajectory(echo_voice_log, persona_aliases)

        # Domain engagement ratio.
        engagement_count = 0
        for a in persona_aliases:
            if a in domain_engagement_counts:
                engagement_count = domain_engagement_counts[a]
                break
        engagement_ratio = engagement_count / total_ticks

        # Contradiction impact (normalized to 0–1, capped at 5).
        c_count = 0
        for a in persona_aliases:
            if a in contradiction_counts:
                c_count = contradiction_counts[a]
                break
        contradiction_impact = min(c_count / 5.0, 1.0)

        # WTT score.
        wtt_score, wtt_breakdown = _compute_wtt(
            final_sentiment, final_resistance, engagement_ratio, contradiction_impact,
        )

        # Malicious compliance risk.
        avg_inner = _avg_inner_sentiment_for_role(echo_voice_log, persona_aliases)
        mc_risk, mc_signals = _assess_malicious_compliance(
            final_stance, wall_severity, avg_inner,
        )

        # Terminal state.
        terminal = None
        if resolved_role in (defected_ids or []) or role in (defected_ids or []):
            terminal = "defected"
        elif resolved_role in (aligned_ids or []) or role in (aligned_ids or []):
            terminal = "aligned"

        # Coalition allies.
        coalition = persona.get("coalition_affinity", [])
        allies: list[str] = []
        for c in (coalition if isinstance(coalition, list) else []):
            if isinstance(c, str):
                ally_name = c.split(" (")[0].strip()
                if ally_name:
                    allies.append(format_role_display(ally_name))

        # Categorize events from echo_voice_log into advocacy_actions and escalation_events
        _stance_ranks = {
            "hostile": 0,
            "resistant": 1,
            "skeptical": 1,
            "neutral": 2,
            "receptive": 3,
            "supportive": 4,
            "champion": 5,
            "aligned": 5,
        }

        advocacy_list: list[str] = []
        escalation_list: list[str] = []

        for entry in echo_voice_log:
            entry_role = entry.get("role")
            entry_low = str(entry_role or "").strip().lower()
            if entry_low in persona_aliases or entry_low.replace(" ", "_") in persona_aliases:
                for key in ("notable_event", "autonomous_action"):
                    raw_evt = entry.get(key)
                    if not raw_evt or not isinstance(raw_evt, str) or not raw_evt.strip():
                        continue
                    evt_str = raw_evt.strip()

                    # Defensively replace raw role slug with formatted stakeholder_label if present at the start
                    replace_patterns = sorted(persona_aliases, key=len, reverse=True)
                    alias_regex = "|".join(re.escape(a) for a in replace_patterns if a)
                    if alias_regex:
                        leading_pattern = rf"^(?:{alias_regex})\b"
                        evt_formatted = re.sub(
                            leading_pattern,
                            lambda _: stakeholder_label,
                            evt_str,
                            flags=re.IGNORECASE,
                        )
                    else:
                        evt_formatted = evt_str

                    # Defensively format any remaining snake_case slugs in the string
                    def _replace_slug(m: re.Match) -> str:
                        word = m.group(0)
                        return format_role_display(word)

                    evt_formatted = re.sub(r"\b[a-z]+(?:_[a-z0-9]+)+\b", _replace_slug, evt_formatted)

                    low = evt_formatted.lower()

                    # Check for stance shift transition
                    shift_match = re.search(r"shifts\s+(?:from\s+)?(\w+)\s+to\s+(\w+)", low)
                    is_advocacy = False
                    is_escalation = False

                    if "agenda" in low:
                        is_escalation = True
                    elif shift_match:
                        from_s, to_s = shift_match.group(1), shift_match.group(2)
                        rank_from = _stance_ranks.get(from_s, 2)
                        rank_to = _stance_ranks.get(to_s, 2)
                        if rank_to > rank_from:
                            is_advocacy = True
                        elif rank_to < rank_from:
                            is_escalation = True

                    if not is_advocacy and not is_escalation:
                        if any(k in low for k in ("champion", "aligned")):
                            is_advocacy = True
                        elif any(k in low for k in ("undermine", "escalat", "withdraw", "emergency", "defected")):
                            is_escalation = True

                    if is_advocacy:
                        advocacy_list.append(evt_formatted)
                    elif is_escalation:
                        escalation_list.append(evt_formatted)

        # Deduplicate preserving chronological order, capped at 5 items each
        def _dedupe_and_cap(events: list[str], cap: int = 5) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for e in events:
                if e not in seen:
                    seen.add(e)
                    out.append(e)
                    if len(out) >= cap:
                        break
            return out

        advocacy_actions = _dedupe_and_cap(advocacy_list, 5)
        escalation_events = _dedupe_and_cap(escalation_list, 5)

        if mc_risk == "high":
            high_risk_count += 1
        if trajectory["trend"] == "declining":
            declining_count += 1

        profile: dict[str, Any] = {
            "stakeholder_id": resolved_role,
            "stakeholder_label": stakeholder_label,
            "tier": resolved_tier,
            "persona_summary": str(persona.get("persona") or "")[:200],
            "decision_style": str(behavioral.get("decision_style") or "pragmatic"),
            "communication_style": str(
                behavioral.get("communication_style") or behavioral.get("communication") or "structured"
            ),
            "risk_tolerance": str(behavioral.get("risk_tolerance") or "moderate"),
            "domain_principles": _coerce_str_list(persona.get("domain_principles")),
            "alignment_drivers": _coerce_str_list(persona.get("alignment_drivers")),
            "resistance_triggers": _coerce_str_list(persona.get("resistance_triggers")),
            "coalition_allies": allies,
            "wtt_score": wtt_score,
            "wtt_breakdown": wtt_breakdown,
            "malicious_compliance_risk": mc_risk,
            "malicious_compliance_signals": mc_signals,
            "sentiment_trajectory": trajectory,
            "trust_breaches": c_count,
            "terminal_state": terminal,
            "advocacy_actions": advocacy_actions,
            "escalation_events": escalation_events,
        }
        profiles.append(profile)

    if not profiles:
        return {
            "status": "error",
            "error": "no_telemetry",
            "message": "No simulation telemetry available. Run the simulation to record stakeholder decisions and sentiment before requesting stakeholder psychology.",
            "stakeholder_psychology": {
                "profiles": [],
                "summary": "No simulation telemetry available. Run the simulation to record stakeholder decisions and sentiment before requesting stakeholder psychology.",
            },
        }

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
