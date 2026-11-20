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

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
import inspect
import json
import logging
import math
import re
import os
import random
import threading
import time
import uuid
import redis.asyncio as redis
from typing import Optional, Any
from opentelemetry import context as otel_context
from google.genai import types
import agents.utils.pulses as pulses_util
from agents.utils.redis_pool import get_shared_redis_client
from agents.utils import simulation_registry
from agents.utils import tenant_keys
from agents.utils import user_auth_context

logger = logging.getLogger(__name__)

NUM_SPAWN_SHARDS = 8

_ANONYMOUS_USER_IDS = frozenset({"simulation", "simulation_user", "anonymous", "default", "user"})


def _extract_payload_user_id(mapping: Any) -> tuple[str, bool]:
    """Extract a consistent tenant user_id from payload fields.

    Checks ``user_id``, ``acting_user_id``, and ``userId`` (safely ignoring
    non-string values). Returns ``(user_id, has_conflict)`` so callers can
    fail closed if multiple non-empty fields disagree.
    """
    if not isinstance(mapping, Mapping):
        return "", False
    ordered_values: list[str] = []
    for key in ("user_id", "acting_user_id", "userId"):
        raw = mapping.get(key)
        if isinstance(raw, str):
            cleaned = raw.strip()
            if cleaned:
                ordered_values.append(cleaned)
    if not ordered_values:
        return "", False
    distinct = set(ordered_values)
    if len(distinct) > 1:
        return "", True
    return ordered_values[0], False


def _resolve_event_tenant(
    data: Any,
    payload: Any,
    source_channel: str | bytes | None = None,
) -> tuple[str, Optional[str], bool]:
    """Resolve and validate ``(eff_user, channel_sim_id, ok)`` for an event.

    Enforces unconditional two-source ``user_id`` conflict checks across
    *payload* and *data*, parses ``source_channel`` when provided in
    ``SAAS_MODE=on``, cross-checks ``bound_user``, ``event_user``, and
    ``channel_user`` consistency, and fails closed in ``SAAS_MODE=on`` if
    the resulting tenant is empty, anonymous, or invalid.
    """
    payload_user, p_conflict = _extract_payload_user_id(payload)
    data_user, d_conflict = _extract_payload_user_id(data)
    if p_conflict or d_conflict or (payload_user and data_user and payload_user != data_user):
        logger.error("ORCHESTRATION_ERROR: conflicting user_id fields in event")
        return "", None, False

    event_user = payload_user or data_user
    bound_user = _dispatcher_user_id()
    saas_on = tenant_keys._saas_mode_enabled()

    channel_user = ""
    channel_sim_id: Optional[str] = None
    if source_channel is not None and saas_on:
        channel_user, channel_sim_id = tenant_keys.user_from_simulation_channel(source_channel)
        if not channel_user:
            logger.error(
                "ORCHESTRATION_ERROR: rejected unprefixed or malformed Pub/Sub channel %r in SAAS_MODE=on",
                source_channel,
            )
            return "", None, False

    if bound_user and event_user and event_user != bound_user:
        logger.error(
            "ORCHESTRATION_ERROR: cross-tenant event rejected (bound=%s event_user=%s)",
            bound_user,
            event_user,
        )
        return "", None, False
    if bound_user and channel_user and channel_user != bound_user:
        logger.error(
            "ORCHESTRATION_ERROR: cross-tenant channel event rejected (bound=%s channel_user=%s)",
            bound_user,
            channel_user,
        )
        return "", None, False
    if channel_user and event_user and channel_user != event_user:
        logger.error(
            "ORCHESTRATION_ERROR: mismatched channel vs payload user_id (channel=%s event=%s)",
            channel_user,
            event_user,
        )
        return "", None, False

    eff_user = event_user or channel_user or bound_user
    if saas_on:
        if not eff_user or eff_user.lower() in _ANONYMOUS_USER_IDS:
            logger.error(
                "ORCHESTRATION_ERROR: unauthenticated or anonymous event rejected in SAAS_MODE=on "
                "(event_user=%r channel_user=%r bound=%r)",
                event_user,
                channel_user,
                bound_user,
            )
            return "", None, False
        try:
            eff_user = tenant_keys._validate_component(eff_user, "user_id")
        except ValueError as exc:
            logger.error(
                "ORCHESTRATION_ERROR: invalid user_id %r in SAAS_MODE=on: %s",
                eff_user,
                exc,
            )
            return "", None, False

    return eff_user, channel_sim_id, True


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


def _dispatcher_user_id() -> str:
    """Return the user this dispatcher process is bound to (Phase 2.5b).

    In SaaS mode each agent type runs a dispatcher *per tenant*; the
    tenant is selected via ``DISPATCHER_USER_ID`` (set by the deployment
    layer: docker-compose, Cloud Run env, or the per-tenant Honcho
    profile). When unset (single-tenant local dev), the helper returns
    "" and every key/channel name collapses to today's legacy format
    byte-for-byte.
    """
    return os.environ.get("DISPATCHER_USER_ID", "").strip()


def _global_broadcast_channel() -> str:
    """Tenant-scoped name for the global ``simulation:broadcast`` channel.

    * SAAS_MODE=off → ``simulation:broadcast`` (legacy, byte-identical).
    * SAAS_MODE=on + DISPATCHER_USER_ID=user1 → ``u:user1:simulation:broadcast``.
    """
    eff_user = simulation_registry._resolve_registry_user(None, require_when_saas=False)
    return tenant_keys.key_for_user(eff_user, "simulation", "broadcast")


def _scoped_broadcast_channel(sim_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-scoped name for a per-simulation broadcast channel."""
    eff_user = simulation_registry._resolve_registry_user(user_id, require_when_saas=False)
    return tenant_keys.key_for_user(eff_user, "simulation", sim_id, "broadcast")


def _sim_scope_key(sim_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-scoped key for ``_simulation_subscriptions`` and ``_simulation_user_map``.

    In SAAS_MODE=on with a non-empty user_id, returns ``{prefix}:{sim_id}`` so
    concurrent tenants sharing the same ``sim_id`` do not collide. In
    SAAS_MODE=off or when user_id is empty/None, returns ``sim_id``.
    """
    if tenant_keys._saas_mode_enabled() and user_id:
        prefix = tenant_keys._key_prefix_for_user(user_id)
        if prefix:
            return f"{prefix}:{sim_id}"
    return sim_id


def spawn_queue_names(agent_type: str, num_shards: int = NUM_SPAWN_SHARDS) -> list[str]:
    """Return all shard queue names for an agent type.

    SaaS-aware (Phase 2.5b): when ``SAAS_MODE=on`` and
    ``DISPATCHER_USER_ID`` is set, queue names are prefixed with
    ``{ACCOUNT_KEY_PREFIX}:{user}:`` so the dispatcher only pops spawns
    intended for its own tenant. Off path is byte-identical to today.
    """
    user = _dispatcher_user_id()
    return [tenant_keys.key_for_user(user, "simulation", "spawns", agent_type, str(i)) for i in range(num_shards)]


class ThreadSafeSet:
    """A thread-safe set for tracking active sessions."""

    def __init__(self):
        self._set = set()
        self._lock = threading.Lock()

    def add(self, item):
        with self._lock:
            self._set.add(item)

    def discard(self, item):
        with self._lock:
            self._set.discard(item)

    def clear(self):
        with self._lock:
            self._set.clear()

    def __len__(self):
        with self._lock:
            return len(self._set)

    def __contains__(self, item):
        with self._lock:
            return item in self._set

    def copy(self):
        with self._lock:
            return self._set.copy()

    def __iter__(self):
        return iter(self.copy())


class ThreadSafeDict:
    """A thread-safe dict for session-to-simulation mapping.

    Accessed from the dispatcher background thread (writes on spawn,
    reads on broadcast) and from the main thread via remove_session().
    """

    def __init__(self):
        self._dict: dict[str, str] = {}
        self._lock = threading.Lock()

    def __setitem__(self, key, value):
        with self._lock:
            self._dict[key] = value

    def __getitem__(self, key):
        with self._lock:
            return self._dict[key]

    def get(self, key, default=None):
        with self._lock:
            return self._dict.get(key, default)

    def pop(self, key, *args):
        with self._lock:
            return self._dict.pop(key, *args)

    def clear(self):
        with self._lock:
            self._dict.clear()

    def items(self):
        with self._lock:
            return list(self._dict.items())

    def values(self):
        with self._lock:
            return list(self._dict.values())

    def __contains__(self, key):
        with self._lock:
            return key in self._dict

    def __len__(self):
        with self._lock:
            return len(self._dict)


class RedisOrchestratorDispatcher:
    """
    Listens to Redis pubsub channels for cross-agent broadcast messages and dispatches them to the local runner.

    Runs in a background thread to ensure it's always listening regardless of the ADK lifecycle.
    """

    def __init__(
        self,
        runner: Any,
        redis_url: Optional[str] = None,
        dispatch_mode: str = "subscriber",
        suppress_gateway_emission: bool = False,
    ):
        self.runner = runner
        self.dispatch_mode = dispatch_mode
        self.suppress_gateway_emission = suppress_gateway_emission

        # Try to discover agent and app names from runner
        self.agent_type = "unknown"
        self.allowed_authors = {"agent", "tool", "model"}
        if hasattr(runner, "app"):
            if hasattr(runner.app, "name"):
                self.agent_type = runner.app.name
                self.allowed_authors.add(runner.app.name)
            if hasattr(runner.app, "root_agent") and hasattr(runner.app.root_agent, "name"):
                self.allowed_authors.add(runner.app.root_agent.name)
        elif hasattr(runner, "app_name"):
            self.agent_type = runner.app_name
            self.allowed_authors.add(runner.app_name)

        logger.info(
            f"ORCHESTRATION_INIT: Dispatcher initialized for agent type: "
            f"{self.agent_type}, allowed: {self.allowed_authors}"
        )
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        self.active_sessions = ThreadSafeSet()
        self._seen_events: OrderedDict[str, None] = OrderedDict()  # M8: FIFO eviction
        self._background_tasks = set()  # Track to prevent GC issues
        self._active_session_tasks: dict[str, asyncio.Task] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self.session_simulation_map = ThreadSafeDict()
        self.session_user_map = ThreadSafeDict()
        self._simulation_user_map = ThreadSafeDict()
        self._simulation_subscriptions: set[str] = set()
        self._pubsub = None  # Active Redis pubsub instance for dynamic subscription
        self._run_semaphore: Optional[asyncio.Semaphore] = None

    def start(self):
        """Starts the background Redis listener in a dedicated thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()
        logger.info(f"ORCHESTRATION_LIFECYCLE: Thread started for {self.agent_type}")

    async def _cancel_all_tasks(self):
        """Internal helper to cancel all tasks in the current loop."""
        tasks = [t for t in asyncio.all_tasks(self._loop) if t is not asyncio.current_task()]
        if not tasks:
            return

        logger.debug(f"ORCHESTRATION_LIFECYCLE: Cancelling {len(tasks)} tasks...")
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self):
        """Stops the background Redis listener."""
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            # Simply stop the loop; the thread will handle cleanup in finally block
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=5.0)
            self._thread = None
        logger.info(f"ORCHESTRATION_LIFECYCLE: Thread stopped for {self.agent_type}")

    def _resolve_sim_user_id(self, sim_id: str, fallback_user_id: Optional[str] = None) -> Optional[str]:
        """Resolve tenant user_id for *sim_id* from sim/session maps or fallback."""
        sim_user_map = getattr(self, "_simulation_user_map", None)
        if fallback_user_id:
            if sim_user_map is not None:
                mapped = sim_user_map.get(_sim_scope_key(sim_id, fallback_user_id))
                if mapped:
                    return mapped
            return fallback_user_id
        if sim_user_map is not None:
            mapped = sim_user_map.get(sim_id)
            if mapped:
                return mapped
        user_map = getattr(self, "session_user_map", None)
        sim_map = getattr(self, "session_simulation_map", None)
        if user_map is not None and sim_map is not None:
            for sid, mapped_sim in sim_map.items():
                if mapped_sim == sim_id:
                    uid = user_map.get(sid)
                    if uid:
                        return uid
        return fallback_user_id

    def _unpack_sim_scope_key(
        self,
        scope_key: str,
        fallback_user: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """Extract ``(sim_id, user_id)`` from a ``_simulation_subscriptions`` entry."""
        sim_user_map = getattr(self, "_simulation_user_map", None)
        uid = sim_user_map.get(scope_key) if sim_user_map is not None else None
        saas_on = tenant_keys._saas_mode_enabled()
        cand_uid = uid or fallback_user
        if saas_on and cand_uid:
            prefix = tenant_keys._key_prefix_for_user(cand_uid)
            if prefix and scope_key.startswith(f"{prefix}:"):
                return scope_key[len(prefix) + 1 :], cand_uid
        return scope_key, uid or self._resolve_sim_user_id(scope_key, fallback_user_id=fallback_user)

    async def _subscribe_scoped_channel(self, sim_id: str, user_id: Optional[str] = None) -> None:
        """Subscribe to a simulation-scoped broadcast channel."""
        eff_uid = user_id if user_id is not None else self._resolve_sim_user_id(sim_id)
        scoped_channel = _scoped_broadcast_channel(sim_id, user_id=eff_uid)
        if self._pubsub is not None:
            try:
                await self._pubsub.subscribe(scoped_channel)
                logger.info(f"ORCHESTRATION_EVENT: Subscribed to {scoped_channel}")
            except Exception as e:
                logger.error(f"ORCHESTRATION_ERROR: Failed to subscribe to {scoped_channel}: {e}")
        else:
            logger.info(
                f"ORCHESTRATION_EVENT: Will subscribe to {scoped_channel} on next pubsub connect"
            )

    async def _unsubscribe_scoped_channel(self, sim_id: str, user_id: Optional[str] = None) -> None:
        """Unsubscribe from a simulation-scoped broadcast channel.

        Called internally when no sessions remain for a given simulation_id.
        """
        eff_uid = user_id if user_id is not None else self._resolve_sim_user_id(sim_id)
        scoped_channel = _scoped_broadcast_channel(sim_id, user_id=eff_uid)
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(scoped_channel)
                logger.info(f"ORCHESTRATION_LIFECYCLE: Unsubscribed from {scoped_channel}")
            except Exception as e:
                logger.error(f"ORCHESTRATION_ERROR: Failed to unsubscribe from {scoped_channel}: {e}")

    async def _run_on_dispatcher_loop(self, coro_factory: Any) -> None:
        """Run an async pubsub operation on ``self._loop`` when invoked from another loop."""
        loop = getattr(self, "_loop", None)
        if loop and loop.is_running():
            try:
                cur_loop = asyncio.get_running_loop()
            except RuntimeError:
                cur_loop = None
            if cur_loop is not loop:
                fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
                await asyncio.wrap_future(fut)
                return
        await coro_factory()

    def _schedule_loop_cleanup(self, coro_factory: Any, label: str) -> None:
        """Schedule a fire-and-forget cleanup coroutine on ``self._loop`` safely."""
        loop = getattr(self, "_loop", None)
        if not (loop and loop.is_running()):
            return

        def _spawn() -> None:
            t = asyncio.ensure_future(coro_factory())
            self._background_tasks.add(t)

            def _done(done_task: asyncio.Task) -> None:
                self._background_tasks.discard(done_task)
                if not done_task.cancelled():
                    exc = done_task.exception()
                    if exc is not None:
                        logger.warning("ORCHESTRATION_CLEANUP: %s failed: %s", label, exc)

            t.add_done_callback(_done)

        try:
            cur_loop = asyncio.get_running_loop()
        except RuntimeError:
            cur_loop = None
        if cur_loop is loop:
            _spawn()
        else:
            loop.call_soon_threadsafe(_spawn)

    def remove_session(self, session_id: str) -> None:
        """Remove a session and clean up simulation tracking state.

        Removes the session from active_sessions, session_simulation_map, and
        — if no other sessions remain for its simulation — from
        _simulation_subscriptions (with an async unsubscribe from the scoped
        Redis channel).

        This method is safe to call from any thread; task cancellation and the
        async unsubscribe are marshaled onto the dispatcher's event loop if it's
        running.
        """
        self.active_sessions.discard(session_id)
        # Cancel any active running task for this session on its owning loop
        active_session_tasks = getattr(self, "_active_session_tasks", None)
        task = active_session_tasks.pop(session_id, None) if active_session_tasks is not None else None
        loop = getattr(self, "_loop", None)
        if task and not task.done():
            if loop and loop.is_running():
                try:
                    cur_loop = asyncio.get_running_loop()
                except RuntimeError:
                    cur_loop = None
                if cur_loop is loop:
                    task.cancel()
                else:
                    loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()
        # M9: schedule _session_locks cleanup on the event loop thread
        # to avoid RuntimeError from concurrent dict mutation during
        # _eviction_loop iteration.  Falls back to direct pop when the
        # loop is not running (shutdown / tests).
        session_locks = getattr(self, "_session_locks", None)
        if session_locks is not None:
            if loop and loop.is_running():
                loop.call_soon_threadsafe(session_locks.pop, session_id, None)
            else:
                session_locks.pop(session_id, None)
        sim_id = self.session_simulation_map.pop(session_id, None)
        user_map = getattr(self, "session_user_map", None)
        session_user = user_map.pop(session_id, None) if user_map is not None else None
        eff_user = session_user or _dispatcher_user_id() or None
        # Deferral A: forget this session's cached user bearer token
        # so we don't leak entries across long-running dispatcher
        # processes.
        user_auth_context.clear_session_token(session_id)
        # Schedule async registry cleanup using the same pattern as
        # _unsubscribe_scoped_channel — keeps remove_session() sync.
        simulation_registry._local.pop(simulation_registry._l1_key(session_id, user_id=eff_user), None)
        simulation_registry._local.pop(session_id, None)
        if not tenant_keys._saas_mode_enabled() or eff_user:
            self._schedule_loop_cleanup(
                lambda sid=session_id, uid=eff_user: simulation_registry.unregister(sid, user_id=uid),
                f"unregister({session_id})",
            )

        if sim_id is not None:
            saas_on = tenant_keys._saas_mode_enabled()
            # Check if any other sessions still belong to this simulation (and tenant in SaaS mode)
            if saas_on and eff_user and user_map is not None:
                remaining = any(
                    s == sim_id and (user_map.get(sid) or _dispatcher_user_id() or None) == eff_user
                    for sid, s in self.session_simulation_map.items()
                )
            else:
                remaining = any(s == sim_id for s in self.session_simulation_map.values())
            sim_subs = getattr(self, "_simulation_subscriptions", None)
            sub_key = _sim_scope_key(sim_id, eff_user)
            if not remaining and sim_subs is not None:
                target_key = (
                    sub_key
                    if sub_key in sim_subs
                    else (sim_id if (not saas_on or not eff_user) and sim_id in sim_subs else None)
                )
                if target_key is not None:
                    sim_user = session_user or self._resolve_sim_user_id(sim_id)
                    sim_user_map = getattr(self, "_simulation_user_map", None)
                    if sim_user_map is not None:
                        sim_user_map.pop(target_key, None)
                    sim_subs.discard(target_key)
                    # Schedule the unsubscribe on the dispatcher's event loop
                    self._schedule_loop_cleanup(
                        lambda sid=sim_id, uid=sim_user: self._unsubscribe_scoped_channel(sid, user_id=uid),
                        f"unsubscribe({sim_id})",
                    )
                    logger.info(f"ORCHESTRATION_LIFECYCLE: Cleaned up simulation {sim_id} (no remaining sessions)")

        sim_subs_len = len(getattr(self, "_simulation_subscriptions", ()))
        logger.info(
            f"ORCHESTRATION_LIFECYCLE: Removed session {session_id} "
            f"(active={len(self.active_sessions)}, sims={sim_subs_len})"
        )

    def _run_thread(self):
        """Thread entry point: creates a new event loop and runs the listener."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            # Run the main listener loop
            self._loop.run_until_complete(self._listen_loop())
        except Exception as e:
            if not self._stop_event.is_set():
                logger.error(f"ORCHESTRATION_ERROR: Loop error: {e}")
        finally:
            # CLEANUP: Cancel all pending tasks before closing the loop
            try:
                self._loop.run_until_complete(self._cancel_all_tasks())
                # Process any pending callbacks (like cancellations finishing)
                self._loop.run_until_complete(asyncio.sleep(0.1))
            except Exception as e:
                logger.debug(f"ORCHESTRATION_LIFECYCLE: Cleanup error: {e}")

            self._loop.close()
            self._loop = None

    async def _eviction_loop(self):
        """Periodically cleans up stale _session_locks for crashed sessions."""
        while not self._stop_event.is_set():
            await asyncio.sleep(60)  # Evict every 60 seconds
            # Snapshot keys to avoid RuntimeError if remove_session()
            # mutates _session_locks from another thread concurrently.
            stale = [sid for sid in list(self._session_locks) if sid not in self.active_sessions]
            for sid in stale:
                self._session_locks.pop(sid, None)

    async def _listen_loop(self):
        """Main loop that runs both Pub/Sub and Queue listeners."""
        if self.dispatch_mode == "callable":
            logger.info(f"ORCHESTRATION_LIFECYCLE: Callable mode — HTTP only, no Redis listeners for {self.agent_type}")
            # Callable agents (Agent Engine) cannot connect to Redis.
            # They receive events solely via HTTP orchestration pushes.
            eviction_task = asyncio.ensure_future(self._eviction_loop())
            while not self._stop_event.is_set():
                await asyncio.sleep(1)
            eviction_task.cancel()
            return

        backoff = 5  # Initial backoff seconds
        while not self._stop_event.is_set():
            try:
                r = get_shared_redis_client()
                if r is None:
                    logger.error("ORCHESTRATION_ERROR: No Redis client available (REDIS_ADDR not set?)")
                    await asyncio.sleep(backoff)
                    continue
                # Run both listeners concurrently.  If EITHER exits
                # (crash, disconnect, pool exhaustion), cancel the
                # survivor and restart both via the outer loop.
                # asyncio.gather waits for ALL tasks — a silently-
                # returning listener leaves gather blocking on the
                # surviving one, preventing reconnection forever.
                pubsub_task = asyncio.ensure_future(self._pubsub_listener(r))
                queue_task = asyncio.ensure_future(self._queue_listener(r))
                eviction_task = asyncio.ensure_future(self._eviction_loop())
                try:
                    done, pending = await asyncio.wait(
                        [pubsub_task, queue_task, eviction_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # Cancel the surviving listener so both restart together
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    # Re-raise if the completed task had an exception
                    for task in done:
                        exc = task.exception()
                        if exc is not None:
                            raise exc
                except (asyncio.CancelledError, GeneratorExit):
                    pubsub_task.cancel()
                    queue_task.cancel()
                    raise
                backoff = 5  # Reset on successful connection cycle
            except (asyncio.CancelledError, GeneratorExit):
                break
            except Exception as e:
                if not self._stop_event.is_set():
                    jitter = random.uniform(0, backoff * 0.25)
                    logger.error(
                        "ORCHESTRATION_ERROR: Listener loop crash, reconnecting in %.1fs: %s",
                        backoff + jitter,
                        e,
                    )
                    await asyncio.sleep(backoff + jitter)
                    backoff = min(backoff * 2, 60)  # Double, cap at 60s

    async def _pubsub_listener(self, r: redis.Redis):
        """Listens for global and simulation-scoped broadcast pulses via Pub/Sub."""
        channel_name = _global_broadcast_channel()
        try:
            async with r.pubsub() as pubsub:
                self._pubsub = pubsub
                await pubsub.subscribe(channel_name)
                logger.info(f"ORCHESTRATION_PUB/SUB: Subscribed to {channel_name}")

                # Re-subscribe to any simulation channels from a prior connection cycle
                sim_subs = getattr(self, "_simulation_subscriptions", None)
                for sub_key in list(sim_subs or ()):
                    actual_sim_id, sub_uid = self._unpack_sim_scope_key(sub_key)
                    scoped = _scoped_broadcast_channel(actual_sim_id, user_id=sub_uid)
                    await pubsub.subscribe(scoped)
                    logger.info(f"ORCHESTRATION_PUB/SUB: Re-subscribed to {scoped}")

                while not self._stop_event.is_set():
                    try:
                        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                        if message and message["type"] == "message":
                            await self._handle_message(r, message["data"], channel=message.get("channel"))
                    except (GeneratorExit, asyncio.CancelledError):
                        return
                    except Exception as e:
                        if not self._stop_event.is_set():
                            logger.error(f"ORCHESTRATION_ERROR: PubSub read error: {e}")
                        break  # Reconnect
        except asyncio.CancelledError:
            return
        except Exception as e:
            if not self._stop_event.is_set():
                logger.error(f"ORCHESTRATION_ERROR: PubSub connection error: {e}")
        finally:
            self._pubsub = None

    async def _queue_listener(self, r: redis.Redis):
        """Listens for targeted spawn events via Redis List (Queue)."""
        queues = spawn_queue_names(self.agent_type)
        logger.info(f"ORCHESTRATION_QUEUE: Listening on {len(queues)} shards: {queues}")

        while not self._stop_event.is_set():
            try:
                # BLPOP from all shards — Redis returns first available
                # Randomize order to avoid priority bias (BLPOP prioritizes first key)
                shuffled = list(queues)
                random.shuffle(shuffled)
                res = await r.blpop(shuffled, timeout=1)  # type: ignore[reportGeneralTypeIssues,misc]
                if res:
                    _, data = res
                    await self._handle_message(r, data)
            except (GeneratorExit, asyncio.CancelledError):
                return
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error(f"ORCHESTRATION_ERROR: Queue read error: {e}")
                    await asyncio.sleep(1)
                break  # Reconnect

    async def handle_event(self, data: dict):
        """Processes an orchestration event pushed via HTTP."""
        return await self._process_event(data)

    async def _handle_message(self, r: redis.Redis, data_str: bytes, channel: str | bytes | None = None):
        """Processes a message from Redis Pub/Sub or Queue."""
        try:
            data = json.loads(data_str)
            await self._process_event(data, source_channel=channel)
        except Exception as e:
            logger.error(f"ORCHESTRATION_ERROR: Failed to handle Redis message: {e}")

    def _record_seen_event(self, event_id: str):
        """Records an event ID and enforces the FIFO cache limit."""
        self._seen_events[event_id] = None
        # M8: FIFO eviction — OrderedDict preserves insertion order,
        # so popitem(last=False) always removes the oldest entry.
        if len(self._seen_events) > 1000:
            self._seen_events.popitem(last=False)

    async def _process_event(self, data: dict, source_channel: str | bytes | None = None):
        """Core event processing logic shared by Push and Pull models."""
        try:
            msg_type = data.get("type")
            event_id = data.get("eventId")

            if event_id:
                if event_id in self._seen_events:
                    logger.debug(f"ORCHESTRATION_DEDUPE: Skipping duplicate event {event_id}")
                    return
                self._record_seen_event(event_id)

            raw_payload = data.get("payload")
            payload = raw_payload if isinstance(raw_payload, dict) else {}

            # sessionId can be at top level or inside payload depending on source
            session_id = data.get("sessionId") or payload.get("sessionId")

            logger.info(f"SIMULATION_EVENT: Incoming [{msg_type}] for {session_id}")

            if msg_type == "environment_reset":
                eff_reset_user, _, tenant_ok = _resolve_event_tenant(
                    data, payload, source_channel=source_channel
                )
                if not tenant_ok:
                    return

                bound_user = _dispatcher_user_id()
                saas_on = tenant_keys._saas_mode_enabled()
                active_session_tasks = getattr(self, "_active_session_tasks", None)
                session_locks = getattr(self, "_session_locks", None)
                user_map = getattr(self, "session_user_map", None)
                sim_user_map = getattr(self, "_simulation_user_map", None)
                sim_subs = getattr(self, "_simulation_subscriptions", None)
                loop = getattr(self, "_loop", None)

                if saas_on:
                    target_sids: set[str] = set()
                    if user_map is not None:
                        for sid, uid in user_map.items():
                            if uid == eff_reset_user:
                                target_sids.add(sid)
                    if bound_user == eff_reset_user:
                        for sid in list(self.active_sessions):
                            if user_map is None or user_map.get(sid) in (None, eff_reset_user):
                                target_sids.add(sid)
                        for sid, _ in self.session_simulation_map.items():
                            if user_map is None or user_map.get(sid) in (None, eff_reset_user):
                                target_sids.add(sid)

                    target_tasks: list[asyncio.Task] = []
                    if active_session_tasks is not None:
                        for sid in target_sids:
                            task = active_session_tasks.pop(sid, None)
                            if task is not None:
                                target_tasks.append(task)
                                self._background_tasks.discard(task)

                    for sid in target_sids:
                        user_auth_context.clear_session_token(sid)
                        self.active_sessions.discard(sid)
                        self.session_simulation_map.pop(sid, None)
                        if user_map is not None:
                            user_map.pop(sid, None)

                    logger.info(
                        "ORCHESTRATION_RESET: Tenant-scoped reset for user=%s: "
                        "clearing %d session(s) and cancelling %d task(s)",
                        eff_reset_user,
                        len(target_sids),
                        len(target_tasks),
                    )

                    def cancel_tenant_tasks_and_locks() -> None:
                        for t in target_tasks:
                            t.cancel()
                        if session_locks is not None:
                            for sid in target_sids:
                                session_locks.pop(sid, None)

                    if loop and loop.is_running():
                        try:
                            cur_loop = asyncio.get_running_loop()
                        except RuntimeError:
                            cur_loop = None
                        if cur_loop is loop:
                            cancel_tenant_tasks_and_locks()
                        else:
                            loop.call_soon_threadsafe(cancel_tenant_tasks_and_locks)
                    else:
                        cancel_tenant_tasks_and_locks()

                    tenant_prefix = f"{tenant_keys._key_prefix_for_user(eff_reset_user)}:"
                    tenant_sub_keys: list[str] = []
                    if sim_subs is not None:
                        for sub_key in list(sim_subs):
                            mapped_uid = sim_user_map.get(sub_key) if sim_user_map is not None else None
                            if mapped_uid == eff_reset_user or sub_key.startswith(tenant_prefix):
                                tenant_sub_keys.append(sub_key)
                            elif bound_user == eff_reset_user and mapped_uid is None and ":" not in sub_key:
                                tenant_sub_keys.append(sub_key)
                        for sub_key in tenant_sub_keys:
                            actual_sim_id, sub_uid = self._unpack_sim_scope_key(
                                sub_key, fallback_user=eff_reset_user
                            )
                            if sim_user_map is not None:
                                sim_user_map.pop(sub_key, None)
                            sim_subs.discard(sub_key)
                            await self._run_on_dispatcher_loop(
                                lambda sid=actual_sim_id, uid=sub_uid: self._unsubscribe_scoped_channel(
                                    sid, user_id=uid
                                )
                            )
                    elif sim_user_map is not None:
                        for sub_key, mapped_uid in list(sim_user_map.items()):
                            if mapped_uid == eff_reset_user or sub_key.startswith(tenant_prefix):
                                sim_user_map.pop(sub_key, None)

                    await simulation_registry.clear(user_id=eff_reset_user)
                    return

                session_count = len(self.active_sessions)
                task_count = len(self._background_tasks)
                logger.info(
                    f"ORCHESTRATION_RESET: Environment reset received. "
                    f"Clearing {session_count} session(s) and cancelling {task_count} task(s)"
                )
                # Cancel all in-flight run_async tasks safely inside the event loop
                session_tasks = list(active_session_tasks.values()) if active_session_tasks else []
                all_tasks = list(self._background_tasks) + session_tasks
                self._background_tasks.clear()
                if active_session_tasks is not None:
                    active_session_tasks.clear()
                # Clear per-session user auth tokens before wiping active_sessions
                for sid in list(self.active_sessions):
                    user_auth_context.clear_session_token(sid)
                # Clear session tracking
                self.active_sessions.clear()
                if loop and loop.is_running():
                    try:
                        cur_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        cur_loop = None

                    def cancel_all():
                        for t in all_tasks:
                            t.cancel()
                        # Clear per-session locks on the loop thread after cancelling tasks
                        if session_locks is not None:
                            session_locks.clear()

                    if cur_loop is loop:
                        cancel_all()
                    else:
                        loop.call_soon_threadsafe(cancel_all)
                else:
                    for t in all_tasks:
                        t.cancel()
                    if session_locks is not None:
                        session_locks.clear()
                # M10: Clear simulation tracking and unsubscribe scoped channels
                # so stale mappings/subscriptions don't survive an environment reset.
                stale_subs = list(sim_subs) if sim_subs is not None else []
                sub_infos = {sub_key: self._unpack_sim_scope_key(sub_key) for sub_key in stale_subs}
                self.session_simulation_map.clear()
                if user_map is not None:
                    user_map.clear()
                if sim_user_map is not None:
                    sim_user_map.clear()
                if sim_subs is not None:
                    sim_subs.clear()
                    for sub_key in stale_subs:
                        actual_sim_id, sub_uid = sub_infos[sub_key]
                        await self._run_on_dispatcher_loop(
                            lambda sid=actual_sim_id, uid=sub_uid: self._unsubscribe_scoped_channel(
                                sid, user_id=uid
                            )
                        )
                # Clear dedup cache
                seen_events = getattr(self, "_seen_events", None)
                if seen_events is not None:
                    seen_events.clear()
                await simulation_registry.clear(user_id=eff_reset_user or None)
                return

            elif msg_type == "spawn_agent":
                raw_payload = data.get("payload")
                payload = raw_payload if isinstance(raw_payload, dict) else {}
                target_agent_type = payload.get("agentType")

                if target_agent_type == self.agent_type:
                    if not session_id:
                        logger.warning("ORCHESTRATION_EVENT: spawn_agent missing sessionId, ignoring")
                        return
                    # Phase 2.5b: reject cross-tenant or unauthenticated spawns BEFORE
                    # mutating active_sessions, session_simulation_map, simulation_registry,
                    # or scoped Pub/Sub subscriptions.
                    payload_user, has_user_conflict = _extract_payload_user_id(payload)
                    if has_user_conflict:
                        logger.error(
                            "ORCHESTRATION_ERROR: conflicting user_id fields in spawn_agent payload "
                            "(session=%s)",
                            session_id,
                        )
                        return
                    bound_user = _dispatcher_user_id()
                    saas_on = tenant_keys._saas_mode_enabled()
                    if bound_user and payload_user != bound_user:
                        logger.error(
                            "ORCHESTRATION_ERROR: cross-tenant spawn rejected "
                            "(bound=%s payload=%s session=%s) — check DISPATCHER_USER_ID",
                            bound_user,
                            payload_user,
                            session_id,
                        )
                        return
                    effective_user = payload_user or bound_user
                    if saas_on:
                        if not effective_user or effective_user.lower() in _ANONYMOUS_USER_IDS:
                            logger.error(
                                "ORCHESTRATION_ERROR: unauthenticated or anonymous spawn rejected in SAAS_MODE=on "
                                "(payload=%r bound=%r session=%s)",
                                payload_user,
                                bound_user,
                                session_id,
                            )
                            return
                        try:
                            effective_user = tenant_keys._validate_component(effective_user, "user_id")
                        except ValueError as exc:
                            logger.error(
                                "ORCHESTRATION_ERROR: invalid user_id %r in SAAS_MODE=on (session=%s): %s",
                                effective_user,
                                session_id,
                                exc,
                            )
                            return
                        user_map = getattr(self, "session_user_map", None)
                        existing_owner = user_map.get(session_id) if user_map is not None else None
                        if existing_owner and existing_owner != effective_user:
                            logger.error(
                                "ORCHESTRATION_ERROR: cross-tenant spawn_agent rejected "
                                "(existing_owner=%s effective_user=%s session=%s)",
                                existing_owner,
                                effective_user,
                                session_id,
                            )
                            return

                    logger.info(f"ORCHESTRATION_EVENT: Spawning session {session_id} (type: {target_agent_type})")
                    self.active_sessions.add(session_id)
                    if effective_user:
                        user_map = getattr(self, "session_user_map", None)
                        if user_map is None:
                            user_map = ThreadSafeDict()
                            self.session_user_map = user_map
                        user_map[session_id] = effective_user

                    # Track simulation_id for scoped broadcast subscription
                    sim_id = payload.get("simulation_id")
                    if sim_id:
                        self.session_simulation_map[session_id] = sim_id
                        sub_key = _sim_scope_key(sim_id, effective_user or None)
                        if effective_user:
                            sim_user_map = getattr(self, "_simulation_user_map", None)
                            if sim_user_map is None:
                                sim_user_map = ThreadSafeDict()
                                self._simulation_user_map = sim_user_map
                            sim_user_map[sub_key] = effective_user
                        await simulation_registry.register(session_id, sim_id, user_id=effective_user or None)
                        sim_subs = getattr(self, "_simulation_subscriptions", None)
                        if sim_subs is None:
                            sim_subs = set()
                            self._simulation_subscriptions = sim_subs
                        if sub_key not in sim_subs:
                            sim_subs.add(sub_key)
                            await self._run_on_dispatcher_loop(
                                lambda sid=sim_id, uid=effective_user or None: self._subscribe_scoped_channel(
                                    sid, user_id=uid
                                )
                            )

                    # Deferral A (multi-user cloud correctness): stash
                    # the spawning user's raw bearer token so the
                    # planner's memory-retrieval tool can call the
                    # gateway as *this* user, not as a shared service
                    # identity. Local dev typically has no such token
                    # (loopback LocalResolver handles auth) — an
                    # empty/missing value is a no-op inside
                    # ``set_session_token``.
                    user_auth_context.set_session_token(
                        session_id,
                        payload.get("user_auth_token"),
                    )

                    # Session creation is deferred to the first run_async() call,
                    # which auto-creates via Runner(auto_create_session=True).
                    # No DB round-trip needed at spawn time.
                else:
                    logger.debug(f"ORCHESTRATION_EVENT: Ignored spawn for {target_agent_type}")

            elif msg_type == "broadcast":
                raw_payload = data.get("payload")
                payload = raw_payload if isinstance(raw_payload, dict) else {}
                eff_user, channel_sim_id, tenant_ok = _resolve_event_tenant(
                    data, payload, source_channel=source_channel
                )
                if not tenant_ok:
                    return
                bound_user = _dispatcher_user_id()
                saas_on = tenant_keys._saas_mode_enabled()
                msg_sim_id = data.get("simulation_id") or payload.get("simulation_id")
                if channel_sim_id and msg_sim_id and msg_sim_id != channel_sim_id:
                    logger.error(
                        "ORCHESTRATION_ERROR: mismatched channel vs payload simulation_id in broadcast "
                        "(channel_sim=%s msg_sim=%s)",
                        channel_sim_id,
                        msg_sim_id,
                    )
                    return

                broadcast_data = payload.get("data", "PULSE")
                raw_targets = payload.get("targets")  # Optional list of session IDs
                targets = raw_targets if isinstance(raw_targets, list) else None

                logger.info(
                    f"ORCHESTRATION_EVENT: Processing pulse: {broadcast_data} "
                    f"(Targets: {len(targets) if targets else 'all'}, "
                    f"active_sessions={len(self.active_sessions)})"
                )

                # Prepare content: if broadcast_data is JSON from UI, extract 'text'
                user_msg = broadcast_data if isinstance(broadcast_data, str) else str(broadcast_data)
                try:
                    inner = json.loads(user_msg)
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        user_msg = inner["text"]
                except Exception:
                    pass

                content = types.Content(role="user", parts=[types.Part(text=user_msg)])

                # Filter targets.  When the broadcast carries a
                # simulation_id, ONLY trigger sessions belonging to
                # that simulation — never cross-contaminate.  When BOTH
                # simulation_id and explicit session targets are present
                # (e.g. a relayed or targeted simulation-scoped broadcast),
                # intersect them so already-triggered sessions are not
                # double-triggered on peer instances.
                broadcast_sim_id = msg_sim_id or channel_sim_id
                to_trigger = []
                if broadcast_sim_id:
                    sim_sessions = [
                        sid for sid in self.active_sessions if self.session_simulation_map.get(sid) == broadcast_sim_id
                    ]
                    if targets and self.agent_type not in targets:
                        target_set = set(targets)
                        to_trigger = [sid for sid in sim_sessions if sid in target_set]
                    else:
                        to_trigger = sim_sessions
                elif targets:
                    if self.agent_type in targets:
                        to_trigger = list(self.active_sessions)
                    else:
                        to_trigger = [sid for sid in targets if sid in self.active_sessions]
                else:
                    to_trigger = list(self.active_sessions)

                if saas_on:
                    user_map = getattr(self, "session_user_map", None)
                    if user_map is not None:
                        to_trigger = [
                            sid
                            for sid in to_trigger
                            if (user_map.get(sid) or bound_user) == eff_user
                        ]
                    elif bound_user != eff_user:
                        to_trigger = []

                # Exclude finished/collapsed runners from fan-out
                raw_exclude_ids = payload.get("exclude_runner_ids")
                exclude_set = set(raw_exclude_ids) if isinstance(raw_exclude_ids, list) else set()
                if exclude_set:
                    to_trigger = [sid for sid in to_trigger if sid not in exclude_set]

                # The gateway's DispatchToAgent sends targeted broadcasts
                # via HTTP POST to ONE random Cloud Run instance.  With
                # min_instances=5, this instance likely owns only a subset
                # of the target sessions.  We must:
                #  1. Trigger sessions we own.
                #  2. Re-publish to Redis pub/sub so other instances
                #     can trigger their sessions.
                #
                # Exclude finished runners and self.agent_type from unmatched
                # so excluded runners do not trigger spurious Redis relays.
                if targets and self.agent_type not in targets:
                    unmatched = set(targets) - set(to_trigger) - exclude_set
                else:
                    unmatched = set()
                # Only relay from the initial HTTP delivery, never
                # re-relay a message that already came through pub/sub.
                already_relayed = data.get("_relayed", False)

                logger.info(
                    f"ORCHESTRATION_EVENT: Broadcast filter: "
                    f"matched={len(to_trigger)}, unmatched={len(unmatched)}, "
                    f"relayed={already_relayed}, "
                    f"active_sessions={len(self.active_sessions)}"
                )

                if unmatched and not already_relayed:
                    try:
                        # Use the shared pool instead of a fresh redis.from_url(url):
                        # (1) the bare from_url ignored REDIS_PASSWORD, so local Redis
                        # (which is started with --requirepass localdev by docker-compose)
                        # rejected the publish with NOAUTH and the broadcast for
                        # unmatched sessions silently dropped; (2) every relay also
                        # opened a fresh connection outside the BlockingConnectionPool
                        # budget, defeating the pool-sized backpressure on Cloud Run.
                        # _process_event runs in self._loop (the dispatcher background
                        # loop), the same loop the shared pool's first connection was
                        # created in, so there's no event-loop mismatch.
                        relay_client = get_shared_redis_client()
                        if relay_client is not None:
                            relay_id = f"relay-{uuid.uuid4().hex[:12]}"
                            self._record_seen_event(relay_id)
                            relay_data = dict(data)
                            relay_data["eventId"] = relay_id
                            relay_data["_relayed"] = True
                            relay_payload = dict(payload)
                            relay_payload["targets"] = list(unmatched)
                            if saas_on and eff_user:
                                relay_payload.setdefault("user_id", eff_user)
                            relay_data["payload"] = relay_payload
                            # Derive the relay channel from the broadcast's
                            # simulation_id.  If the original broadcast was
                            # scoped (simulation:{sim_id}:broadcast), we must
                            # relay to the same scoped channel — otherwise
                            # other dispatcher instances subscribed only to
                            # the scoped channel would never see it.
                            if broadcast_sim_id:
                                eff_relay_user = (
                                    eff_user
                                    if saas_on
                                    else self._resolve_sim_user_id(
                                        broadcast_sim_id,
                                        fallback_user_id=eff_user or None,
                                    )
                                )
                                relay_channel = _scoped_broadcast_channel(broadcast_sim_id, user_id=eff_relay_user)
                            elif saas_on and eff_user:
                                relay_channel = tenant_keys.key_for_user(eff_user, "simulation", "broadcast")
                            else:
                                relay_channel = _global_broadcast_channel()
                            await relay_client.publish(relay_channel, _strict_json_dumps(relay_data))
                            logger.info(
                                f"ORCHESTRATION_EVENT: Relayed broadcast for "
                                f"{len(unmatched)} unmatched sessions "
                                f"to {relay_channel} (relayId={relay_id})"
                            )
                            # NOTE: do NOT aclose() relay_client — it's the
                            # process-wide shared pool, owned by redis_pool.
                        else:
                            logger.warning(
                                "ORCHESTRATION_EVENT: Skipping relay for %d unmatched "
                                "sessions — no shared Redis client (REDIS_ADDR unset?)",
                                len(unmatched),
                            )
                    except Exception as e:
                        logger.error(f"ORCHESTRATION_ERROR: Failed to relay broadcast: {e}")

                if not to_trigger:
                    return

                logger.info(
                    "PERF_TRACE: broadcast_received sessions_to_trigger=%d active=%d t=%.6f",
                    len(to_trigger),
                    len(self.active_sessions),
                    time.perf_counter(),
                )

                t_trigger_start = time.perf_counter()
                for sid in to_trigger:
                    try:
                        self._trigger_agent_run(sid, content)
                    except Exception as e:
                        logger.error(f"ORCHESTRATION_ERROR: Failed to trigger pulse for {sid}: {e}")
                t_trigger_end = time.perf_counter()
                logger.info(
                    "PERF_TRACE: broadcast_triggered count=%d elapsed_ms=%.1f",
                    len(to_trigger),
                    (t_trigger_end - t_trigger_start) * 1000,
                )

            elif msg_type == "end_simulation":
                raw_payload = data.get("payload")
                payload = raw_payload if isinstance(raw_payload, dict) else {}
                eff_user, channel_sim_id, tenant_ok = _resolve_event_tenant(
                    data, payload, source_channel=source_channel
                )
                if not tenant_ok:
                    return
                bound_user = _dispatcher_user_id()
                saas_on = tenant_keys._saas_mode_enabled()
                msg_sim_id = data.get("simulation_id") or payload.get("simulation_id")
                if channel_sim_id and msg_sim_id and msg_sim_id != channel_sim_id:
                    logger.error(
                        "ORCHESTRATION_ERROR: mismatched channel vs payload simulation_id in end_simulation "
                        "(channel_sim=%s msg_sim=%s)",
                        channel_sim_id,
                        msg_sim_id,
                    )
                    return
                sim_id = msg_sim_id or channel_sim_id

                if sim_id:
                    # Remove all sessions belonging to this simulation (and tenant in SaaS mode)
                    user_map = getattr(self, "session_user_map", None)
                    sessions_to_remove = [
                        sid
                        for sid, mapped_sim in self.session_simulation_map.items()
                        if mapped_sim == sim_id
                        and (
                            not saas_on
                            or (
                                user_map is not None
                                and (user_map.get(sid) or bound_user) == eff_user
                            )
                            or (user_map is None and bound_user == eff_user)
                        )
                    ]
                    for sid in sessions_to_remove:
                        self.remove_session(sid)
                    logger.info(
                        f"ORCHESTRATION_LIFECYCLE: Simulation {sim_id} ended, "
                        f"removed {len(sessions_to_remove)} session(s)"
                    )
                else:
                    logger.warning("ORCHESTRATION_EVENT: end_simulation without simulation_id, ignoring")

            elif msg_type == "a2ui_action":
                raw_payload = data.get("payload")
                payload = raw_payload if isinstance(raw_payload, dict) else {}
                eff_user, channel_sim_id, tenant_ok = _resolve_event_tenant(
                    data, payload, source_channel=source_channel
                )
                if not tenant_ok:
                    return
                bound_user = _dispatcher_user_id()
                saas_on = tenant_keys._saas_mode_enabled()

                action_name = payload.get("actionName", "unknown")
                target_sid = data.get("sessionId") or payload.get("sessionId")

                logger.info(f"ORCHESTRATION_EVENT: A2UI action '{action_name}' for session {target_sid}")

                if target_sid and target_sid in self.active_sessions:
                    if saas_on:
                        user_map = getattr(self, "session_user_map", None)
                        session_owner = (user_map.get(target_sid) if user_map is not None else None) or bound_user
                        if session_owner != eff_user:
                            logger.error(
                                "ORCHESTRATION_ERROR: cross-tenant a2ui_action rejected "
                                "(owner=%s eff_user=%s session=%s)",
                                session_owner,
                                eff_user,
                                target_sid,
                            )
                            return
                        if channel_sim_id and self.session_simulation_map.get(target_sid) != channel_sim_id:
                            logger.error(
                                "ORCHESTRATION_ERROR: simulation-mismatched a2ui_action rejected "
                                "(channel_sim=%s session_sim=%s session=%s)",
                                channel_sim_id,
                                self.session_simulation_map.get(target_sid),
                                target_sid,
                            )
                            return

                    action_text = _strict_json_dumps(
                        {
                            "a2ui_action": action_name,
                            "source": "a2ui_button",
                        }
                    )

                    content = types.Content(
                        role="user",
                        parts=[types.Part(text=action_text)],
                    )
                    try:
                        self._trigger_agent_run(target_sid, content)
                    except Exception as e:
                        logger.error(f"ORCHESTRATION_ERROR: Failed to trigger A2UI action for {target_sid}: {e}")
                else:
                    logger.warning(f"ORCHESTRATION_EVENT: Session {target_sid} not active, ignoring A2UI action")

        except Exception as e:
            logger.error(f"ORCHESTRATION_ERROR: Event processing failed: {e}")

    async def _locked_trigger(self, session_id: str, content: Any):
        """Acquire a per-session lock, then run the agent logic.

        Serializes concurrent events for the same session (e.g. START_GUN +
        TICK-0 race) while allowing different sessions to run concurrently.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        try:
            async with lock:
                await self._trigger_agent_run_logic(session_id, content)
        except asyncio.CancelledError:
            logger.info(f"ORCHESTRATION_LIFECYCLE: Task for session {session_id} cancelled.")
            raise

    def _trigger_agent_run(self, session_id: str, content: Any):
        """Helper to run the agent in the background."""
        # Ensure we use the correct event loop (our own internal loop)
        if self._loop and self._loop.is_running():

            def spawn():
                # Cancel any pending running task for that session if one is still in progress
                existing_task = self._active_session_tasks.get(session_id)
                if existing_task and not existing_task.done():
                    logger.info(
                        f"ORCHESTRATION_LIFECYCLE: Cancelling stale task for session {session_id} "
                        "before starting new run"
                    )
                    existing_task.cancel()

                task = self._loop.create_task(self._locked_trigger(session_id, content))
                self._active_session_tasks[session_id] = task
                self._background_tasks.add(task)

                def cleanup(t):
                    self._background_tasks.discard(t)
                    if self._active_session_tasks.get(session_id) is t:
                        self._active_session_tasks.pop(session_id, None)

                task.add_done_callback(cleanup)

            self._loop.call_soon_threadsafe(spawn)
        else:
            logger.error(f"ORCHESTRATION_ERROR: Cannot trigger run for {session_id}, event loop is not running.")

    async def _trigger_agent_run_logic(self, session_id: str, content: Any, pulses_collector: Optional[list] = None):
        """Core logic for running the agent and relaying results."""
        if self._run_semaphore is None:
            raw_max_conn = os.environ.get("REDIS_MAX_CONNECTIONS", "50").strip()
            if raw_max_conn:
                try:
                    max_conn = int(raw_max_conn)
                except ValueError as exc:
                    raise ValueError(f"Invalid REDIS_MAX_CONNECTIONS={raw_max_conn!r}") from exc
                if max_conn < 1:
                    raise ValueError(f"REDIS_MAX_CONNECTIONS must be >= 1, got {max_conn}")
            else:
                max_conn = 50
            # Reserve 20 connections for listeners and background workers
            semaphore_limit = max(1, max_conn - 20)
            self._run_semaphore = asyncio.Semaphore(semaphore_limit)

        user_map = getattr(self, "session_user_map", None)
        resolved_user = (user_map.get(session_id) if user_map is not None else None) or _dispatcher_user_id()
        saas_on = tenant_keys._saas_mode_enabled()
        if saas_on:
            if not resolved_user or resolved_user.lower() in _ANONYMOUS_USER_IDS:
                raise ValueError(f"Authenticated user_id is required when SAAS_MODE=on (session={session_id})")
            resolved_user = tenant_keys._validate_component(resolved_user, "user_id")
        run_user_id = resolved_user or "simulation"

        # Ensure we have a clean OTEL context for this background execution
        token = otel_context.attach(otel_context.Context())
        broadcast_to_redis = pulses_collector is None
        try:

            # NOTE: Session existence is handled by runner.run_async() internally
            # via _get_or_create_session(). Calling get_session() here was a
            # duplicate that added ~4 unnecessary DB round-trips per invocation.

            logger.info(f"SIMULATION_EVENT: Triggering run_async for {session_id}")
            # 3. Iterate through generator to execute and relay results
            t_run_start = time.perf_counter()
            run_kwargs: dict[str, Any] = {
                "user_id": run_user_id,
                "session_id": session_id,
                "new_message": content,
            }
            if resolved_user:
                try:
                    sig = inspect.signature(self.runner.run_async)
                    accepts_state_delta = "state_delta" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                    )
                except (ValueError, TypeError):
                    accepts_state_delta = False
                if accepts_state_delta:
                    run_kwargs["state_delta"] = {
                        "user_id": resolved_user,
                        "acting_user_id": resolved_user,
                    }

            async with self._run_semaphore:
                with simulation_registry.user_scope(resolved_user or None):
                    gen = self.runner.run_async(**run_kwargs)
                    async for event in gen:
                        logger.debug(f"ORCHESTRATION_EVENT: Received event from runner for {session_id}")
                        # Process each event as it arrives, not just the last one.
                        # This prevents intermediate events (reasoning, tool calls)
                        # from being silently discarded.
                        pulses = self._prepare_pulses(session_id, event)
                        for pulse_text in pulses:
                            if pulses_collector is not None:
                                logger.debug(f"ORCHESTRATION_DEBUG: Appending to collector for {session_id}")
                                pulses_collector.append(pulse_text)

                            if broadcast_to_redis and not self.suppress_gateway_emission:
                                # Passthrough: emit pulse as text or json.
                                # A2UI extraction is handled exclusively by the
                                # plugin's _emit_narrative (tool_end JSON path).
                                # Strip fenced ```a2ui``` blocks from text so raw
                                # A2UI markup doesn't leak to the frontend.
                                cleaned = re.sub(r"```a2ui\n[\s\S]*?(?:\n```|$)", "", pulse_text).strip()
                                emit_text = cleaned if cleaned else pulse_text

                                msg_type = "text"
                                event_type = "text"
                                emit_data = {"text": emit_text}

                                try:
                                    parsed = json.loads(emit_text)
                                    if isinstance(parsed, (dict, list)):
                                        # Skip A2UI tool results — the plugin's
                                        # _emit_narrative handles these with the
                                        # correct msg_type="a2ui" wrapper.
                                        if isinstance(parsed, dict) and "a2ui" in parsed:
                                            continue
                                        emit_data = parsed
                                        msg_type = "json"
                                        event_type = "json"
                                except Exception:
                                    pass  # Keep as text

                                sim_id = self.session_simulation_map.get(session_id)
                                await pulses_util.emit_gateway_message(
                                    origin={
                                        "type": "agent",
                                        "id": self.agent_type,
                                        "session_id": session_id,
                                    },
                                    destination=[session_id],
                                    status="success",
                                    msg_type=msg_type,
                                    event=event_type,
                                    data=emit_data,
                                    simulation_id=sim_id,
                                )

            t_run_end = time.perf_counter()
            logger.info(
                "PERF_TRACE: run_async_complete session=%s elapsed_ms=%.1f",
                session_id,
                (t_run_end - t_run_start) * 1000,
            )
            logger.info(f"ORCHESTRATION_EVENT: Completed run_async for {session_id}")
        except Exception as run_err:
            import traceback

            logger.error(
                f"ORCHESTRATION_ERROR: Failed to trigger run for {session_id}: {run_err}\n{traceback.format_exc()}"
            )
            if broadcast_to_redis and not self.suppress_gateway_emission:
                sim_id = self.session_simulation_map.get(session_id)
                err_text = f"Simulation Error: {run_err}"
                try:
                    with simulation_registry.user_scope(resolved_user or None):
                        await pulses_util.emit_gateway_message(
                            origin={
                                "type": "agent",
                                "id": self.agent_type,
                                "session_id": session_id,
                            },
                            destination=[session_id],
                            status="error",
                            msg_type="json",
                            event="text",
                            data={"text": err_text},
                            simulation_id=sim_id,
                        )
                        await pulses_util.emit_gateway_message(
                            origin={
                                "type": "agent",
                                "id": self.agent_type,
                                "session_id": session_id,
                            },
                            destination=[session_id],
                            status="success",
                            msg_type="json",
                            event="run_end",
                            data={"agent": self.agent_type, "status": "error", "error": str(run_err)},
                            simulation_id=sim_id,
                        )
                except Exception as emit_err:
                    logger.error(f"Failed to emit error pulse to gateway: {emit_err}")
        finally:
            try:
                otel_context.detach(token)
            except Exception as e:
                # This can happen during task cancellation or system shutdown
                logger.debug(f"ORCHESTRATION_DEBUG: OTEL context detach failed (likely noise): {e}")

    def _prepare_pulses(self, session_id: str, event: Any) -> list[Any]:
        """Converts an agent event into a list of payloads (strings).

        Mixed-content turns (text + function_call in the same Content)
        suppress the text Part: it is mid-turn planning narration ("I'll
        now call X"), not user-facing chat. Emitting it would defeat the
        FE's `previousMessageIsA2ui` suppression invariant because text
        reaches the wire before the function_call's tool result and the
        resulting a2ui event. Terminal turns (text-only, no
        function_calls) still emit cleanly.

        See docs/plans/2026-04-18-dispatcher-text-suppression-design.md.
        """
        if event.author not in self.allowed_authors:
            logger.debug(
                f"ORCHESTRATION_FILTER: Ignoring event from author '{event.author}' (Allowed: {self.allowed_authors})"
            )
            return []

        has_function_call = (
            any(getattr(p, "function_call", None) for p in event.content.parts)
            if event.content and event.content.parts
            else False
        )

        payloads = []
        if event.content and event.content.parts:
            for p in event.content.parts:
                text = ""
                if p.text:
                    if has_function_call:
                        continue
                    if re.match(
                        r'^\s*(?:```(?:json)?\s*)?\{\s*["\']?(?:name|function)["\']?\s*:\s*["\']?[A-Za-z0-9_]+',
                        p.text,
                    ):
                        logger.debug(
                            f"ORCHESTRATION_FILTER: Suppressing raw function call JSON from chat UI for {session_id}"
                        )
                        continue
                    text = p.text
                elif getattr(p, "function_response", None) is not None and getattr(p.function_response, "response", None) is not None:
                    resp = p.function_response.response
                    try:
                        if isinstance(resp, (dict, list)):
                            text = _strict_json_dumps(resp)
                        else:
                            text = str(resp)
                    except Exception:
                        text = str(resp)

                if text:
                    logger.debug(f"ORCHESTRATION_EVENT: Relaying part from {event.author} for {session_id}")
                    payloads.append(text)

        return payloads
