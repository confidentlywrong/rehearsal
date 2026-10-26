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

"""Emergent Critical Section Identifier (ECSI).

Deterministic post-tick analysis that detects organizational resource
contention and emits Synchronicity Alerts.  No LLM dependency — pure
Python pattern matching on the aggregated tick snapshot.

Follows the ``compute_tick_traffic()`` pattern in
``agents/simulator/skills/advancing-race-ticks/tools.py:310-338``:
runs as a pure function after tick aggregation, appending results to
the snapshot.

Detection rules:
    1. Resource Contention — 2+ Echo actions claim same resource pool
    2. Threshold Breach — resource crosses critical threshold
    3. Sentiment Cascade — 3+ Echoes shift to hostile stance
    4. Hidden Agenda Trigger — Echo's agenda_trigger_tick reached
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

StateLike = Any


@dataclass
class SynchronicityAlert:
    """A detected organizational race condition or contention event."""

    alert_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    severity: str = "warning"  # "warning" or "critical"
    alert_type: str = ""  # contention, threshold, cascade, agenda
    description: str = ""
    conflicting_initiatives: list[str] = field(default_factory=list)
    recommended_guardrails: list[str] = field(default_factory=list)
    affected_resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "severity": self.severity,
            "alert_type": self.alert_type,
            "description": self.description,
            "conflicting_initiatives": self.conflicting_initiatives,
            "recommended_guardrails": self.recommended_guardrails,
            "affected_resources": self.affected_resources,
        }


def _detect_threshold_breaches(
    resources: dict[str, dict],
    previous_resources: dict[str, dict] | None,
) -> list[SynchronicityAlert]:
    """Detect when resources cross their critical thresholds."""
    alerts = []
    for name, pool in resources.items():
        current = pool.get("current", 0)
        threshold = pool.get("critical_threshold", 20)

        if current <= threshold:
            # Check if this is a new breach (wasn't below threshold before)
            was_above = True
            if previous_resources and name in previous_resources:
                prev_val = previous_resources[name].get("current", 0)
                was_above = prev_val > threshold

            if was_above:
                severity = "critical" if current <= threshold * 0.5 else "warning"
                alerts.append(
                    SynchronicityAlert(
                        severity=severity,
                        alert_type="threshold",
                        description=(
                            f"THRESHOLD BREACH: {name.upper()} has dropped to {current:.0f} "
                            f"(critical threshold: {threshold:.0f}). "
                            f"Immediate attention required to prevent organizational failure."
                        ),
                        affected_resources=[name.upper()],
                        recommended_guardrails=[
                            f"Prioritize {name.upper()} recovery in next decision",
                            "Consider reallocating from other resource pools",
                            f"Engage stakeholders most affected by {name.upper()} depletion",
                        ],
                    )
                )

    return alerts


def _detect_sentiment_cascade(
    echo_sentiments: dict[str, dict],
) -> list[SynchronicityAlert]:
    """Detect when 3+ Echoes are in hostile stance simultaneously."""
    hostile_echoes = []
    for echo_id, data in echo_sentiments.items():
        stance = data.get("stance", "neutral")
        if stance == "hostile":
            hostile_echoes.append(data.get("role", echo_id))

    alerts = []
    if len(hostile_echoes) >= 3:
        alerts.append(
            SynchronicityAlert(
                severity="critical",
                alert_type="cascade",
                description=(
                    f"SENTIMENT CASCADE: {len(hostile_echoes)} stakeholders have reached "
                    f"hostile stance ({', '.join(hostile_echoes)}). "
                    f"Organizational consensus is fracturing."
                ),
                conflicting_initiatives=[f"Hostile: {role}" for role in hostile_echoes],
                recommended_guardrails=[
                    "Initiate direct dialogue with most influential hostile stakeholder",
                    "Consider a strategic pause on contentious initiatives",
                    "Address root causes of resistance before pushing forward",
                ],
            )
        )

    return alerts


def _detect_resource_contention(
    echo_actions: list[dict],
) -> list[SynchronicityAlert]:
    """Detect when 2+ Echo autonomous actions target the same resource pool."""
    resource_claims: dict[str, list[str]] = {}

    for action in echo_actions:
        claimed_resources = action.get("resource_claims", [])
        echo_role = action.get("role", "unknown")
        for resource in claimed_resources:
            resource_claims.setdefault(resource, []).append(echo_role)

    alerts = []
    for resource, claimants in resource_claims.items():
        if len(claimants) >= 2:
            alerts.append(
                SynchronicityAlert(
                    severity="warning",
                    alert_type="contention",
                    description=(
                        f"RESOURCE CONTENTION: {len(claimants)} stakeholders "
                        f"({', '.join(claimants)}) are competing for {resource.upper()}. "
                        f"Without mediation, this will create organizational deadlock."
                    ),
                    conflicting_initiatives=claimants,
                    affected_resources=[resource.upper()],
                    recommended_guardrails=[
                        f"Arbitrate {resource.upper()} allocation between competing parties",
                        "Establish clear priority criteria for resource distribution",
                        "Consider creating a shared resource pool or phased allocation",
                    ],
                )
            )

    return alerts


def _detect_hidden_agenda_triggers(
    current_tick: int,
    echo_configs: list[dict],
    revealed_agendas: set[str],
) -> list[SynchronicityAlert]:
    """Detect when an Echo's hidden agenda trigger tick is reached."""
    alerts = []
    for config in echo_configs:
        role = config.get("role", "unknown")
        if role in revealed_agendas:
            continue

        has_agenda = config.get("has_hidden_agenda", False)
        trigger_tick = config.get("agenda_trigger_tick")

        if has_agenda and trigger_tick is not None and current_tick >= trigger_tick:
            alerts.append(
                SynchronicityAlert(
                    severity="critical",
                    alert_type="agenda",
                    description=(
                        f"HIDDEN AGENDA REVEALED: {role.replace('_', ' ').title()} "
                        f"has revealed a previously undisclosed agenda. "
                        f"Their true priorities may conflict with the stated organizational goals."
                    ),
                    conflicting_initiatives=[role],
                    recommended_guardrails=[
                        f"Engage {role.replace('_', ' ').title()} in direct, transparent dialogue",
                        "Reassess alignment assumptions with all stakeholders",
                        "Consider how this changes the power dynamics in ongoing initiatives",
                    ],
                )
            )

    return alerts


def detect_contention(
    tick_snapshot: dict,
    quest_config: dict,
    resource_state: dict,
    state: StateLike | None = None,
) -> list[SynchronicityAlert]:
    """Run all ECSI detection rules against the current tick state.

    Called from inside ``advance_tick`` after aggregation is complete.
    Reads the aggregated snapshot (O(1) data, not O(N) raw messages).

    Args:
        tick_snapshot: The aggregated tick data (status_counts, notable_events, etc.)
        quest_config: The quest configuration dict from session state.
        resource_state: Current resource pool values.
        state: Full session state for reading accumulators.

    Returns:
        List of SynchronicityAlert instances (may be empty).
    """
    current_tick = tick_snapshot.get("tick", 0)
    alerts: list[SynchronicityAlert] = []

    # 1. Threshold breaches
    # NOTE: previous_resources is read from tick_snapshots[-2] intentionally.
    # It is ONLY used for deduplication (was the resource already breached
    # last tick?), NOT for the threshold comparison itself. The live
    # resource_state parameter is always the current-tick value.
    previous_resources = None
    if state:
        snapshots = state.get("tick_snapshots", [])
        if len(snapshots) >= 2:
            prev_snap = snapshots[-2]
            previous_resources = prev_snap.get("organizational_state", {}).get("resources")

    alerts.extend(_detect_threshold_breaches(resource_state, previous_resources))

    # 2. Sentiment cascade
    echo_sentiments = tick_snapshot.get("echo_sentiments", {})
    alerts.extend(_detect_sentiment_cascade(echo_sentiments))

    # 3. Resource contention (from Echo autonomous actions this tick)
    echo_actions = tick_snapshot.get("echo_actions", [])
    alerts.extend(_detect_resource_contention(echo_actions))

    # 4. Hidden agenda triggers
    # Check state first (populated by inject_quest_into_state), then quest_config
    echo_configs = []
    if state is not None:
        echo_configs = state.get("echo_configs", [])
    if not echo_configs:
        echo_configs = quest_config.get("echoes", quest_config.get("echo_configs", []))

    revealed = set(state.get("revealed_agendas", [])) if state else set()
    agenda_alerts = _detect_hidden_agenda_triggers(current_tick, echo_configs, revealed)
    if agenda_alerts and state is not None:
        # Mark agendas as revealed so they don't fire again
        for alert in agenda_alerts:
            for role in alert.conflicting_initiatives:
                revealed.add(role)
        state["revealed_agendas"] = list(revealed)
    alerts.extend(agenda_alerts)

    if alerts:
        logger.info(
            "ECSI tick %d: %d alerts detected — %s",
            current_tick,
            len(alerts),
            ", ".join(f"{a.alert_type}:{a.severity}" for a in alerts),
        )

    return alerts
