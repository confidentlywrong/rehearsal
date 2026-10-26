# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
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

"""Redis side-channel for passing large simulation data between agents.

Stores route GeoJSON, traffic assessment, quest config, persona status
counts, and runner count in Redis hashes so that the planner and
simulator agents can exchange large payloads WITHOUT going through
the LLM (which truncates/corrupts large JSON).

Key pattern: ``simdata:{simulation_id}``
TTL: 7200 seconds (2 hours).
"""

import json
import logging

from agents.utils.redis_pool import get_shared_redis_client

logger = logging.getLogger(__name__)

_KEY_PREFIX = "simdata:"
_TTL_SECONDS = 7200


async def store_simulation_data(
    simulation_id: str,
    route_geojson: dict | None = None,
    traffic_assessment: dict | None = None,
    quest_config: dict | None = None,
    persona_status_counts: dict | None = None,
    persona_avg_progress: dict | None = None,
    runner_count: int | None = None,
    echo_voice_log: list | None = None,
    decision_log: list | None = None,
    contradiction_log: list | None = None,
    route_friction_map: dict | None = None,
    terminal_echo_ids: dict | None = None,
    vitals_trend: list | None = None,
    alert_history: list | None = None,
    resources: dict | None = None,
    triggered_black_swans: list | None = None,
) -> bool:
    """Store simulation data in a Redis hash for the given simulation.

    Args:
        simulation_id: Unique simulation identifier.
        route_geojson: Route GeoJSON FeatureCollection (optional).
        traffic_assessment: Traffic assessment dict (optional).
        quest_config: QuestConfig-compatible dict (optional, leadership flow).
        persona_status_counts: Persona → status → count mapping (optional).
        persona_avg_progress: Persona → avg alignment progress % (optional).
        runner_count: Final reconciled runner count (optional).
        echo_voice_log: Per-tick stakeholder voice log (optional).
        decision_log: Per-tick leader decision log (optional).
        contradiction_log: Truth Engine contradiction records (optional).
        route_friction_map: Role → wall_severity mapping (optional).
        terminal_echo_ids: Aligned/defected stakeholder IDs (optional).
        vitals_trend: Per-tick velocity, water, distance (optional).
        alert_history: Contention alert history (optional).
        resources: Final capital/resource values (optional).
        triggered_black_swans: List of triggered events (optional).

    Returns:
        True on success, False if Redis is unavailable or on error.
    """
    r = get_shared_redis_client()
    if r is None:
        logger.warning("store_simulation_data: no Redis client available")
        return False

    key = f"{_KEY_PREFIX}{simulation_id}"
    mapping: dict[str, str] = {}

    if route_geojson is not None:
        mapping["route_geojson"] = json.dumps(route_geojson)
    if traffic_assessment is not None:
        mapping["traffic_assessment"] = json.dumps(traffic_assessment)
    if quest_config is not None:
        mapping["quest_config"] = json.dumps(quest_config)
    if persona_status_counts is not None:
        mapping["persona_status_counts"] = json.dumps(persona_status_counts)
    if persona_avg_progress is not None:
        mapping["persona_avg_progress"] = json.dumps(persona_avg_progress)
    if runner_count is not None:
        mapping["runner_count"] = json.dumps(runner_count)
    if echo_voice_log is not None:
        mapping["echo_voice_log"] = json.dumps(echo_voice_log)
    if decision_log is not None:
        mapping["decision_log"] = json.dumps(decision_log)
    if contradiction_log is not None:
        mapping["contradiction_log"] = json.dumps(contradiction_log)
    if route_friction_map is not None:
        mapping["route_friction_map"] = json.dumps(route_friction_map)
    if terminal_echo_ids is not None:
        mapping["terminal_echo_ids"] = json.dumps(terminal_echo_ids)
    if vitals_trend is not None:
        mapping["vitals_trend"] = json.dumps(vitals_trend)
    if alert_history is not None:
        mapping["alert_history"] = json.dumps(alert_history)
    if resources is not None:
        mapping["resources"] = json.dumps(resources)
    if triggered_black_swans is not None:
        mapping["triggered_black_swans"] = json.dumps(triggered_black_swans)

    if not mapping:
        return True  # Nothing to store

    try:
        # L15 fix: Use pipeline for atomic HSET + EXPIRE to prevent
        # orphaned keys if the process crashes between the two calls.
        pipe = r.pipeline(transaction=False)  # type: ignore[misc]
        pipe.hset(key, mapping=mapping)  # type: ignore[misc]
        pipe.expire(key, _TTL_SECONDS)  # type: ignore[misc]
        await pipe.execute()  # type: ignore[misc]
        return True
    except Exception:
        logger.warning("store_simulation_data: Redis error for %s", simulation_id, exc_info=True)
        return False


async def load_simulation_data(simulation_id: str) -> dict:
    """Load simulation data from the Redis hash.

    Args:
        simulation_id: Unique simulation identifier.

    Returns:
        Dict with keys ``route_geojson``, ``traffic_assessment``,
        ``quest_config``, ``persona_status_counts``, ``runner_count``,
        ``echo_voice_log``, ``decision_log``, ``contradiction_log``,
        ``route_friction_map``, ``terminal_echo_ids``, ``vitals_trend``,
        ``alert_history``, ``resources``, and ``triggered_black_swans``
        (parsed from JSON). Missing fields return None.
        Returns empty dict if Redis is unavailable or on error.
    """
    r = get_shared_redis_client()
    if r is None:
        return {}

    key = f"{_KEY_PREFIX}{simulation_id}"
    try:
        raw = await r.hgetall(key)  # type: ignore[misc]
    except Exception:
        logger.warning("load_simulation_data: Redis error for %s", simulation_id, exc_info=True)
        return {}

    if not raw:
        return {
            "route_geojson": None,
            "traffic_assessment": None,
            "quest_config": None,
            "persona_status_counts": None,
            "persona_avg_progress": None,
            "runner_count": None,
            "echo_voice_log": None,
            "decision_log": None,
            "contradiction_log": None,
            "route_friction_map": None,
            "terminal_echo_ids": None,
            "vitals_trend": None,
            "alert_history": None,
            "resources": None,
            "triggered_black_swans": None,
        }

    result: dict = {}
    for field_name in (
        "route_geojson",
        "traffic_assessment",
        "quest_config",
        "persona_status_counts",
        "persona_avg_progress",
        "runner_count",
        "echo_voice_log",
        "decision_log",
        "contradiction_log",
        "route_friction_map",
        "terminal_echo_ids",
        "vitals_trend",
        "alert_history",
        "resources",
        "triggered_black_swans",
    ):
        # L16 fix: Handle both bytes and string keys — depends on
        # whether the Redis client has decode_responses enabled.
        raw_value = raw.get(field_name) or raw.get(field_name.encode())
        if raw_value is not None:
            result[field_name] = json.loads(raw_value)
        else:
            result[field_name] = None

    return result


async def clear_simulation_data(simulation_id: str) -> bool:
    """Delete the simulation data hash key.

    Args:
        simulation_id: Unique simulation identifier.

    Returns:
        True on success, False if Redis is unavailable or on error.
    """
    r = get_shared_redis_client()
    if r is None:
        return False

    key = f"{_KEY_PREFIX}{simulation_id}"
    try:
        await r.delete(key)  # type: ignore[misc]
        return True
    except Exception:
        logger.warning("clear_simulation_data: Redis error for %s", simulation_id, exc_info=True)
        return False
