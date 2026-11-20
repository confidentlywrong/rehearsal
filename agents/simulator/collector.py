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

"""RaceCollector — Redis-backed telemetry collector for multi-instance AE.

Subscribes to ``gateway:broadcast`` (protobuf-encoded ``gateway.Wrapper``
messages) and filters for messages originating from specific runner session IDs.
Matching messages are buffered in a **Redis list** so that ``drain()`` works
from any Agent Engine instance, not just the one that started the collector.

The ``drain()`` method atomically reads and clears the Redis buffer, returning
whatever arrived since the last drain (approximate sampling).
"""

import asyncio
import json
import logging
import math
import os
from typing import Any, Optional

from redis.asyncio.client import PubSub, Redis

from gen_proto.gateway import gateway_pb2

from agents.utils.redis_pool import get_shared_redis_client
from agents.utils import simulation_registry
from agents.utils import tenant_keys

logger = logging.getLogger(__name__)

# Module-level registry of locally-owned collectors (PubSub subscriptions).
# Only the instance that called start() has an entry here.
# SAFETY: All access is from async methods in a single asyncio event loop,
# so dict operations are effectively atomic (CPython GIL). No lock needed
# unless multi-threaded access is introduced.
_instances: dict[str, "RaceCollector"] = {}

# Legacy literal prefixes — kept only as fallbacks for SAAS_MODE=off.
# Tenant-aware key builders below produce the actual Redis keys.
_BUFFER_PREFIX = "collector:buffer:"
_ACTIVE_PREFIX = "collector:active:"


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


def _parse_buffer_max_len(default: int = 10000) -> int:
    """Parse ``COLLECTOR_BUFFER_MAX_LEN`` safely without crashing at import."""
    raw = os.environ.get("COLLECTOR_BUFFER_MAX_LEN")
    if raw is None or not raw.strip():
        return max(100, default)
    try:
        return max(100, int(raw.strip()))
    except (TypeError, ValueError):
        logger.warning(
            "RaceCollector: invalid COLLECTOR_BUFFER_MAX_LEN=%r; using default %d",
            raw,
            default,
        )
        return max(100, default)


# Per-tenant cap on the buffered telemetry list. A bad/runaway tenant can
# otherwise rpush unbounded data into one Redis list and exhaust memory.
# 10 000 entries covers ~3–5 minutes of 1000-runner simulations at typical
# tick rates; older entries are dropped LTRIM-style. Operator override via
# COLLECTOR_BUFFER_MAX_LEN env var.
_BUFFER_MAX_LEN = _parse_buffer_max_len()


def _decode_buffer_items(raw_items: Any, *, session_id: str) -> list[dict]:
    """Decode raw Redis buffer entries item-by-item, skipping bad entries."""
    if not raw_items:
        return []
    decoded: list[dict] = []
    for item in raw_items:
        try:
            if isinstance(item, bytes):
                item_str = item.decode("utf-8")
            elif isinstance(item, str):
                item_str = item
            else:
                logger.warning(
                    "RaceCollector[%s]: skipping unsupported buffer item type %s",
                    session_id,
                    type(item).__name__,
                )
                continue
            parsed = json.loads(item_str)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "RaceCollector[%s]: skipping malformed telemetry item (%s)",
                session_id,
                exc,
            )
            continue
        if not isinstance(parsed, dict):
            logger.warning(
                "RaceCollector[%s]: skipping non-dict telemetry item (%s)",
                session_id,
                type(parsed).__name__,
            )
            continue
        decoded.append(parsed)
    return decoded


def _dispatcher_user_id(user_id: Optional[str] = None) -> str:
    """Resolve effective tenant user_id from explicit arg, simreg.user_scope, or DISPATCHER_USER_ID.

    Read on every call so test ``monkeypatch.setenv`` and ``user_scope``
    mutations take effect without a re-import.
    """
    return simulation_registry._resolve_registry_user(user_id, require_when_saas=False)


def _instance_key(session_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-aware in-process registry key for ``_instances``.

    SAAS_MODE=off or anonymous/unbound -> ``session_id`` (backward-compatible).
    SAAS_MODE=on + resolved user -> ``{ACCOUNT_KEY_PREFIX}:{user}:collector:instance:{session_id}``.
    """
    eff_user = _dispatcher_user_id(user_id)
    if tenant_keys._saas_mode_enabled() and eff_user:
        return tenant_keys.key_for_user(eff_user, "collector", "instance", session_id)
    return session_id


def _buffer_key(session_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-aware Redis key for a collector's session buffer."""
    return tenant_keys.key_for_user(_dispatcher_user_id(user_id), "collector", "buffer", session_id)


def _active_key(session_id: str, user_id: Optional[str] = None) -> str:
    """Tenant-aware Redis key marking a collector as active."""
    return tenant_keys.key_for_user(_dispatcher_user_id(user_id), "collector", "active", session_id)


def _gateway_broadcast_channel(user_id: Optional[str] = None) -> str:
    """Tenant-aware name of the gateway broadcast pub/sub channel.

    SAAS_MODE=off → ``gateway:broadcast`` (legacy).
    SAAS_MODE=on  → ``{ACCOUNT_KEY_PREFIX}:{user}:gateway:broadcast``.
    """
    return tenant_keys.key_for_user(_dispatcher_user_id(user_id), "gateway", "broadcast")


class _DrainProxy:
    """Lightweight proxy for draining the collector from a non-owning instance.

    When ``advance_tick`` runs on a different AE instance than the one
    that started the collector, ``RaceCollector.get()`` returns this proxy.
    It can ``drain()`` the shared Redis buffer but does not own the PubSub
    subscription.
    """

    def __init__(self, session_id: str, redis_client: Redis, user_id: Optional[str] = None) -> None:
        self.session_id = session_id
        self._redis = redis_client
        self._user_id: Optional[str] = _dispatcher_user_id(user_id) or None
        self._buffer_key = _buffer_key(session_id, user_id=self._user_id)
        self._active_key = _active_key(session_id, user_id=self._user_id)

    async def drain(self) -> list[dict]:
        """Atomically read and clear the Redis buffer."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.lrange(self._buffer_key, 0, -1)
            pipe.delete(self._buffer_key)
            results = await pipe.execute()
        raw_items = results[0] if results else []
        return _decode_buffer_items(raw_items, session_id=self.session_id)

    async def stop(self) -> None:
        """Clean up Redis keys (remote stop — no PubSub to cancel)."""
        try:
            await self._redis.delete(self._buffer_key)
            await self._redis.delete(self._active_key)
        except Exception:
            logger.warning("RaceCollector (proxy): Redis cleanup failed for %s", self.session_id, exc_info=True)
        logger.info("RaceCollector (proxy) stopped for session %s", self.session_id)


class RaceCollector:
    """Collects gateway broadcast messages for a set of runner sessions.

    The PubSub subscription runs on the instance that called ``start()``.
    Matching messages are pushed to a **Redis list** so that any instance
    can ``drain()`` them.
    """

    def __init__(
        self,
        session_id: str,
        runner_session_ids: set[str],
        *,
        skip_pubsub: bool = False,
        user_id: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.runner_session_ids = runner_session_ids
        self.skip_pubsub = skip_pubsub
        self._pubsub: Optional[PubSub] = None
        self._redis: Optional[Redis] = None
        self._task: Optional[asyncio.Task] = None
        self._user_id: Optional[str] = _dispatcher_user_id(user_id) or None
        self._instance_key: str = _instance_key(session_id, user_id=self._user_id)
        self._buffer_key = _buffer_key(session_id, user_id=self._user_id)
        self._active_key = _active_key(session_id, user_id=self._user_id)
        self._ltrim_counter: int = 0

    # ------------------------------------------------------------------
    # Factory / registry
    # ------------------------------------------------------------------

    @classmethod
    async def start(
        cls,
        session_id: str,
        runner_session_ids: set[str],
        *,
        skip_pubsub: bool = False,
        user_id: Optional[str] = None,
    ) -> "RaceCollector":
        """Create a collector, connect to Redis, subscribe, and start background task.

        Args:
            session_id: The simulation session ID.
            runner_session_ids: Set of runner session IDs to track.
            skip_pubsub: When True, skip the PubSub subscription and background
                collect loop.  Runners write directly via RPUSH so the PubSub
                path is redundant for autopilot simulations and adds Redis
                contention at scale.
            user_id: Optional explicit tenant user_id (falls back to
                ``simulation_registry.user_scope`` / ``DISPATCHER_USER_ID``).
        """
        instance = cls(session_id, runner_session_ids, skip_pubsub=skip_pubsub, user_id=user_id)
        inst_key = instance._instance_key
        r = get_shared_redis_client()
        if r is None:
            raise RuntimeError("Cannot start RaceCollector: REDIS_ADDR not configured")
        instance._redis = r

        existing = _instances.pop(inst_key, None)
        if existing is not None:
            try:
                await existing.stop()
            except Exception:
                logger.warning(
                    "RaceCollector: failed to stop existing collector for %s before restart",
                    session_id,
                    exc_info=True,
                )

        # L3: Delete stale buffer from a previous collector that may have
        # crashed without cleanup. Prevents old telemetry from being drained.
        try:
            await r.delete(instance._buffer_key)
        except Exception as e:
            logger.error(
                "RaceCollector: failed to clear stale buffer %s: %s",
                instance._buffer_key,
                e,
                exc_info=True,
            )
            raise

        try:
            if not skip_pubsub:
                instance._pubsub = r.pubsub()
                assert instance._pubsub is not None
                await instance._pubsub.subscribe(_gateway_broadcast_channel(user_id=instance._user_id))
                instance._task = asyncio.create_task(instance._collect_loop())

            # Mark as active in Redis so other instances know a collector exists.
            await r.set(instance._active_key, "1", ex=7200)
        except Exception:
            if instance._task is not None:
                instance._task.cancel()
                try:
                    await instance._task
                except (asyncio.CancelledError, Exception):
                    pass
                instance._task = None
            if instance._pubsub is not None:
                try:
                    await instance._pubsub.close()
                except Exception:
                    logger.debug(
                        "RaceCollector: PubSub close failed during start rollback for %s",
                        session_id,
                    )
                instance._pubsub = None
            instance._redis = None
            raise

        _instances[inst_key] = instance
        logger.info(
            "RaceCollector started for session %s (tracking %d runners, skip_pubsub=%s)",
            session_id,
            len(runner_session_ids),
            skip_pubsub,
        )
        return instance

    @classmethod
    def is_running(cls, session_id: str, user_id: Optional[str] = None) -> bool:
        """Return True if a local RaceCollector is active for *session_id* (and tenant)."""
        return _instance_key(session_id, user_id=user_id) in _instances

    @classmethod
    def get(cls, session_id: str, user_id: Optional[str] = None) -> Optional["RaceCollector | _DrainProxy"]:
        """Look up a collector by session_id (and tenant user_id).

        Returns the local collector if this instance owns it, otherwise a
        ``_DrainProxy`` that can drain the shared Redis buffer from any
        AE instance.
        """
        local = _instances.get(_instance_key(session_id, user_id=user_id))
        if local is not None:
            return local

        # Cross-instance: return a drain proxy if Redis is available.
        r = get_shared_redis_client()
        if r is None:
            return None
        return _DrainProxy(session_id, r, user_id=user_id)

    # ------------------------------------------------------------------
    # Background collection
    # ------------------------------------------------------------------

    async def _collect_loop(self) -> None:
        """Background task: read from pubsub, deserialize, filter, push to Redis."""
        try:
            assert self._pubsub is not None
            assert self._redis is not None
            while True:
                try:
                    msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error(
                        "RaceCollector: PubSub read failed for session %s",
                        self.session_id,
                        exc_info=True,
                    )
                    break
                if msg is not None and msg.get("type") == "message":
                    try:
                        wrapper = gateway_pb2.Wrapper()
                        raw = msg.get("data")
                        if raw is None:
                            continue
                        wrapper.ParseFromString(raw)
                        parsed = self._parse_wrapper(wrapper)
                        if parsed is not None:
                            await self._redis.rpush(  # type: ignore[misc]
                                self._buffer_key,
                                _strict_json_dumps(parsed, default=str),
                            )
                            # M5: hard-cap the buffer length so a runaway
                            # tenant cannot exhaust Redis memory by
                            # rpushing faster than drain() consumes.
                            # LTRIM keeps the LAST _BUFFER_MAX_LEN entries.
                            # Only LTRIM every 100 pushes to reduce Redis round-trips.
                            self._ltrim_counter += 1
                            if self._ltrim_counter % 100 == 0:
                                await self._redis.ltrim(  # type: ignore[misc]
                                    self._buffer_key, -_BUFFER_MAX_LEN, -1
                                )
                            await self._redis.expire(self._buffer_key, 7200)
                    except Exception:
                        logger.warning("RaceCollector: failed to parse broadcast message", exc_info=True)
        except asyncio.CancelledError:
            logger.debug("RaceCollector collect loop cancelled for session %s", self.session_id)

    # ------------------------------------------------------------------
    # Parsing / filtering
    # ------------------------------------------------------------------

    def _parse_wrapper(self, wrapper: gateway_pb2.Wrapper) -> Optional[dict]:
        """Filter wrapper by runner session IDs and return structured dict or None."""
        origin_session_id = wrapper.origin.session_id
        if origin_session_id not in self.runner_session_ids:
            return None

        # Decode payload as JSON
        try:
            payload = json.loads(wrapper.payload) if wrapper.payload else {}
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        return {
            "session_id": origin_session_id,
            "agent_id": wrapper.origin.id,
            "event": wrapper.event,
            "msg_type": wrapper.type,
            "timestamp": wrapper.timestamp,
            "payload": payload,
        }

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    async def drain(self) -> list[dict]:
        """Atomically read and clear the Redis buffer."""
        r = self._redis or get_shared_redis_client()
        if r is None:
            return []
        async with r.pipeline(transaction=True) as pipe:
            pipe.lrange(self._buffer_key, 0, -1)
            pipe.delete(self._buffer_key)
            results = await pipe.execute()
        raw_items = results[0] if results else []
        return _decode_buffer_items(raw_items, session_id=self.session_id)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Cancel background task, close PubSub, clean up Redis keys."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._pubsub is not None:
            # Skip unsubscribe() — the server automatically cleans up
            # subscriptions when the connection closes.  After task.cancel()
            # the PubSub connection may be in a corrupted state (partial
            # read in flight), so any write/read on it can raise
            # ConnectionError.  Just close the connection.
            try:
                await self._pubsub.close()
            except Exception:
                logger.debug("RaceCollector: PubSub close failed for %s (expected after cancel)", self.session_id)
            self._pubsub = None

        # Clean up Redis buffer and active marker
        r = self._redis or get_shared_redis_client()
        if r is not None:
            try:
                active_key = getattr(self, "_active_key", None) or _active_key(
                    self.session_id, user_id=getattr(self, "_user_id", None)
                )
                await r.delete(self._buffer_key)
                await r.delete(active_key)
            except Exception:
                logger.warning("RaceCollector: Redis cleanup failed for %s", self.session_id, exc_info=True)

        # Do NOT close self._redis — it is the shared pool client.
        self._redis = None

        inst_key = getattr(self, "_instance_key", None) or _instance_key(
            self.session_id, user_id=getattr(self, "_user_id", None)
        )
        if _instances.get(inst_key) is self:
            _instances.pop(inst_key, None)
        logger.info("RaceCollector stopped for session %s", self.session_id)
