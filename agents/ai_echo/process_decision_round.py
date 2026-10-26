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

"""process_decision_round — the core per-tick tool for AI Echo agents.

Direct analogue of the runner's ``process_tick`` tool.  Called once per
decision round by the Echo to update its organizational state (sentiment,
resistance, stance, affinity) based on the participant's decision and the
current simulation context.

This module is pure logic — no ADK agent construction.  All functions are
independently testable with mock state dicts.
"""

import logging
from typing import Any

from agents.ai_echo.initialization import sentiment_to_stance

logger = logging.getLogger(__name__)

StateLike = Any


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
    if decision_tags is None:
        decision_tags = {}
    if resource_state is None:
        resource_state = {}

    role = state.get("role", "unknown")
    tier = state.get("tier", 3)

    # --- Check for defection/alignment terminal states ---
    if state.get("defected", False):
        return _terminal_result(state, tick, "defected", inner_thought, 10.0)
    if state.get("aligned", False):
        return _terminal_result(state, tick, "aligned", inner_thought, 0.0)

    # --- Update principle affinity based on decision ---
    affinity = list(state.get("principle_affinity", [0.5] * 7))
    receptivity = state.get("communication_receptivity", 0.7)
    principles_applied = decision_tags.get("principles_applied", [])
    principles_neglected = decision_tags.get("principles_neglected", [])
    principles = state.get(
        "_principles",
        [
            "Strategic Foresight & Vision Cascading",
            "Adaptive Decision-Making & Agility",
            "Stakeholder Engagement & Inclusive Communication",
            "Ethical Leadership & Governance",
            "Empowerment & Cultivating Distributed Leadership",
            "Organizational Resilience & Change Agility",
            "Resource Optimization & Strategic Allocation",
        ],
    )

    domain_principles = set(state.get("domain_principles", []))

    for i, principle in enumerate(principles):
        if i >= len(affinity):
            break
        if principle in principles_applied:
            affinity[i] = min(1.0, affinity[i] + 0.03 * receptivity)
        elif principle in principles_neglected:
            affinity[i] = max(0.0, affinity[i] - 0.02 * (1.0 - receptivity))

    state["principle_affinity"] = [round(a, 4) for a in affinity]

    # --- Update sentiment ---
    sentiment = state.get("sentiment", 0.0)

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
    old_stance = state.get("stance", "neutral")
    if old_stance == "hostile" and sentiment > -0.4:
        breach_count = len(state.get("_contradiction_history", []))
        recovery_penalty = 0.03 * min(breach_count, 5)
        sentiment -= recovery_penalty

    # Contradiction alert causes extra sentiment drop
    if contradiction_alert:
        sentiment -= 0.1

    # Resource health affects sentiment
    owb = resource_state.get("owb", {}).get("current", 70) if resource_state else 70
    if owb < 30:
        sentiment -= 0.02  # Low organizational will erodes all sentiment

    # --- Apply inter-stakeholder influence deltas ---
    # The influence network propagation (computed in advance_tick) produces
    # per-role sentiment nudges representing peer pressure from connected
    # stakeholders. Scale by receptivity so closed-minded echoes resist.
    if influence_deltas:
        role = state.get("role", "unknown")
        delta = influence_deltas.get(role, 0.0)
        if delta:
            sentiment += delta * receptivity

    sentiment = max(-1.0, min(1.0, sentiment))
    state["sentiment"] = round(sentiment, 4)

    # --- Update resistance ---
    resistance = state.get("resistance", 0.3)
    if domain_neglected:
        resistance = min(1.0, resistance + 0.05 * len(domain_neglected))
    elif domain_applied:
        resistance = max(0.0, resistance - 0.03 * len(domain_applied))
    state["resistance"] = round(resistance, 4)

    # --- Update urgency ---
    urgency = state.get("urgency", 0.2)
    if owb < 40:
        urgency = min(1.0, urgency + 0.05)
    elif owb >= 60:
        urgency = max(0.0, urgency - 0.03)
    state["urgency"] = round(urgency, 4)

    # --- Check for hidden agenda activation (BEFORE stance update) ---
    agenda_event = None
    agenda_trigger = state.get("agenda_trigger_tick")
    if (
        state.get("has_hidden_agenda")
        and agenda_trigger is not None
        and tick >= agenda_trigger
        and not state.get("agenda_revealed")
    ):
        state["agenda_revealed"] = True
        sentiment -= 0.15
        state["sentiment"] = round(max(-1.0, sentiment), 4)
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
    if agenda_event:
        notable_event = f"{role} reveals hidden agenda and shifts to {state['stance']}"
    elif new_stance != old_stance:
        notable_event = f"{role} shifts from {old_stance} to {new_stance}"
    elif state.get("defected"):
        notable_event = f"{role} has defected from the organization"
    elif state.get("aligned"):
        notable_event = f"{role} is now fully aligned with leadership"

    # --- Determine autonomous action ---
    autonomous_action = None
    resource_claims = []

    if state.get("defected"):
        autonomous_action = f"{role} withdraws all support and actively undermines initiatives"
    elif resistance > 0.7:
        autonomous_action = f"{role} escalates concerns to external stakeholders"
    elif urgency > 0.7 and owb < 40:
        dp_str = ", ".join(sorted(domain_principles))
        autonomous_action = f"{role} demands emergency intervention on {dp_str}"
        resource_claims = ["lau"]  # Claims leadership attention
    elif sentiment > 0.5 and domain_applied:
        autonomous_action = f"{role} champions the initiative internally"

    # --- Build result ---
    result = {
        "tick": tick,
        "role": role,
        "tier": tier,
        "sentiment": state["sentiment"],
        "resistance": state["resistance"],
        "influence": state.get("influence", 0.5),
        "urgency": state["urgency"],
        "stance": state["stance"],
        "defected": state.get("defected", False),
        "aligned": state.get("aligned", False),
        "echo_status": state.get("echo_status", "active"),
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
    return {
        "tick": tick,
        "role": state.get("role", "unknown"),
        "tier": state.get("tier", 3),
        "sentiment": state.get("sentiment", 0.0),
        "resistance": state.get("resistance", 0.0),
        "influence": state.get("influence", 0.5),
        "urgency": state.get("urgency", 0.0),
        "stance": state.get("stance", "neutral"),
        "defected": state.get("defected", False),
        "aligned": state.get("aligned", False),
        "echo_status": status,
        "principle_affinity": state.get("principle_affinity", [0.5] * 7),
        "inner_thought": inner_thought,
        "wall_severity": wall_severity,
        "notable_event": None,
        "autonomous_action": None,
        "resource_claims": [],
    }
