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

"""Stakeholder mapping tools for the Leadership Planner agent.

Mirrors agents/planner/skills/directing-the-event/scripts/tools.py
but generates stakeholder persona cards instead of marathon event plans.
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Behavioral pattern templates
# ---------------------------------------------------------------------------

_BEHAVIORAL_PATTERNS = {
    "C-Suite": {
        "decision_style": "strategic",
        "communication": "top-down directive",
        "influence_radius": "organization-wide",
        "risk_tolerance": "calculated",
    },
    "VP": {
        "decision_style": "tactical",
        "communication": "cross-functional collaborative",
        "influence_radius": "multi-department",
        "risk_tolerance": "moderate",
    },
    "Director": {
        "decision_style": "operational",
        "communication": "team-focused",
        "influence_radius": "departmental",
        "risk_tolerance": "conservative",
    },
    "Senior": {
        "decision_style": "analytical",
        "communication": "data-driven",
        "influence_radius": "team-level",
        "risk_tolerance": "evidence-based",
    },
    "Manager": {
        "decision_style": "pragmatic",
        "communication": "hands-on",
        "influence_radius": "direct reports",
        "risk_tolerance": "low",
    },
}


# ---------------------------------------------------------------------------
# Tool: map_stakeholders
# ---------------------------------------------------------------------------


async def map_stakeholders(
    scenario_context: str = "",
    tool_context=None,
) -> Dict[str, Any]:
    """Generate stakeholder persona cards for the leadership scenario.

    Creates AI Echo-compatible persona definitions enriched with behavioral
    patterns, communication styles, and influence mapping. Reads the base
    stakeholder topology from session state (set by design_leadership_scenario).

    Args:
        scenario_context: Additional context to inform persona generation.
            Optional — used to tailor personas to specific organizational dynamics.
        tool_context: ADK ToolContext (injected automatically).

    Returns:
        dict with stakeholder_map (list of enriched persona configs).
    """
    if not tool_context:
        return {"status": "error", "message": "No tool context available."}

    scenario = tool_context.state.get("leadership_scenario")
    if not scenario:
        return {
            "status": "error",
            "message": "No leadership scenario found. Run 'design_leadership_scenario' first.",
        }

    stakeholders = scenario.get("echoes", [])
    enriched_stakeholders: List[Dict[str, Any]] = []

    for sh in stakeholders:
        # Echoes use 'tier' and 'domain_principles' which map to 'seniority' and 'principles'
        tier = sh.get("tier", 3)
        if tier == 1:
            seniority = "C-Suite"
        elif tier == 2:
            seniority = "VP"
        else:
            seniority = "Director"

        sh["seniority"] = seniority
        sh["principles"] = sh.get("domain_principles", [])

        patterns = _BEHAVIORAL_PATTERNS.get(seniority, _BEHAVIORAL_PATTERNS["Senior"])

        enriched = {
            **sh,
            "behavioral_patterns": patterns,
            "alignment_drivers": _compute_alignment_drivers(sh),
            "resistance_triggers": _compute_resistance_triggers(sh),
            "coalition_affinity": _compute_coalition_affinity(sh, stakeholders),
        }
        enriched_stakeholders.append(enriched)

    # Store enriched map in session state
    tool_context.state["stakeholder_map"] = enriched_stakeholders

    logger.info(
        "map_stakeholders: enriched %d stakeholders for scenario %s",
        len(enriched_stakeholders),
        scenario.get("id", "unknown"),
    )

    return {
        "status": "success",
        "stakeholder_count": len(enriched_stakeholders),
        "stakeholder_map": enriched_stakeholders,
    }


def _compute_alignment_drivers(stakeholder: Dict[str, Any]) -> List[str]:
    """Determine what drives a stakeholder toward alignment.

    Uses case-insensitive substring matching against domain_principles,
    which contain full LEADERSHIP_PRINCIPLES strings like
    "Strategic Foresight & Vision Cascading".
    """
    principles_lower = [p.lower() for p in stakeholder.get("principles", [])]
    drivers = []

    for p in principles_lower:
        if "foresight" in p or "vision" in p:
            drivers.append("Sees transformation as opportunity for advancement")
        if "resource" in p or "allocation" in p or "optimization" in p:
            drivers.append("Clear financial justification and budget discipline")
        if "empowerment" in p or "distributed leadership" in p:
            drivers.append("Evidence that people are protected during change")
        if "ethical" in p or "governance" in p:
            drivers.append("Regulatory alignment and risk documentation")
        if "resilience" in p or "agility" in p or "adaptive" in p:
            drivers.append("Operational improvements and reduced friction")
        if "stakeholder" in p or "communication" in p or "engagement" in p:
            drivers.append("Inclusive communication and transparent process")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_drivers = []
    for d in drivers:
        if d not in seen:
            seen.add(d)
            unique_drivers.append(d)

    return unique_drivers or ["Demonstrated competence and clear communication"]


def _compute_resistance_triggers(stakeholder: Dict[str, Any]) -> List[str]:
    """Determine what triggers resistance from a stakeholder.

    Uses case-insensitive substring matching against domain_principles.
    """
    principles_lower = [p.lower() for p in stakeholder.get("principles", [])]
    triggers = []

    for p in principles_lower:
        if "resource" in p or "allocation" in p or "optimization" in p:
            triggers.append("Unbudgeted expenditures or unclear ROI")
        if "empowerment" in p or "distributed leadership" in p:
            triggers.append("Perceived threat to job security or team stability")
        if "ethical" in p or "governance" in p:
            triggers.append("Regulatory uncertainty or governance gaps")
        if "resilience" in p or "agility" in p:
            triggers.append("Changes that threaten organizational stability")
        if "stakeholder" in p or "communication" in p or "engagement" in p:
            triggers.append("Lack of transparency or exclusion from decisions")
        if "foresight" in p or "vision" in p:
            triggers.append("Strategic misalignment or unclear long-term direction")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_triggers = []
    for t in triggers:
        if t not in seen:
            seen.add(t)
            unique_triggers.append(t)

    return unique_triggers or ["Lack of transparency or rushed decision-making"]


def _compute_coalition_affinity(
    stakeholder: Dict[str, Any],
    all_stakeholders: List[Dict[str, Any]],
) -> List[str]:
    """Identify which other stakeholders this one naturally aligns with.

    Uses 'role' as the unique identifier (echo templates use 'role',
    not 'stakeholder_id') and 'domain_principles' for overlap detection.
    """
    my_principles = set(stakeholder.get("domain_principles", stakeholder.get("principles", [])))
    my_role = stakeholder.get("role", "")
    affinities = []

    for other in all_stakeholders:
        other_role = other.get("role", "")
        if not other_role or other_role == my_role:
            continue
        other_principles = set(other.get("domain_principles", other.get("principles", [])))
        overlap = my_principles & other_principles
        if overlap:
            affinities.append(f"{other_role} (shared: {', '.join(overlap)})")

    return affinities[:3]  # Top 3 natural allies
