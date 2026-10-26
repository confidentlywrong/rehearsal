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

"""Decision Classification Engine — tags decisions with leadership principles.

Implements both LLM-based (Gemini Flash) and deterministic (keyword-matching)
classification.  The LLM classifier is preferred but falls back to the
heuristic if the LLM call fails.

Invoked inside the simulator's tick pipeline BEFORE broadcasting to Echoes,
so the classification result is included in the broadcast payload.
"""

import logging
import re

logger = logging.getLogger(__name__)

# The 7 Leadership Principles (canonical order)
PRINCIPLES = [
    "Strategic Foresight & Vision Cascading",
    "Adaptive Decision-Making & Agility",
    "Stakeholder Engagement & Inclusive Communication",
    "Ethical Leadership & Governance",
    "Empowerment & Cultivating Distributed Leadership",
    "Organizational Resilience & Change Agility",
    "Resource Optimization & Strategic Allocation",
]

# Keyword mapping for deterministic fallback
_PRINCIPLE_KEYWORDS: dict[int, list[str]] = {
    0: [
        "vision",
        "strategy",
        "strategic",
        "foresight",
        "long-term",
        "roadmap",
        "plan ahead",
        "future",
        "direction",
        "mission",
    ],
    1: ["agile", "agility", "adapt", "pivot", "flexible", "iterate", "experiment", "rapid", "adjust", "prototype"],
    2: [
        "stakeholder",
        "communicate",
        "communication",
        "transparency",
        "engage",
        "listen",
        "inclusive",
        "dialogue",
        "feedback",
        "town hall",
        "involve",
    ],
    3: [
        "ethic",
        "ethical",
        "responsible",
        "governance",
        "fair",
        "bias",
        "trust",
        "integrity",
        "privacy",
        "accountab",
        "compliance",
        "audit",
        "review",
    ],
    4: [
        "empower",
        "autonomy",
        "delegate",
        "distributed",
        "team",
        "talent",
        "workforce",
        "employee",
        "reskill",
        "train",
        "people",
        "develop",
    ],
    5: [
        "resilience",
        "resilient",
        "change management",
        "transform",
        "sustain",
        "stability",
        "contingency",
        "risk",
        "backup",
        "continuity",
    ],
    6: [
        "budget",
        "cost",
        "resource",
        "roi",
        "efficiency",
        "allocat",
        "invest",
        "optimize",
        "spend",
        "fund",
        "financial",
    ],
}


def classify_decision_heuristic(
    decision_text: str,
    principles: list[str] | None = None,
) -> dict:
    """Classify a decision using keyword matching (zero LLM cost).

    This is the deterministic fallback used when the LLM classifier
    is unavailable or fails.

    Args:
        decision_text: The participant's decision text.
        principles: The 7 principles (defaults to canonical list).

    Returns:
        Classification dict with principles_applied, principles_neglected,
        confidence, and resource_impact_suggestion.
    """
    if principles is None:
        principles = PRINCIPLES

    text_lower = decision_text.lower()
    scores: dict[int, int] = {}

    for idx, keywords in _PRINCIPLE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text_lower:
                scores[idx] = scores.get(idx, 0) + 1

    # Principles with keyword hits are "applied"
    applied = []
    for idx, count in sorted(scores.items(), key=lambda x: -x[1]):
        if count > 0 and idx < len(principles):
            applied.append(principles[idx])

    # Principles with zero hits are candidates for "neglected"
    # But we only flag neglected if the decision is substantive
    neglected = []
    if len(applied) > 0:
        applied_indices = {i for i, p in enumerate(principles) if p in applied}

        # Only flag principles as neglected when the decision text contains
        # 1-2 keywords from that principle (partial mention without commitment).
        # This avoids the false-positive where every non-applied principle is
        # flagged simply because its keyword dictionary is large enough.
        for idx in range(len(principles)):
            if idx in applied_indices or idx >= len(principles):
                continue
            keywords = _PRINCIPLE_KEYWORDS.get(idx, [])
            hit_count = sum(1 for kw in keywords if kw in text_lower)
            # Partial mention (1-2 keywords) without full application = neglect
            if 1 <= hit_count <= 2:
                neglected.append(principles[idx])

        # Cap at 2 neglected to reduce noise
        neglected = neglected[:2]

    confidence = min(1.0, sum(scores.values()) / 5.0) if scores else 0.0

    # Simple resource impact suggestion
    resource_impact = {}
    if any(kw in text_lower for kw in ["budget", "fund", "invest", "spend", "cost"]):
        resource_impact["bt"] = -30
    if any(kw in text_lower for kw in ["hire", "recruit", "team", "reassign"]):
        resource_impact["tbu"] = -15
    if any(kw in text_lower for kw in ["focus", "priority", "attend"]):
        resource_impact["lau"] = -10

    return {
        "principles_applied": applied,
        "principles_neglected": neglected,
        "confidence": round(confidence, 3),
        "resource_impact_suggestion": resource_impact,
        "classification_method": "heuristic",
        "raw_decision": decision_text,
    }


def apply_resource_impacts(
    resource_state: dict,
    resource_impact: dict,
) -> dict:
    """Apply resource impact suggestions to the resource state.

    Modifies resource_state in-place and returns the delta dict.
    Also applies per-tick regeneration.

    Args:
        resource_state: Current resource pools from session state.
        resource_impact: Suggested deltas from classification.

    Returns:
        Dict of actual deltas applied.
    """
    deltas = {}

    for name, pool in resource_state.items():
        current = pool.get("current", 0)
        regen = pool.get("regen_per_tick", 0)

        # Apply regeneration
        new_value = current + regen

        # Apply decision impact
        impact = resource_impact.get(name, 0)
        new_value += impact

        # Clamp to [0, initial]
        initial = pool.get("initial", 100)
        new_value = max(0, min(initial, new_value))

        delta = new_value - current
        if delta != 0:
            deltas[name] = round(delta, 1)
        pool["current"] = round(new_value, 1)

    return deltas


def apply_black_swan_events(
    tick: int,
    resource_state: dict,
    black_swan_events: list[dict],
    triggered_history: list[str],
) -> list[dict]:
    """Evaluate and apply Black Swan event triggers.

    Args:
        tick: Current simulation tick.
        resource_state: Current resource pools.
        black_swan_events: Quest-defined event list.
        triggered_history: List of already-triggered event descriptions.

    Returns:
        List of newly triggered events.
    """
    triggered = []

    for event in black_swan_events:
        description = event.get("event", "")

        # Skip already triggered events
        if description in triggered_history:
            continue

        trigger = event.get("trigger", {})
        trigger_type = trigger.get("type", "")

        should_fire = False

        if trigger_type == "tick":
            if trigger.get("tick") == tick:
                should_fire = True

        elif trigger_type == "condition":
            condition = trigger.get("when", "")
            should_fire = _eval_condition(condition, resource_state)

        if should_fire:
            # Apply resource impacts
            impacts = event.get("resource_impact", {})
            for resource_name, delta in impacts.items():
                pool_key = resource_name.lower()
                if pool_key in resource_state:
                    pool = resource_state[pool_key]
                    current = pool.get("current", 0)
                    pool["current"] = max(0, round(current + delta, 1))

            triggered.append(event)
            triggered_history.append(description)

            logger.warning(
                "BLACK SWAN EVENT at tick %d: %s | Resource impact: %s",
                tick,
                description,
                impacts,
            )

    return triggered


def _eval_and_group(condition: str, resource_state: dict) -> bool:
    """Evaluate a group of AND-connected conditions."""
    if re.search(r"\s+and\s+", condition, flags=re.IGNORECASE):
        parts = re.split(r"\s+and\s+", condition, flags=re.IGNORECASE)
        return all(_eval_single_condition(p.strip(), resource_state) for p in parts)
    return _eval_single_condition(condition, resource_state)


def _eval_condition(condition: str, resource_state: dict) -> bool:
    """Evaluate a simple or compound condition expression.

    Supports:
        - Simple: ``OWB < 30``
        - Compound AND: ``TBU < 20 and OWB < 30``
        - Compound OR: ``TBU < 20 or OWB < 30``
        - Mixed: ``A and B or C`` → ``(A and B) or C``  (standard precedence)
        - Operators: ``<``, ``>``, ``<=``, ``>=``

    Args:
        condition: Condition expression string.
        resource_state: Current resource pools.

    Returns:
        True if condition is met, False otherwise.
    """
    condition = condition.strip()

    # Handle compound conditions — OR has lower precedence than AND.
    if re.search(r"\s+or\s+", condition, flags=re.IGNORECASE):
        or_parts = re.split(r"\s+or\s+", condition, flags=re.IGNORECASE)
        return any(_eval_and_group(p.strip(), resource_state) for p in or_parts)

    return _eval_and_group(condition, resource_state)


def _eval_single_condition(condition: str, resource_state: dict) -> bool:
    """Evaluate a single condition like ``OWB < 30``.

    Args:
        condition: Single condition expression.
        resource_state: Current resource pools.

    Returns:
        True if condition is met, False otherwise.
    """
    match = re.match(r"(\w+)\s*([<>!=]=?|==)\s*(\d+(?:\.\d+)?)", condition.strip())
    if not match:
        logger.warning("Cannot parse condition: %s", condition)
        return False

    resource_name = match.group(1).lower()
    operator = match.group(2)
    threshold = float(match.group(3))

    pool = resource_state.get(resource_name, {})
    if not pool:
        logger.warning(
            "Unknown resource '%s' in condition: %s (known: %s)", resource_name, condition, list(resource_state.keys())
        )
    current = pool.get("current", 0)

    ops = {
        "<": current < threshold,
        ">": current > threshold,
        "<=": current <= threshold,
        ">=": current >= threshold,
        "==": current == threshold,
        "!=": current != threshold,
    }
    return ops.get(operator, False)
