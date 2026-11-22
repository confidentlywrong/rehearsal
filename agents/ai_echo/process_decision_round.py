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

"""process_decision_round — the core per-tick tool for AI Echo agents.

Direct analogue of the runner's ``process_tick`` tool.  Called once per
decision round by the Echo to update its organizational state (sentiment,
resistance, stance, affinity) based on the participant's decision and the
current simulation context.

This module is pure logic — no ADK agent construction.  All functions are
independently testable with mock state dicts.
"""

import logging
import math
from typing import Any

from agents.ai_echo.initialization import ACRONYMS, format_role_display, sentiment_to_stance

logger = logging.getLogger(__name__)

StateLike = Any

__all__ = [
    "ACRONYMS",
    "format_role_display",
    "process_decision_round",
    "sentiment_to_stance",
]


def _finite_float(value: object, default: float = 0.0) -> float:
    """Coerce numeric value to finite float, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: object, default: int = 0) -> int:
    """Coerce int/float value to int, rejecting bool/None/NaN/Inf."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        try:
            num = float(value)
        except OverflowError:
            return default
        return value if math.isfinite(num) else default
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(num):
        return default
    return int(num)


def _str_list(value: object) -> list[str]:
    """Normalize an iterable of principle names to list[str]."""
    if isinstance(value, (list, tuple, set)):
        return [x for x in value if isinstance(x, str)]
    return []


def process_decision_round(
    state: StateLike,
    tick: int,
    decision_text: str = "",
    decision_tags: dict | None = None,
    resource_state: dict | None = None,
    contradiction_alert: dict | None = None,
    inner_thought: str = "",
    wall_severity: float = 5.0,
    collector_buffer_key: str = "",
    influence_deltas: dict | None = None,
) -> dict:
    """Process one decision round for an AI Echo.

    Called by both LLM-powered (Tier 1/2) and deterministic (Tier 3) Echoes.

    Args:
        state: ADK session state (dict-like).
        tick: Current simulation tick number.
        decision_text: The participant's decision text.
        decision_tags: Output from Decision Classification Engine.
        resource_state: Current global resource pools.
        contradiction_alert: If set, this Echo should challenge the participant.
        inner_thought: LLM-generated inner monologue (Tier 1 only).
        wall_severity: Dynamic route friction determined by the Echo LLM.
        collector_buffer_key: Redis key for direct-write collection.

    Returns:
        Result dict with updated Echo state for aggregation.
    """
    if not isinstance(decision_tags, dict):
        decision_tags = {}
    if not isinstance(resource_state, dict):
        resource_state = {}
    tick = _finite_int(tick, 0)
    wall_severity = max(0.0, min(10.0, _finite_float(wall_severity, 5.0)))
    if not isinstance(inner_thought, str):
        inner_thought = str(inner_thought) if inner_thought is not None else ""

    raw_role = state.get("role", "unknown")
    role = raw_role if isinstance(raw_role, str) and raw_role else "unknown"
    role_display = (
        (state.get("label") if isinstance(state.get("label"), str) else None)
        or (state.get("name") if isinstance(state.get("name"), str) else None)
        or format_role_display(role)
    )
    tier = _finite_int(state.get("tier", 3), 3)

    # --- Check for defection/alignment terminal states ---
    if state.get("defected", False):
        return _terminal_result(state, tick, "defected", inner_thought, 10.0)
    if state.get("aligned", False):
        return _terminal_result(state, tick, "aligned", inner_thought, 0.0)

    # --- Update principle affinity based on decision ---
    raw_aff = state.get("principle_affinity")
    affinity = (
        [max(0.0, min(1.0, _finite_float(a, 0.5))) for a in raw_aff]
        if isinstance(raw_aff, (list, tuple))
        else [0.5] * 7
    )
    receptivity = max(0.0, min(1.0, _finite_float(state.get("communication_receptivity", 0.7), 0.7)))
    principles_applied = _str_list(decision_tags.get("principles_applied"))
    principles_neglected = _str_list(decision_tags.get("principles_neglected"))
    raw_principles = state.get("_principles")
    principles = (
        _str_list(raw_principles)
        if isinstance(raw_principles, (list, tuple)) and len(raw_principles) > 0
        else [
            "Strategic Foresight & Vision Cascading",
            "Adaptive Decision-Making & Agility",
            "Stakeholder Engagement & Inclusive Communication",
            "Ethical Leadership & Governance",
            "Empowerment & Cultivating Distributed Leadership",
            "Organizational Resilience & Change Agility",
            "Resource Optimization & Strategic Allocation",
        ]
    )

    domain_principles = set(_str_list(state.get("domain_principles")))

    for i, principle in enumerate(principles):
        if i >= len(affinity):
            break
        if principle in principles_applied:
            affinity[i] = min(1.0, affinity[i] + 0.03 * receptivity)
        elif principle in principles_neglected:
            affinity[i] = max(0.0, affinity[i] - 0.02 * (1.0 - receptivity))

    state["principle_affinity"] = [round(a, 4) for a in affinity]

    # --- Update sentiment ---
    sentiment = max(-1.0, min(1.0, _finite_float(state.get("sentiment", 0.0), 0.0)))

    # Positive shift if domain principles are applied
    domain_applied = domain_principles & set(principles_applied)
    domain_neglected = domain_principles & set(principles_neglected)

    # Asymmetric trust dynamics (research-backed):
    # Trust builds slowly, erodes ~3x faster.
    TRUST_BUILD_RATE = 0.03
    TRUST_ERODE_RATE = 0.10

    if domain_applied:
        sentiment += TRUST_BUILD_RATE * len(domain_applied) * receptivity
    if domain_neglected:
        sentiment -= TRUST_ERODE_RATE * len(domain_neglected) * (1.0 - receptivity * 0.5)

    # Hysteresis: once hostile, harder to recover (trust debt)
    raw_old_stance = state.get("stance", "neutral")
    old_stance = raw_old_stance if isinstance(raw_old_stance, str) and raw_old_stance else "neutral"
    raw_chist = state.get("_contradiction_history")
    chist = list(raw_chist) if isinstance(raw_chist, list) else []
    if old_stance == "hostile" and sentiment > -0.4:
        breach_count = len(chist)
        recovery_penalty = 0.03 * min(breach_count, 5)
        sentiment -= recovery_penalty

    # Contradiction alert causes extra sentiment drop and accumulates trust debt
    if contradiction_alert:
        sentiment -= 0.1
        chist.append(contradiction_alert)
        state["_contradiction_history"] = chist

    # Resource health affects sentiment
    owb_pool = resource_state.get("owb")
    owb = _finite_float(owb_pool.get("current"), 70.0) if isinstance(owb_pool, dict) else 70.0
    if owb < 30:
        sentiment -= 0.02  # Low organizational will erodes all sentiment

    # --- Apply inter-stakeholder influence deltas ---
    # The influence network propagation (computed in advance_tick) produces
    # per-role sentiment nudges representing peer pressure from connected
    # stakeholders. Scale by receptivity so closed-minded echoes resist.
    if isinstance(influence_deltas, dict):
        delta = _finite_float(influence_deltas.get(role, 0.0), 0.0)
        if delta:
            sentiment += delta * receptivity

    sentiment = max(-1.0, min(1.0, sentiment))
    state["sentiment"] = round(sentiment, 4)

    # --- Update resistance ---
    resistance = max(0.0, min(1.0, _finite_float(state.get("resistance", 0.3), 0.3)))
    if domain_neglected:
        resistance = min(1.0, resistance + 0.05 * len(domain_neglected))
    elif domain_applied:
        resistance = max(0.0, resistance - 0.03 * len(domain_applied))
    state["resistance"] = round(resistance, 4)

    # --- Update urgency ---
    urgency = max(0.0, min(1.0, _finite_float(state.get("urgency", 0.2), 0.2)))
    if owb < 40:
        urgency = min(1.0, urgency + 0.05)
    elif owb >= 60:
        urgency = max(0.0, urgency - 0.03)
    state["urgency"] = round(urgency, 4)

    # --- Check for hidden agenda activation (BEFORE stance update) ---
    agenda_event = None
    raw_trigger = state.get("agenda_trigger_tick")
    agenda_trigger = (
        _finite_int(raw_trigger)
        if raw_trigger is not None and not isinstance(raw_trigger, bool)
        else None
    )
    if (
        state.get("has_hidden_agenda")
        and agenda_trigger is not None
        and tick >= agenda_trigger
        and not state.get("agenda_revealed")
    ):
        state["agenda_revealed"] = True
        sentiment = max(-1.0, min(1.0, sentiment - 0.15))
        state["sentiment"] = round(sentiment, 4)
        agenda_event = True

    # --- Update stance ---
    new_stance = sentiment_to_stance(sentiment)
    state["stance"] = new_stance

    # --- Coach role protection: never defects, never hostile ---
    is_coach = role == "executive_coach"
    if is_coach:
        sentiment = max(0.0, sentiment)
        state["sentiment"] = round(sentiment, 4)
        state["stance"] = sentiment_to_stance(sentiment)
        new_stance = state["stance"]

    # --- Check for defection (extreme negative sentiment + high resistance) ---
    if not is_coach and sentiment <= -0.8 and resistance >= 0.8:
        state["defected"] = True
        state["echo_status"] = "defected"

    # --- Check for alignment (high positive sentiment + low resistance) ---
    if not is_coach and sentiment >= 0.8 and resistance <= 0.1:
        state["aligned"] = True
        state["echo_status"] = "aligned"

    # --- Determine notable event ---
    notable_event = None
    if state.get("defected"):
        notable_event = f"{role_display} has defected from the organization"
    elif state.get("aligned"):
        notable_event = f"{role_display} is now fully aligned with leadership"
    elif agenda_event:
        notable_event = f"{role_display} reveals hidden agenda and shifts to {state['stance']}"
    elif new_stance != old_stance:
        notable_event = f"{role_display} shifts from {old_stance} to {new_stance}"

    # --- Determine autonomous action ---
    autonomous_action = None
    resource_claims = []

    if state.get("defected"):
        autonomous_action = f"{role_display} withdraws all support and actively undermines initiatives"
    elif resistance > 0.7:
        autonomous_action = f"{role_display} escalates concerns to external stakeholders"
    elif urgency > 0.7 and owb < 40:
        dp_str = ", ".join(sorted(domain_principles)) if domain_principles else "core domain principles"
        autonomous_action = f"{role_display} demands emergency intervention on {dp_str}"
        resource_claims = ["lau"]  # Claims leadership attention
    elif sentiment > 0.5 and domain_applied:
        autonomous_action = f"{role_display} champions the initiative internally"

    # --- Build result ---
    influence = _finite_float(state.get("influence", 0.5), 0.5)
    raw_status = state.get("echo_status", "active")
    echo_status = raw_status if isinstance(raw_status, str) and raw_status else "active"
    result = {
        "tick": tick,
        "role": role,
        "role_display": role_display,
        "tier": tier,
        "sentiment": state["sentiment"],
        "resistance": state["resistance"],
        "influence": influence,
        "urgency": state["urgency"],
        "stance": state["stance"],
        "defected": bool(state.get("defected", False)),
        "aligned": bool(state.get("aligned", False)),
        "echo_status": echo_status,
        "principle_affinity": state["principle_affinity"],
        "inner_thought": inner_thought,
        "wall_severity": wall_severity,
        "notable_event": notable_event,
        "autonomous_action": autonomous_action,
        "resource_claims": resource_claims,
    }

    logger.info(
        "Echo %s tick %d: sentiment=%.2f stance=%s resistance=%.2f",
        role,
        tick,
        state["sentiment"],
        state["stance"],
        state["resistance"],
    )

    return result


def _terminal_result(state: StateLike, tick: int, status: str, inner_thought: str, wall_severity: float) -> dict:
    """Build result for a terminal-state Echo (defected or aligned)."""
    safe_tick = _finite_int(tick, 0)
    raw_role = state.get("role", "unknown")
    role = raw_role if isinstance(raw_role, str) and raw_role else "unknown"
    role_display = (
        (state.get("label") if isinstance(state.get("label"), str) else None)
        or (state.get("name") if isinstance(state.get("name"), str) else None)
        or format_role_display(role)
    )
    raw_stance = state.get("stance", "neutral")
    stance = raw_stance if isinstance(raw_stance, str) and raw_stance else "neutral"
    raw_aff = state.get("principle_affinity")
    principle_affinity = (
        [_finite_float(a, 0.5) for a in raw_aff]
        if isinstance(raw_aff, (list, tuple))
        else [0.5] * 7
    )
    safe_thought = inner_thought if isinstance(inner_thought, str) else (str(inner_thought) if inner_thought is not None else "")
    safe_status = status if isinstance(status, str) and status else "active"
    return {
        "tick": safe_tick,
        "role": role,
        "role_display": role_display,
        "tier": _finite_int(state.get("tier", 3), 3),
        "sentiment": _finite_float(state.get("sentiment", 0.0), 0.0),
        "resistance": _finite_float(state.get("resistance", 0.0), 0.0),
        "influence": _finite_float(state.get("influence", 0.5), 0.5),
        "urgency": _finite_float(state.get("urgency", 0.0), 0.0),
        "stance": stance,
        "defected": bool(state.get("defected", False)),
        "aligned": bool(state.get("aligned", False)),
        "echo_status": safe_status,
        "principle_affinity": principle_affinity,
        "inner_thought": safe_thought,
        "wall_severity": _finite_float(wall_severity, 5.0),
        "notable_event": None,
        "autonomous_action": None,
        "resource_claims": [],
    }
