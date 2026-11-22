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
import math
import os
from typing import Any

from agents.utils import simulation_registry
from agents.utils import tenant_keys
from agents.utils.redis_pool import get_shared_redis_client

logger = logging.getLogger(__name__)

_KEY_PREFIX = "simdata:"
_TTL_SECONDS = 7200
_ANONYMOUS_USER_IDS = frozenset(
    {"simulation", "simulation_user", "anonymous", "default", "user", "local-dev", ""}
)


def _simdata_key(simulation_id: str, user_id: str | None = None) -> str:
    """Resolve the Redis key for simulation side-channel data.

    When ``SAAS_MODE=off``, returns legacy ``simdata:{simulation_id}``.
    When ``SAAS_MODE=on``, requires a non-anonymous ``user_id`` (from
    explicit *user_id*, ``simulation_registry.get_current_user_id()``,
    or ``DISPATCHER_USER_ID``) and returns
    ``tenant_keys.key_for_user(uid, "simdata", simulation_id)``.
    """
    if not tenant_keys._saas_mode_enabled():
        return f"{_KEY_PREFIX}{simulation_id}"

    if not isinstance(simulation_id, str) or not simulation_id.strip():
        raise SimDataError("simulation_id must be a non-empty string when SAAS_MODE=on")

    if user_id is not None:
        if not isinstance(user_id, str):
            raise SimDataError(
                f"Authenticated user_id must be a string when SAAS_MODE=on (got {type(user_id).__name__})"
            )
        uid = user_id.strip()
    else:
        uid = simulation_registry.get_current_user_id()
        if not uid:
            uid = os.environ.get("DISPATCHER_USER_ID", "").strip()

    if not uid or uid.lower() in _ANONYMOUS_USER_IDS:
        raise SimDataError(
            f"Authenticated user_id is required for simdata when SAAS_MODE=on (got {uid!r})"
        )

    try:
        uid = tenant_keys._validate_component(uid, "user_id")
        return tenant_keys.key_for_user(uid, "simdata", simulation_id.strip())
    except ValueError as exc:
        raise SimDataError(f"Invalid simdata key component: {exc}") from exc


def _strip_nonfinite(obj: object) -> object:
    """Recursively replace non-finite floats (NaN/Inf) with None for RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_nonfinite(v) for v in obj]
    return obj


def _strict_json_dumps(obj: object, **kwargs: Any) -> str:
    """Serialize *obj* to strict RFC-8259 JSON, replacing non-finite floats with null."""
    try:
        return json.dumps(obj, allow_nan=False, **kwargs)
    except ValueError:
        return json.dumps(_strip_nonfinite(obj), allow_nan=False, **kwargs)


class SimDataError(RuntimeError):
    """Raised when configured Redis side-channel storage or serialization fails."""


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
    user_id: str | None = None,
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
        user_id: Explicit tenant user_id when ``SAAS_MODE=on`` (optional).

    Returns:
        True on success, False if Redis is unconfigured (``r is None``).

    Raises:
        SimDataError: When Redis is configured and serialization or pipeline execution fails,
            or when ``SAAS_MODE=on`` and tenant identity is missing/anonymous.
    """
    key = _simdata_key(simulation_id, user_id=user_id)
    r = get_shared_redis_client()
    if r is None:
        logger.warning("store_simulation_data: no Redis client available")
        return False
    mapping: dict[str, str] = {}

    fields = (
        ("route_geojson", route_geojson),
        ("traffic_assessment", traffic_assessment),
        ("quest_config", quest_config),
        ("persona_status_counts", persona_status_counts),
        ("persona_avg_progress", persona_avg_progress),
        ("runner_count", runner_count),
        ("echo_voice_log", echo_voice_log),
        ("decision_log", decision_log),
        ("contradiction_log", contradiction_log),
        ("route_friction_map", route_friction_map),
        ("terminal_echo_ids", terminal_echo_ids),
        ("vitals_trend", vitals_trend),
        ("alert_history", alert_history),
        ("resources", resources),
        ("triggered_black_swans", triggered_black_swans),
    )
    try:
        for fname, fval in fields:
            if fval is not None:
                mapping[fname] = _strict_json_dumps(fval, default=str)
    except Exception as exc:
        logger.error("store_simulation_data: serialization error for %s", simulation_id, exc_info=True)
        raise SimDataError(f"Failed to serialize simulation data for {simulation_id}: {exc}") from exc

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
    except Exception as exc:
        logger.error("store_simulation_data: Redis error for %s", simulation_id, exc_info=True)
        raise SimDataError(f"Failed to store simulation data for {simulation_id}: {exc}") from exc


async def load_simulation_data(simulation_id: str, user_id: str | None = None) -> dict:
    """Load simulation data from the Redis hash.

    Args:
        simulation_id: Unique simulation identifier.
        user_id: Explicit tenant user_id when ``SAAS_MODE=on`` (optional).

    Returns:
        Dict with keys ``route_geojson``, ``traffic_assessment``,
        ``quest_config``, ``persona_status_counts``, ``runner_count``,
        ``echo_voice_log``, ``decision_log``, ``contradiction_log``,
        ``route_friction_map``, ``terminal_echo_ids``, ``vitals_trend``,
        ``alert_history``, ``resources``, and ``triggered_black_swans``
        (parsed from JSON). Missing fields return None.
        Returns empty dict if Redis is unconfigured (``r is None``).

    Raises:
        SimDataError: When Redis is configured and ``hgetall`` fails,
            or when ``SAAS_MODE=on`` and tenant identity is missing/anonymous.
    """
    key = _simdata_key(simulation_id, user_id=user_id)
    r = get_shared_redis_client()
    if r is None:
        return {}
    try:
        raw = await r.hgetall(key)  # type: ignore[misc]
    except Exception as exc:
        logger.error("load_simulation_data: Redis error for %s", simulation_id, exc_info=True)
        raise SimDataError(f"Failed to load simulation data for {simulation_id}: {exc}") from exc

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
        field_bytes = field_name.encode()
        if field_name in raw:
            raw_value = raw[field_name]
        elif field_bytes in raw:
            raw_value = raw[field_bytes]
        else:
            raw_value = None
        if raw_value is not None:
            try:
                result[field_name] = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                logger.error(
                    "load_simulation_data: malformed JSON in field %s for %s",
                    field_name,
                    simulation_id,
                    exc_info=True,
                )
                result[field_name] = None
        else:
            result[field_name] = None

    return result


async def clear_simulation_data(simulation_id: str, user_id: str | None = None) -> bool:
    """Delete the simulation data hash key.

    Args:
        simulation_id: Unique simulation identifier.
        user_id: Explicit tenant user_id when ``SAAS_MODE=on`` (optional).

    Returns:
        True on success, False if Redis is unconfigured (``r is None``).

    Raises:
        SimDataError: When Redis is configured and ``delete`` fails,
            or when ``SAAS_MODE=on`` and tenant identity is missing/anonymous.
    """
    key = _simdata_key(simulation_id, user_id=user_id)
    r = get_shared_redis_client()
    if r is None:
        return False
    try:
        await r.delete(key)  # type: ignore[misc]
        return True
    except Exception as exc:
        logger.error("clear_simulation_data: Redis error for %s", simulation_id, exc_info=True)
        raise SimDataError(f"Failed to clear simulation data for {simulation_id}: {exc}") from exc
