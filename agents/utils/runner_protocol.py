# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

"""Structured message protocol for runner agents.

Defines the contract between the simulator and runner agents. Both the
LLM-powered runner and the deterministic runner_autopilot consume these
messages. The simulator constructs them via serialize_runner_event().
"""

import json
import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agents.utils.sim_defaults import DEFAULT_RACE_DISTANCE_MI

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce numeric value to finite float, rejecting None, bool, NaN, and Inf."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: Any, default: int = 0) -> int:
    """Coerce numeric value to float-range int, rejecting None, bool, NaN, Inf, and OverflowError."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        try:
            num = float(value)
        except OverflowError:
            return default
        return value if math.isfinite(num) else default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return int(round(num)) if math.isfinite(num) else default


def _strip_nonfinite(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/Inf) with None for RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_nonfinite(v) for v in obj]
    return obj


class RunnerEventType(str, Enum):
    """Event types the simulator sends to runner agents."""

    START_GUN = "start_gun"
    CROWD_BOOST = "crowd_boost"
    DISTANCE_UPDATE = "distance_update"
    HYDRATION_STATION = "hydration_station"
    TICK = "tick"
    DECISION_ROUND = "decision_round"
    UNKNOWN = "unknown"


@dataclass
class RunnerEvent:
    """A parsed runner event with typed event and payload data."""

    event: RunnerEventType
    data: dict = field(default_factory=dict)


def parse_runner_event(text: str) -> RunnerEvent:
    """Parse a JSON message string into a RunnerEvent.

    Returns RunnerEvent with UNKNOWN type for malformed or unrecognized input.
    """
    if not text or not text.strip():
        return RunnerEvent(event=RunnerEventType.UNKNOWN)

    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return RunnerEvent(event=RunnerEventType.UNKNOWN, data={"raw": text})

    if not isinstance(obj, dict) or "event" not in obj:
        return RunnerEvent(event=RunnerEventType.UNKNOWN, data=obj if isinstance(obj, dict) else {"raw": text})

    event_str = obj.get("event")
    data = {k: v for k, v in obj.items() if k != "event"}
    try:
        event_type = RunnerEventType(event_str)
    except (ValueError, TypeError):
        return RunnerEvent(event=RunnerEventType.UNKNOWN, data=data)

    return RunnerEvent(event=event_type, data=data)


def build_tick_event(
    tick: int,
    max_ticks: int,
    total_race_hours: float,
    race_distance_mi: float = DEFAULT_RACE_DISTANCE_MI,
    collector_buffer_key: str = "",
    runner_count: int | None = None,
    persona: str | None = None,
) -> RunnerEvent:
    """Build a TICK RunnerEvent with computed race timing data.

    Tick 0 is the initialization tick: ``minutes_per_tick=0`` so runners
    report their initial velocity at distance=0 without advancing.
    Movement ticks (1+) each cover ``minutes_per_tick`` of simulated time.

    Args:
        collector_buffer_key: Redis key for direct-write collection. When set,
            runners RPUSH their process_tick results directly to this key,
            enabling direct-write aggregation for faster tick processing.
    """
    safe_tick = _finite_int(tick, 0)
    safe_max_ticks = _finite_int(max_ticks, 0)
    safe_total_hours = _finite_float(total_race_hours, 0.0)
    safe_distance_mi = _finite_float(race_distance_mi, DEFAULT_RACE_DISTANCE_MI)

    try:
        raw_mpt = (safe_total_hours * 60.0) / safe_max_ticks if safe_max_ticks > 0 else 0.0
    except OverflowError:
        raw_mpt = 0.0
    minutes_per_tick = raw_mpt if math.isfinite(raw_mpt) else 0.0

    # Tick 0 is the initialization tick: no simulated time passes.
    raw_tick_minutes = 0.0 if safe_tick == 0 else minutes_per_tick
    tick_minutes = raw_tick_minutes if math.isfinite(raw_tick_minutes) else 0.0

    try:
        raw_elapsed = safe_tick * minutes_per_tick
    except OverflowError:
        raw_elapsed = 0.0
    elapsed_minutes = raw_elapsed if math.isfinite(raw_elapsed) else 0.0

    data: dict = {
        "tick": safe_tick,
        "max_ticks": safe_max_ticks,
        "minutes_per_tick": tick_minutes,
        "elapsed_minutes": elapsed_minutes,
        "race_distance_mi": safe_distance_mi,
    }
    if collector_buffer_key:
        data["collector_buffer_key"] = collector_buffer_key
    if runner_count is not None:
        data["runner_count"] = _finite_int(runner_count, 0)
    if persona is not None:
        data["persona"] = persona
    return RunnerEvent(event=RunnerEventType.TICK, data=data)


def serialize_runner_event(event: RunnerEvent) -> str:
    """Serialize a RunnerEvent to a strict RFC 8259 JSON string for transmission."""
    obj = {"event": event.event.value, **event.data}
    try:
        return json.dumps(obj, allow_nan=False)
    except ValueError:
        return json.dumps(_strip_nonfinite(obj), allow_nan=False)


def build_decision_round_event(
    tick: int,
    max_ticks: int,
    decision_text: str,
    decision_tags: dict,
    resource_state: dict,
    contradiction_alert: dict | None,
    collector_buffer_key: str = "",
) -> RunnerEvent:
    """Build a DECISION_ROUND RunnerEvent for AI Echoes."""
    data: dict = {
        "tick": _finite_int(tick, 0),
        "max_ticks": _finite_int(max_ticks, 0),
        "decision_text": decision_text,
        "decision_tags": decision_tags,
        "resource_state": resource_state,
        "contradiction_alert": contradiction_alert,
    }
    if collector_buffer_key:
        data["collector_buffer_key"] = collector_buffer_key
    return RunnerEvent(event=RunnerEventType.DECISION_ROUND, data=data)
