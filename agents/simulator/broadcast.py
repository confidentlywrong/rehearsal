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

"""Redis broadcast helper for the simulator agent.

Provides a single function to publish serialized RunnerEvent data to the
``simulation:{simulation_id}:broadcast`` Redis channel with the standard
broadcast envelope.
"""

import asyncio
import json
import logging
import time
import uuid

from agents.utils.redis_pool import get_shared_redis_client
from agents.utils import simulation_registry
from agents.utils.simulation_registry import _session_key
from agents.utils import tenant_keys

logger = logging.getLogger(__name__)

_READINESS_POLL_INTERVAL = 0.5  # seconds between registry polls


class BroadcastError(RuntimeError):
    """Raised when Redis broadcast or registry polling fails."""


def _scoped_broadcast_channel(simulation_id: str, user_id: str | None = None) -> str:
    """Tenant-scoped name for the per-simulation broadcast channel.

    SaaS-aware (Phase 2.5b): in SAAS_MODE=on this returns
    ``{TENANT_KEY_PREFIX}:{org}:simulation:{sim}:broadcast``
    where ``org`` is resolved from explicit ``user_id``,
    ``simulation_registry.user_scope(...)``, or ``DISPATCHER_USER_ID``.
    In SAAS_MODE=off the helper collapses to the legacy
    ``simulation:{sim}:broadcast`` byte-for-byte so existing tests and
    the single-tenant local stack are unchanged.
    """
    eff_user = simulation_registry._resolve_registry_user(user_id, require_when_saas=False)
    return tenant_keys.key_for_user(eff_user, "simulation", simulation_id, "broadcast")


async def wait_for_runners_ready(
    session_ids: list[str],
    simulation_id: str,
    timeout_seconds: float = 60,
) -> int:
    """Poll the simulation registry until all runner session IDs are registered.

    The dispatchers process spawn_agent events asynchronously via BLPOP and
    register each session in Redis (SETEX simreg:session:{id}) with the
    value set to the simulation_id.  This function blocks until all
    ``session_ids`` have been registered **for this specific simulation**,
    or until ``timeout_seconds`` elapses.

    Args:
        session_ids: Runner session IDs to wait for.
        simulation_id: The current simulation ID -- values must match this.
        timeout_seconds: Max seconds to wait before proceeding anyway.

    Returns the number of runners confirmed registered for this simulation.
    """
    expected = len(session_ids) if session_ids else 0
    if expected == 0:
        return 0

    r = get_shared_redis_client()
    if r is None:
        logger.warning("wait_for_runners_ready: no Redis client, skipping spawn readiness gate")
        return 0

    safe_timeout = timeout_seconds if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0 else 0.0
    deadline = time.monotonic() + safe_timeout
    registered = 0
    any_poll_succeeded = False
    poll_failures = 0
    last_poll_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            keys = [_session_key(sid) for sid in session_ids]
            values = await r.mget(*keys)  # type: ignore[misc]
            registered = sum(
                1
                for v in (values or ())
                if v is not None and (v == simulation_id or (isinstance(v, bytes) and v.decode(errors="replace") == simulation_id))
            )
            any_poll_succeeded = True
        except Exception as e:
            poll_failures += 1
            last_poll_error = e
            logger.warning("wait_for_runners_ready: registry poll failed, retrying: %s", e)

        if registered >= expected:
            elapsed = max(0.0, safe_timeout - max(0.0, deadline - time.monotonic()))
            logger.info(
                "wait_for_runners_ready: all %d runners registered (%.1fs)",
                expected,
                elapsed,
            )
            return registered

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        logger.info(
            "wait_for_runners_ready: %d/%d runners registered, waiting...",
            registered,
            expected,
        )
        await asyncio.sleep(min(_READINESS_POLL_INTERVAL, remaining))

    if not any_poll_succeeded and poll_failures > 0 and last_poll_error is not None:
        logger.error(
            "wait_for_runners_ready: registry poll failed persistently across %d attempt(s): %s",
            poll_failures,
            last_poll_error,
            exc_info=True,
        )
        raise BroadcastError(
            f"wait_for_runners_ready failed persistently across {poll_failures} attempt(s): {last_poll_error}"
        ) from last_poll_error

    logger.warning(
        "wait_for_runners_ready: timeout after %.0fs — %d/%d runners registered, proceeding",
        safe_timeout,
        registered,
        expected,
    )
    return registered


async def publish_to_runners(
    data: str,
    simulation_id: str,
    exclude_runner_ids: list[str] | None = None,
    user_id: str | None = None,
) -> None:
    """Publish a serialized event string to runners via Redis broadcast.

    Wraps ``data`` in the standard broadcast envelope and publishes to
    the scoped ``simulation:{simulation_id}:broadcast`` Redis channel.

    Args:
        data: A JSON string (typically from ``serialize_runner_event()``).
        simulation_id: Simulation ID for channel scoping.
        exclude_runner_ids: Optional list of runner session IDs the
            dispatcher should skip when fanning out this broadcast.
        user_id: Optional explicit tenant user_id (falls back to
            ``simulation_registry.user_scope`` / ``DISPATCHER_USER_ID``).
    """
    payload: dict[str, object] = {"data": data}
    if exclude_runner_ids:
        payload["exclude_runner_ids"] = exclude_runner_ids
    eff_user = simulation_registry._resolve_registry_user(user_id, require_when_saas=False)
    envelope_dict: dict[str, object] = {
        "type": "broadcast",
        "eventId": str(uuid.uuid4()),
        "payload": payload,
        "simulation_id": simulation_id,
    }
    if tenant_keys._saas_mode_enabled() and eff_user:
        envelope_dict["user_id"] = eff_user
    envelope = json.dumps(envelope_dict)

    channel = _scoped_broadcast_channel(simulation_id, user_id=user_id)

    try:
        r = get_shared_redis_client()
        if r is None:
            return
        await r.publish(channel, envelope)
    except Exception as e:
        logger.error("publish_to_runners: Redis publish failed: %s", e, exc_info=True)
        raise


async def publish_end_simulation(simulation_id: str, user_id: str | None = None) -> None:
    """Publish an end_simulation event so dispatchers remove runner sessions.

    This is called by stop_race_collector after the race completes.  Each
    dispatcher instance that owns sessions for this simulation will remove
    them from active_sessions, clean up the simulation registry, and
    unsubscribe from the scoped Redis channel.

    Args:
        simulation_id: The simulation whose runners should be cleaned up.
        user_id: Optional explicit tenant user_id (falls back to
            ``simulation_registry.user_scope`` / ``DISPATCHER_USER_ID``).
    """
    eff_user = simulation_registry._resolve_registry_user(user_id, require_when_saas=False)
    envelope_dict: dict[str, object] = {
        "type": "end_simulation",
        "simulation_id": simulation_id,
        "eventId": str(uuid.uuid4()),
    }
    if tenant_keys._saas_mode_enabled() and eff_user:
        envelope_dict["user_id"] = eff_user
    envelope = json.dumps(envelope_dict)
    channel = _scoped_broadcast_channel(simulation_id, user_id=user_id)
    try:
        r = get_shared_redis_client()
        if r is None:
            return
        await r.publish(channel, envelope)
        logger.info("publish_end_simulation: published to %s", channel)
    except Exception as e:
        logger.error("publish_end_simulation: Redis publish failed: %s", e, exc_info=True)
        raise
