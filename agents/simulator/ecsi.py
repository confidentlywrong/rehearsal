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

"""Emergent Critical Section Identifier (ECSI).

Deterministic post-tick analysis that detects organizational resource
contention and emits Synchronicity Alerts.  No LLM dependency — pure
Python pattern matching on the aggregated tick snapshot.

Follows the ``compute_tick_traffic()`` pattern in
``agents/simulator/skills/advancing-race-ticks/tools.py:1014-1038``:
runs as a pure function after tick aggregation, appending results to
the snapshot.

Detection rules:
    1. Resource Contention — 2+ Echo actions claim same resource pool
    2. Threshold Breach — resource crosses critical threshold
    3. Sentiment Cascade — 3+ Echoes shift to hostile stance
    4. Hidden Agenda Trigger — Echo's agenda_trigger_tick reached
"""

import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

StateLike = Any


def _finite_float(value: Any, default: float) -> float:
    """Coerce numeric value to finite float, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: Any, default: int = 0) -> int:
    """Coerce non-bool int/float to finite int, falling back to default."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return int(value) if math.isfinite(num) else default


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
    alerts: list[SynchronicityAlert] = []
    if not isinstance(resources, dict):
        return alerts
    for name, pool in resources.items():
        if not isinstance(pool, dict) or not name:
            continue
        current = _finite_float(pool.get("current"), 100.0)
        threshold = _finite_float(pool.get("critical_threshold"), 20.0)

        if current <= threshold:
            # Check if this is a new breach (wasn't below threshold before)
            was_above = True
            if isinstance(previous_resources, dict):
                prev_pool = previous_resources.get(name)
                if isinstance(prev_pool, dict):
                    prev_val = _finite_float(prev_pool.get("current"), 100.0)
                    was_above = prev_val > threshold

            if was_above:
                severity = "critical" if current <= threshold * 0.5 else "warning"
                name_upper = str(name).upper()
                alerts.append(
                    SynchronicityAlert(
                        severity=severity,
                        alert_type="threshold",
                        description=(
                            f"THRESHOLD BREACH: {name_upper} has dropped to {current:.0f} "
                            f"(critical threshold: {threshold:.0f}). "
                            f"Immediate attention required to prevent organizational failure."
                        ),
                        affected_resources=[name_upper],
                        recommended_guardrails=[
                            f"Prioritize {name_upper} recovery in next decision",
                            "Consider reallocating from other resource pools",
                            f"Engage stakeholders most affected by {name_upper} depletion",
                        ],
                    )
                )

    return alerts


def _detect_sentiment_cascade(
    echo_sentiments: dict[str, dict],
) -> list[SynchronicityAlert]:
    """Detect when 3+ Echoes are in hostile stance simultaneously."""
    if not isinstance(echo_sentiments, dict):
        return []
    hostile_echoes: list[str] = []
    for echo_id, data in echo_sentiments.items():
        if not isinstance(data, dict):
            continue
        stance = data.get("stance", "neutral")
        if stance == "hostile":
            hostile_echoes.append(str(data.get("role") or echo_id))

    alerts: list[SynchronicityAlert] = []
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
    if not isinstance(echo_actions, list):
        return []
    resource_claims: dict[str, list[str]] = {}

    for action in echo_actions:
        if not isinstance(action, dict):
            continue
        claimed_resources = action.get("resource_claims") or []
        if not isinstance(claimed_resources, (list, tuple, set)):
            continue
        echo_role = str(action.get("role") or "unknown")
        seen_for_action: set[str] = set()
        for resource in claimed_resources:
            if not isinstance(resource, str) or not resource.strip():
                continue
            res_norm = resource.strip()
            if res_norm in seen_for_action:
                continue
            seen_for_action.add(res_norm)
            resource_claims.setdefault(res_norm, []).append(echo_role)

    alerts: list[SynchronicityAlert] = []
    for resource, claimants in resource_claims.items():
        if len(claimants) >= 2:
            res_upper = resource.upper()
            alerts.append(
                SynchronicityAlert(
                    severity="warning",
                    alert_type="contention",
                    description=(
                        f"RESOURCE CONTENTION: {len(claimants)} stakeholders "
                        f"({', '.join(claimants)}) are competing for {res_upper}. "
                        f"Without mediation, this will create organizational deadlock."
                    ),
                    conflicting_initiatives=claimants,
                    affected_resources=[res_upper],
                    recommended_guardrails=[
                        f"Arbitrate {res_upper} allocation between competing parties",
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
    if not isinstance(echo_configs, list):
        return []
    alerts: list[SynchronicityAlert] = []
    for config in echo_configs:
        if not isinstance(config, dict):
            continue
        role = str(config.get("role") or "unknown")
        if role in revealed_agendas:
            continue

        has_agenda = bool(config.get("has_hidden_agenda", False))
        trigger_tick = config.get("agenda_trigger_tick")
        trigger_num: float | None = None
        if isinstance(trigger_tick, (int, float)) and not isinstance(trigger_tick, bool):
            try:
                cand = float(trigger_tick)
                if math.isfinite(cand):
                    trigger_num = cand
            except (TypeError, ValueError, OverflowError):
                trigger_num = None
        if trigger_num is not None and has_agenda and current_tick >= trigger_num:
            role_label = role.replace("_", " ").title()
            alerts.append(
                SynchronicityAlert(
                    severity="critical",
                    alert_type="agenda",
                    description=(
                        f"HIDDEN AGENDA REVEALED: {role_label} "
                        f"has revealed a previously undisclosed agenda. "
                        f"Their true priorities may conflict with the stated organizational goals."
                    ),
                    conflicting_initiatives=[role],
                    recommended_guardrails=[
                        f"Engage {role_label} in direct, transparent dialogue",
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
    safe_snapshot = tick_snapshot if isinstance(tick_snapshot, dict) else {}
    safe_quest = quest_config if isinstance(quest_config, dict) else {}
    raw_tick = safe_snapshot.get("tick")
    current_tick = _finite_int(raw_tick, 0)
    alerts: list[SynchronicityAlert] = []

    # 1. Threshold breaches
    # NOTE: previous_resources is read from the prior tick's snapshot.
    # detect_contention is called before the current tick's snapshot is
    # appended to state["tick_snapshots"], so snapshots[-1] is normally T-1
    # (fall back to snapshots[-2] if the caller already appended T).
    previous_resources = None
    if state is not None:
        snapshots = state.get("tick_snapshots") or []
        if isinstance(snapshots, list) and snapshots:
            last_snap = snapshots[-1] if isinstance(snapshots[-1], dict) else None
            if last_snap is not None and last_snap.get("tick") != current_tick:
                prev_snap = last_snap
            elif len(snapshots) >= 2 and isinstance(snapshots[-2], dict):
                prev_snap = snapshots[-2]
            else:
                prev_snap = None
            if prev_snap is not None:
                org_state = prev_snap.get("organizational_state")
                if isinstance(org_state, dict):
                    previous_resources = org_state.get("resources")

    alerts.extend(_detect_threshold_breaches(resource_state, previous_resources))

    # 2. Sentiment cascade
    echo_sentiments = safe_snapshot.get("echo_sentiments") or {}
    alerts.extend(_detect_sentiment_cascade(echo_sentiments))

    # 3. Resource contention (from Echo autonomous actions this tick)
    echo_actions = safe_snapshot.get("echo_actions") or []
    alerts.extend(_detect_resource_contention(echo_actions))

    # 4. Hidden agenda triggers
    # Check state first (populated by inject_quest_into_state), then quest_config
    echo_configs = []
    if state is not None:
        echo_configs = state.get("echo_configs") or []
    if not echo_configs:
        echo_configs = safe_quest.get("echoes") or safe_quest.get("echo_configs") or []

    raw_revealed = state.get("revealed_agendas") if state is not None else None
    revealed = {r for r in raw_revealed if isinstance(r, str)} if isinstance(raw_revealed, (list, tuple, set)) else set()
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
