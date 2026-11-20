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

"""Decision Classification Engine — tags decisions with leadership principles.

Implements deterministic keyword-matching classification (zero LLM cost).

Invoked inside the simulator's tick pipeline BEFORE broadcasting to Echoes,
so the classification result is included in the broadcast payload.
"""

import logging
import math
import re
from typing import Any

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce numeric value to finite float, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default

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


# Explicit neglect / override cues per principle for deterministic fallback
_NEGLECT_KEYWORDS: dict[int, list[str]] = {
    0: ["short-term only", "no roadmap", "without a plan", "ignore long-term", "ignore strategy"],
    1: ["refuse to adapt", "no iteration", "freeze scope", "inflexible", "reject experiment"],
    2: [
        "without consulting",
        "no consultation",
        "top-down mandate",
        "skip communication",
        "ignore feedback",
        "behind closed doors",
        "unilateral",
        "without stakeholder",
    ],
    3: [
        "skip review",
        "skip ethical",
        "bypass audit",
        "bypass governance",
        "ignore ethics",
        "waive compliance",
        "cut corners",
        "without oversight",
        "skip bias",
    ],
    4: ["micromanage", "centralize all", "no training", "strip autonomy", "without empowering"],
    5: ["no backup", "no contingency", "ignore risk", "single point of failure"],
    6: ["unlimited spend", "blank check", "ignore budget", "unfunded"],
}

_NEGATION_PREFIX_RE = re.compile(
    r"\b(?:without|skip|skipping|bypass|bypassing|ignore|ignoring|no|waive|waiving|cancel|cancelling)\s+(?:\w+\s+){0,2}$"
)


def _has_negated_keyword(text_lower: str, keyword: str) -> bool:
    """Return True if *keyword* only appears preceded by a negation/bypass verb."""
    pos = text_lower.find(keyword)
    if pos == -1:
        return False
    found_any = False
    while pos != -1:
        found_any = True
        prefix_window = text_lower[max(0, pos - 40) : pos]
        if not _NEGATION_PREFIX_RE.search(prefix_window):
            # Found at least one non-negated occurrence
            return False
        pos = text_lower.find(keyword, pos + len(keyword))
    return found_any


def classify_decision_heuristic(
    decision_text: str,
    principles: list[str] | None = None,
) -> dict:
    """Classify a decision using deterministic keyword matching (zero LLM cost).

    Args:
        decision_text: The participant's decision text.
        principles: The 7 principles (defaults to canonical list).

    Returns:
        Classification dict with principles_applied, principles_neglected,
        confidence, and resource_impact_suggestion.
    """
    if principles is None:
        principles = PRINCIPLES

    safe_text = decision_text if isinstance(decision_text, str) else ""
    text_lower = safe_text.lower()
    scores: dict[int, int] = {}
    explicitly_neglected: set[int] = set()

    for idx, neg_phrases in _NEGLECT_KEYWORDS.items():
        if idx < len(principles) and any(phrase in text_lower for phrase in neg_phrases):
            explicitly_neglected.add(idx)

    for idx, keywords in _PRINCIPLE_KEYWORDS.items():
        if idx >= len(principles):
            continue
        for keyword in keywords:
            if keyword in text_lower:
                if _has_negated_keyword(text_lower, keyword):
                    explicitly_neglected.add(idx)
                else:
                    scores[idx] = scores.get(idx, 0) + 1

    # Remove explicitly neglected principles from positive scores
    for idx in explicitly_neglected:
        scores.pop(idx, None)

    max_hits = max(scores.values(), default=0)

    # When a substantive decision strongly commits to primary principle(s)
    # (max_hits >= 3), a weak 1-keyword mention of another principle represents
    # superficial/passing mention without commitment -> neglected.
    min_applied_hits = 2 if max_hits >= 3 else 1

    applied: list[str] = []
    applied_indices: set[int] = set()
    for idx, count in sorted(scores.items(), key=lambda x: -x[1]):
        if count >= min_applied_hits and idx < len(principles):
            applied.append(principles[idx])
            applied_indices.add(idx)

    neglected: list[str] = []
    # 1. Explicit neglect / bypass cues always take priority
    for idx in sorted(explicitly_neglected):
        if idx < len(principles) and idx not in applied_indices:
            neglected.append(principles[idx])

    # 2. Partial mention (1 hit) without commitment when primary focus is strong
    if max_hits >= 3:
        for idx, count in sorted(scores.items(), key=lambda x: x[1]):
            if idx not in applied_indices and idx < len(principles) and 1 <= count < min_applied_hits:
                if principles[idx] not in neglected:
                    neglected.append(principles[idx])

    # Cap at 2 neglected to reduce noise
    neglected = neglected[:2]

    total_signal = sum(scores.values()) + len(explicitly_neglected)
    confidence = min(1.0, total_signal / 5.0) if total_signal else 0.0

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
        "raw_decision": safe_text,
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
    if not isinstance(resource_state, dict):
        return {}
    safe_impact = resource_impact if isinstance(resource_impact, dict) else {}
    deltas: dict[str, float] = {}

    for name, pool in resource_state.items():
        if not isinstance(pool, dict):
            continue
        current = _finite_float(pool.get("current"), 0.0)
        regen = _finite_float(pool.get("regen_per_tick"), 0.0)

        # Apply regeneration
        new_value = current + regen

        # Apply decision impact
        impact = _finite_float(safe_impact.get(name), 0.0)
        new_value += impact

        # Clamp to [0, initial]
        initial = max(0.0, _finite_float(pool.get("initial"), 100.0))
        new_value = max(0.0, min(initial, new_value))
        if not math.isfinite(new_value):
            new_value = 0.0

        raw_delta = new_value - current
        if math.isfinite(raw_delta):
            if raw_delta != 0:
                deltas[name] = round(raw_delta, 1)
        else:
            deltas[name] = 0.0
        rounded_new = round(new_value, 1)
        pool["current"] = rounded_new if math.isfinite(rounded_new) else 0.0

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
    if not isinstance(black_swan_events, list) or not isinstance(resource_state, dict):
        return []
    safe_history = triggered_history if isinstance(triggered_history, list) else []
    triggered: list[dict] = []

    for event in black_swan_events:
        if not isinstance(event, dict):
            continue
        description = str(event.get("event") or "")

        # Skip already triggered events
        if description in safe_history:
            continue

        trigger = event.get("trigger") or {}
        if not isinstance(trigger, dict):
            continue
        trigger_type = trigger.get("type", "")

        should_fire = False

        if trigger_type == "tick":
            raw_trigger_tick = trigger.get("tick")
            if not isinstance(raw_trigger_tick, bool) and isinstance(raw_trigger_tick, (int, float)):
                try:
                    trigger_tick_f = float(raw_trigger_tick)
                    should_fire = math.isfinite(trigger_tick_f) and raw_trigger_tick == tick
                except (TypeError, ValueError, OverflowError):
                    should_fire = False

        elif trigger_type == "condition":
            condition = trigger.get("when", "")
            should_fire = _eval_condition(condition, resource_state)

        if should_fire:
            # Apply resource impacts
            impacts = event.get("resource_impact") or {}
            if isinstance(impacts, dict):
                for resource_name, raw_delta in impacts.items():
                    if not isinstance(resource_name, str):
                        continue
                    pool_key = resource_name.lower()
                    pool = resource_state.get(pool_key)
                    if isinstance(pool, dict):
                        current = _finite_float(pool.get("current"), 0.0)
                        delta = _finite_float(raw_delta, 0.0)
                        updated = current + delta
                        pool["current"] = max(0.0, round(updated, 1)) if math.isfinite(updated) else 0.0

            triggered.append(event)
            safe_history.append(description)

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
    if not isinstance(condition, str) or not isinstance(resource_state, dict):
        return False
    condition = condition.strip()
    if not condition:
        return False

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

    pool = resource_state.get(resource_name)
    if not isinstance(pool, dict) or not pool:
        logger.warning(
            "Unknown resource '%s' in condition: %s (known: %s)", resource_name, condition, list(resource_state.keys())
        )
        return False
    current = _finite_float(pool.get("current"), 0.0)

    ops = {
        "<": current < threshold,
        ">": current > threshold,
        "<=": current <= threshold,
        ">=": current >= threshold,
        "==": current == threshold,
        "!=": current != threshold,
    }
    return ops.get(operator, False)
