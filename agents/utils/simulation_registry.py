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
import contextvars
from contextlib import contextmanager
import logging
import os
from collections import OrderedDict
from typing import Iterator, Optional

from agents.utils.redis_pool import get_shared_redis_client
from agents.utils import tenant_keys

logger = logging.getLogger(__name__)

_ANONYMOUS_USER_IDS = frozenset({"simulation", "simulation_user", "anonymous", "default", "user"})
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("simreg_user_id", default="")

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


@contextmanager
def user_scope(user_id: Optional[str]) -> Iterator[None]:
    """Bind a tenant user_id to the current async/execution context for simreg helpers."""
    cleaned = user_id.strip() if isinstance(user_id, str) else ""
    token = _current_user_id.set(cleaned)
    try:
        yield
    finally:
        _current_user_id.reset(token)


def get_current_user_id() -> str:
    """Return the active tenant user_id bound via :func:`user_scope`, or ``""``."""
    return _current_user_id.get("").strip()


def _resolve_registry_user(
    user_id: Optional[str] = None,
    *,
    require_when_saas: bool = False,
) -> str:
    """Resolve effective tenant user_id from explicit arg, contextvar, or DISPATCHER_USER_ID."""
    if user_id is not None:
        if not isinstance(user_id, str):
            raise ValueError(f"simreg: user_id must be a string, got {type(user_id).__name__}")
        candidate = user_id.strip()
    else:
        ctx_uid = _current_user_id.get("").strip()
        candidate = ctx_uid if ctx_uid else _dispatcher_user_id()

    if tenant_keys._saas_mode_enabled():
        if candidate:
            if candidate.lower() in _ANONYMOUS_USER_IDS:
                if require_when_saas:
                    raise ValueError(
                        f"Authenticated user_id is required in simulation_registry when SAAS_MODE=on (got {candidate!r})"
                    )
                return ""
            return tenant_keys._validate_component(candidate, "user_id")
        if require_when_saas:
            raise ValueError("Authenticated user_id is required in simulation_registry when SAAS_MODE=on")
    return candidate


def _session_key(session_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-aware Redis key for a session→simulation mapping.

    SAAS_MODE=off → ``simreg:session:<id>`` (legacy, byte-identical).
    SAAS_MODE=on  → ``{ACCOUNT_KEY_PREFIX}:{user}:simreg:session:<id>``.
    """
    eff_user = _resolve_registry_user(user_id, require_when_saas=False)
    return tenant_keys.key_for_user(eff_user, "simreg", "session", session_id)


def _context_key(vertex_session_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-aware Redis key for a Vertex session→context mapping."""
    eff_user = _resolve_registry_user(user_id, require_when_saas=False)
    return tenant_keys.key_for_user(eff_user, "simreg", "context", vertex_session_id)


def _l1_key(raw_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-aware L1 cache key matching Redis key scoping.

    When SAAS_MODE=on and a tenant user_id is resolved, namespaces L1 entries by
    tenant prefix so in-process multi-tenant calls cannot collide. Collapses
    to ``raw_id`` when SAAS_MODE=off or no tenant user_id is available.
    """
    eff_user = _resolve_registry_user(user_id, require_when_saas=False)
    if eff_user and tenant_keys._saas_mode_enabled():
        prefix = tenant_keys._key_prefix_for_user(eff_user)
        if prefix:
            return f"{prefix}:{raw_id}"
    return raw_id


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("simreg: invalid integer for %s=%r; using default %d", name, raw, default)
        return default
    if parsed < 1:
        logger.warning("simreg: non-positive %s=%d; clamping to 1", name, parsed)
        return 1
    return parsed


# Cap on the in-process L1 caches. Configurable so an operator can
# raise it without a redeploy if the eviction warning fires steadily.
_L1_MAX_ENTRIES = _parse_positive_int_env("SIMREG_L1_MAX_ENTRIES", 10000)

# Process-local L1 caches — populated on register()/lookup(), used as
# fast-path. OrderedDict + FIFO eviction caps memory growth.
_local: "OrderedDict[str, str]" = OrderedDict()

# Maps Vertex AI session_id → original A2A context_id (spawn session UUID).
# On Agent Engine, VertexAiSessionService generates its own session IDs that
# differ from the gateway's spawn session IDs.  DashLogPlugin uses this
# mapping to emit events with the original spawn ID so the frontend's
# session-based rendering filters work.
_context_map: "OrderedDict[str, str]" = OrderedDict()

# Single-flight in-flight maps. Keyed by (id(loop), lookup_id) so that
# concurrent L1 misses on different event loops (e.g. FastAPI's main loop
# vs RedisOrchestratorDispatcher's background loop) never await a Future
# bound to another loop.
_inflight_session: dict[tuple[int, str], "asyncio.Future[str | None]"] = {}
_inflight_context: dict[tuple[int, str], "asyncio.Future[str | None]"] = {}


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


async def register(session_id: str, simulation_id: str, user_id: Optional[str] = None) -> None:
    """Associate a session with a simulation (writes to Redis + L1)."""
    eff_user = _resolve_registry_user(user_id, require_when_saas=True)
    _set_l1(_local, _l1_key(session_id, user_id=eff_user), simulation_id)
    r = get_shared_redis_client()
    if r is not None:
        try:
            await r.setex(_session_key(session_id, user_id=eff_user), _TTL_SECONDS, simulation_id)  # type: ignore[misc]
        except Exception:
            logger.error("simreg: Redis write failed for %s", session_id, exc_info=True)


def _consume_unretrieved_exception(fut: "asyncio.Future[str | None]") -> None:
    """Retrieve exception on abandoned single-flight Futures so asyncio does not log leaks."""
    if not fut.cancelled():
        try:
            fut.exception()
        except BaseException:
            pass


async def lookup(session_id: str, user_id: Optional[str] = None) -> str | None:
    """Return the simulation_id for a session, or None.

    Checks the process-local L1 cache first, then falls back to Redis
    so that lookups succeed even when the register() happened on a
    different Agent Engine instance.

    Concurrent L1 misses for the same session_id collapse to a single
    Redis GET via the ``_inflight_session`` Future map (single-flight).
    """
    eff_user = _resolve_registry_user(user_id, require_when_saas=True)
    l1_key = _l1_key(session_id, user_id=eff_user)
    # L1 fast-path
    result = _local.get(l1_key)
    if result is not None:
        return result

    loop = asyncio.get_running_loop()
    inflight_key = (id(loop), l1_key)

    # Single-flight: if another task on this loop is already loading this key, wait for it.
    pending = _inflight_session.get(inflight_key)
    if pending is not None:
        return await pending

    # We're the first miss for this key on this loop; install a Future for other tasks to await.
    fut: asyncio.Future[str | None] = loop.create_future()
    fut.add_done_callback(_consume_unretrieved_exception)
    _inflight_session[inflight_key] = fut
    try:
        decoded: str | None = None
        r = get_shared_redis_client()
        if r is not None:
            try:
                val = await r.get(_session_key(session_id, user_id=eff_user))  # type: ignore[misc]
                if val is not None:
                    decoded = val if isinstance(val, str) else val.decode()
                    _set_l1(_local, l1_key, decoded)  # warm L1
            except Exception:
                logger.error("simreg: Redis read failed for %s", session_id, exc_info=True)
        if not fut.done():
            fut.set_result(decoded)
        return decoded
    except BaseException as exc:
        # Propagate cancellation/errors to any awaiters so they don't hang forever.
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _inflight_session.pop(inflight_key, None)


async def register_context(vertex_session_id: str, context_id: str, user_id: Optional[str] = None) -> None:
    """Map a Vertex AI session_id back to the original A2A context_id.

    Called by SimulationExecutor after SessionManager creates a session.
    DashLogPlugin calls ``get_context_id()`` to retrieve the original
    spawn session UUID for the protobuf origin field.
    """
    eff_user = _resolve_registry_user(user_id, require_when_saas=True)
    _set_l1(_context_map, _l1_key(vertex_session_id, user_id=eff_user), context_id)
    r = get_shared_redis_client()
    if r is not None:
        try:
            await r.setex(_context_key(vertex_session_id, user_id=eff_user), _TTL_SECONDS, context_id)  # type: ignore[misc]
        except Exception:
            logger.error("simreg: Redis context write failed for %s", vertex_session_id, exc_info=True)


async def get_context_id(vertex_session_id: str, user_id: Optional[str] = None) -> str | None:
    """Return the original context_id for a Vertex AI session, or None.

    Concurrent L1 misses for the same vertex_session_id collapse to a
    single Redis GET via the ``_inflight_context`` Future map
    (single-flight). This is the hot path for ``DashLogPlugin`` events
    on the in-process runner_autopilot dispatcher under 1000-runner
    bursts.
    """
    eff_user = _resolve_registry_user(user_id, require_when_saas=True)
    l1_key = _l1_key(vertex_session_id, user_id=eff_user)
    result = _context_map.get(l1_key)
    if result is not None:
        return result

    loop = asyncio.get_running_loop()
    inflight_key = (id(loop), l1_key)

    pending = _inflight_context.get(inflight_key)
    if pending is not None:
        return await pending

    fut: asyncio.Future[str | None] = loop.create_future()
    fut.add_done_callback(_consume_unretrieved_exception)
    _inflight_context[inflight_key] = fut
    try:
        decoded: str | None = None
        r = get_shared_redis_client()
        if r is not None:
            try:
                val = await r.get(_context_key(vertex_session_id, user_id=eff_user))  # type: ignore[misc]
                if val is not None:
                    decoded = val if isinstance(val, str) else val.decode()
                    _set_l1(_context_map, l1_key, decoded)
            except Exception:
                logger.error("simreg: Redis context read failed for %s", vertex_session_id, exc_info=True)
        if not fut.done():
            fut.set_result(decoded)
        return decoded
    except BaseException as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _inflight_context.pop(inflight_key, None)


async def unregister(session_id: str, user_id: Optional[str] = None) -> None:
    """Remove a session from the registry (L1 + Redis)."""
    eff_user = _resolve_registry_user(user_id, require_when_saas=True)
    l1_key = _l1_key(session_id, user_id=eff_user)
    _local.pop(l1_key, None)
    _context_map.pop(l1_key, None)  # M12: prevent memory leak
    r = get_shared_redis_client()
    if r is not None:
        try:
            await r.delete(  # type: ignore[misc]
                _session_key(session_id, user_id=eff_user),
                _context_key(session_id, user_id=eff_user),
            )
        except Exception:
            logger.error("simreg: Redis unregister failed for %s", session_id, exc_info=True)


async def clear(user_id: Optional[str] = None) -> None:
    """Remove entries (for environment_reset)."""
    eff_user = _resolve_registry_user(user_id, require_when_saas=False)
    saas_on = tenant_keys._saas_mode_enabled()

    if saas_on and not eff_user:
        logger.warning("simreg: skipping global clear in SAAS_MODE=on without user_id")
        return

    def _cancel_future_safely(fut: "asyncio.Future[str | None]") -> None:
        if fut.done():
            return
        try:
            fut_loop = fut.get_loop()
            try:
                cur_loop = asyncio.get_running_loop()
            except RuntimeError:
                cur_loop = None
            if fut_loop is cur_loop or not fut_loop.is_running():
                fut.cancel()
            else:
                fut_loop.call_soon_threadsafe(fut.cancel)
        except Exception:
            logger.debug("simreg: failed to cancel in-flight future during clear()", exc_info=True)

    if saas_on:
        tenant_prefix = f"{tenant_keys._key_prefix_for_user(eff_user)}:"
        for k in [k for k in list(_local.keys()) if k.startswith(tenant_prefix)]:
            _local.pop(k, None)
        for k in [k for k in list(_context_map.keys()) if k.startswith(tenant_prefix)]:
            _context_map.pop(k, None)
        for ik in [ik for ik in list(_inflight_session.keys()) if ik[1].startswith(tenant_prefix)]:
            fut = _inflight_session.pop(ik, None)
            if fut is not None:
                _cancel_future_safely(fut)
        for ik in [ik for ik in list(_inflight_context.keys()) if ik[1].startswith(tenant_prefix)]:
            fut = _inflight_context.pop(ik, None)
            if fut is not None:
                _cancel_future_safely(fut)
    else:
        _local.clear()
        _context_map.clear()
        for fut in list(_inflight_session.values()):
            _cancel_future_safely(fut)
        _inflight_session.clear()

        for fut in list(_inflight_context.values()):
            _cancel_future_safely(fut)
        _inflight_context.clear()

    r = get_shared_redis_client()
    if r is not None:
        try:
            # Build per-tenant scan patterns so SAAS_MODE=on with
            # user_id / DISPATCHER_USER_ID only clears that tenant's namespace, while
            # SAAS_MODE=off still scans the legacy un-prefixed keyspace.
            scan_patterns = (
                f"{_session_key('', user_id=eff_user)}:*",
                f"{_context_key('', user_id=eff_user)}:*",
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
