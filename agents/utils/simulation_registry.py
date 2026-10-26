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

"""Distributed simulation registry backed by Redis.

Maps session_id → simulation_id across all Agent Engine instances.
Uses per-key Redis STRINGs with a 2-hour TTL as the source of truth,
with a process-local L1 cache for fast reads.  Falls back to
process-local only when Redis is unavailable (unit tests, local dev
without Redis).

Thread-safety: CPython dict get/set are GIL-protected.  Redis operations
are async and use the shared connection pool.

Single-flight (2026-06-24): both ``lookup`` and ``get_context_id`` use
an in-flight Future map keyed by the lookup id, so 1000 concurrent L1
misses for the same key collapse to a single Redis read instead of
1000.  Without this, the in-process runner_autopilot dispatcher (which
fans out up to 1000 runners that each emit several DashLog events per
tick) overwhelms the Redis pool during the cold-cache burst.

Bounded L1 (2026-06-24): ``_local`` and ``_context_map`` are now
``OrderedDict``s capped at ``SIMREG_L1_MAX_ENTRIES`` (default 10 000).
The Redis side already expires after 2 h, but the local cache had no
ceiling — a runner_autopilot instance kept warm for several days
accumulated one entry per (session_id, context_id) it ever observed
and eventually OOM'd. FIFO eviction keeps memory bounded while still
serving the hot recent set; an evicted key just becomes a Redis
re-read on the next access, gated by single-flight.
"""

import asyncio
import logging
import os
from collections import OrderedDict

from agents.utils.redis_pool import get_shared_redis_client
from agents.utils import tenant_keys

logger = logging.getLogger(__name__)

# Legacy un-prefixed prefixes — kept for back-compat scanning in clear()
# and for callers that still reference them by name (e.g. broadcast.py).
# In SAAS_MODE=on the actual Redis keys are computed via
# tenant_keys.key_for_user(_dispatcher_user_id(), "simreg", ...) so each
# tenant gets its own keyspace; the literal prefixes below collapse to
# the legacy form when SAAS_MODE=off.
_REDIS_SESSION_PREFIX = "simreg:session:"
_REDIS_CONTEXT_PREFIX = "simreg:context:"
_TTL_SECONDS = 7200


def _dispatcher_user_id() -> str:
    """Mirror of ``agents.utils.dispatcher._dispatcher_user_id``.

    Read on every call so test ``monkeypatch.setenv`` mutations take
    effect without a re-import.
    """
    return os.environ.get("DISPATCHER_USER_ID", "").strip()


def _session_key(session_id: str) -> str:
    """Tenant-aware Redis key for a session→simulation mapping.

    SAAS_MODE=off → ``simreg:session:<id>`` (legacy, byte-identical).
    SAAS_MODE=on  → ``{ACCOUNT_KEY_PREFIX}:{user}:simreg:session:<id>``.
    """
    return tenant_keys.key_for_user(_dispatcher_user_id(), "simreg", "session", session_id)


def _context_key(vertex_session_id: str) -> str:
    """Tenant-aware Redis key for a Vertex session→context mapping."""
    return tenant_keys.key_for_user(_dispatcher_user_id(), "simreg", "context", vertex_session_id)


# Cap on the in-process L1 caches. Configurable so an operator can
# raise it without a redeploy if the eviction warning fires steadily.
_L1_MAX_ENTRIES = max(1, int(os.environ.get("SIMREG_L1_MAX_ENTRIES", "10000")))

# Process-local L1 caches — populated on register()/lookup(), used as
# fast-path. OrderedDict + FIFO eviction caps memory growth.
_local: "OrderedDict[str, str]" = OrderedDict()

# Maps Vertex AI session_id → original A2A context_id (spawn session UUID).
# On Agent Engine, VertexAiSessionService generates its own session IDs that
# differ from the gateway's spawn session IDs.  DashLogPlugin uses this
# mapping to emit events with the original spawn ID so the frontend's
# session-based rendering filters work.
_context_map: "OrderedDict[str, str]" = OrderedDict()

# Single-flight in-flight maps. Keyed by the lookup id; value is a Future
# resolved with the Redis read result (or None on miss / error). A second
# concurrent caller for the same key awaits the existing Future instead of
# issuing its own Redis read.  Entries are removed in the lookup's finally
# block so subsequent calls re-check L1 (which may have been warmed in the
# meantime).
_inflight_session: dict[str, "asyncio.Future[str | None]"] = {}
_inflight_context: dict[str, "asyncio.Future[str | None]"] = {}


def _set_l1(cache: "OrderedDict[str, str]", key: str, value: str) -> None:
    """Insert/update a value in an L1 cache and evict FIFO if needed.

    A re-insert moves the key to the most-recent end so it survives
    the next eviction cycle (the common case is "register then lookup
    a few times" — those lookups should renew the entry's TTL in L1).
    """
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > _L1_MAX_ENTRIES:
        evicted, _ = cache.popitem(last=False)
        logger.debug("simreg: L1 evicted oldest key %s (cap=%d)", evicted, _L1_MAX_ENTRIES)


async def register(session_id: str, simulation_id: str) -> None:
    """Associate a session with a simulation (writes to Redis + L1)."""
    _set_l1(_local, session_id, simulation_id)
    r = get_shared_redis_client()
    if r is not None:
        try:
            await r.setex(_session_key(session_id), _TTL_SECONDS, simulation_id)  # type: ignore[misc]
        except Exception:
            logger.warning("simreg: Redis write failed for %s", session_id, exc_info=True)


async def lookup(session_id: str) -> str | None:
    """Return the simulation_id for a session, or None.

    Checks the process-local L1 cache first, then falls back to Redis
    so that lookups succeed even when the register() happened on a
    different Agent Engine instance.

    Concurrent L1 misses for the same session_id collapse to a single
    Redis GET via the ``_inflight_session`` Future map (single-flight).
    """
    # L1 fast-path
    result = _local.get(session_id)
    if result is not None:
        return result

    # Single-flight: if another task is already loading this key, wait for it.
    pending = _inflight_session.get(session_id)
    if pending is not None:
        return await pending

    # We're the first miss for this key; install a Future for other tasks to await.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()
    _inflight_session[session_id] = fut
    try:
        decoded: str | None = None
        r = get_shared_redis_client()
        if r is not None:
            try:
                val = await r.get(_session_key(session_id))  # type: ignore[misc]
                if val is not None:
                    decoded = val if isinstance(val, str) else val.decode()
                    _set_l1(_local, session_id, decoded)  # warm L1
            except Exception:
                logger.warning("simreg: Redis read failed for %s", session_id, exc_info=True)
        if not fut.done():
            fut.set_result(decoded)
        return decoded
    except BaseException as exc:
        # Propagate cancellation/errors to any awaiters so they don't hang forever.
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _inflight_session.pop(session_id, None)


async def register_context(vertex_session_id: str, context_id: str) -> None:
    """Map a Vertex AI session_id back to the original A2A context_id.

    Called by SimulationExecutor after SessionManager creates a session.
    DashLogPlugin calls ``get_context_id()`` to retrieve the original
    spawn session UUID for the protobuf origin field.
    """
    _set_l1(_context_map, vertex_session_id, context_id)
    r = get_shared_redis_client()
    if r is not None:
        try:
            await r.setex(_context_key(vertex_session_id), _TTL_SECONDS, context_id)  # type: ignore[misc]
        except Exception:
            logger.warning("simreg: Redis context write failed for %s", vertex_session_id, exc_info=True)


async def get_context_id(vertex_session_id: str) -> str | None:
    """Return the original context_id for a Vertex AI session, or None.

    Concurrent L1 misses for the same vertex_session_id collapse to a
    single Redis GET via the ``_inflight_context`` Future map
    (single-flight). This is the hot path for ``DashLogPlugin`` events
    on the in-process runner_autopilot dispatcher under 1000-runner
    bursts.
    """
    result = _context_map.get(vertex_session_id)
    if result is not None:
        return result

    pending = _inflight_context.get(vertex_session_id)
    if pending is not None:
        return await pending

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str | None] = loop.create_future()
    _inflight_context[vertex_session_id] = fut
    try:
        decoded: str | None = None
        r = get_shared_redis_client()
        if r is not None:
            try:
                val = await r.get(_context_key(vertex_session_id))  # type: ignore[misc]
                if val is not None:
                    decoded = val if isinstance(val, str) else val.decode()
                    _set_l1(_context_map, vertex_session_id, decoded)
            except Exception:
                logger.warning("simreg: Redis context read failed for %s", vertex_session_id, exc_info=True)
        if not fut.done():
            fut.set_result(decoded)
        return decoded
    except BaseException as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _inflight_context.pop(vertex_session_id, None)


async def unregister(session_id: str) -> None:
    """Remove a session from the registry (L1 + Redis)."""
    _local.pop(session_id, None)
    _context_map.pop(session_id, None)  # M12: prevent memory leak
    r = get_shared_redis_client()
    if r is not None:
        try:
            await r.delete(  # type: ignore[misc]
                _session_key(session_id),
                _context_key(session_id),
            )
        except Exception:
            logger.warning("simreg: Redis unregister failed for %s", session_id, exc_info=True)


async def clear() -> None:
    """Remove all entries (for environment_reset)."""
    _local.clear()
    _context_map.clear()
    # Drop in-flight Futures too; the lookups that own them will detect their
    # entry is gone but still resolve their own Future, which is fine for any
    # already-awaiting callers (they get whatever the original Redis read
    # returned, post-clear).
    for fut in _inflight_session.values():
        if not fut.done():
            fut.cancel()
    _inflight_session.clear()

    for fut in _inflight_context.values():
        if not fut.done():
            fut.cancel()
    _inflight_context.clear()
    r = get_shared_redis_client()
    if r is not None:
        try:
            # Build per-tenant scan patterns so SAAS_MODE=on with
            # DISPATCHER_USER_ID only clears that tenant's namespace, while
            # SAAS_MODE=off still scans the legacy un-prefixed keyspace.
            scan_patterns = (
                f"{_session_key('')}*",
                f"{_context_key('')}*",
            )
            for pattern in scan_patterns:
                batch: list = []
                async for key in r.scan_iter(match=pattern, count=100):  # type: ignore[misc]
                    batch.append(key)
                    if len(batch) >= 100:
                        await r.delete(*batch)  # type: ignore[misc]
                        batch.clear()
                if batch:
                    await r.delete(*batch)  # type: ignore[misc]
        except Exception:
            logger.warning("simreg: Redis clear failed", exc_info=True)
