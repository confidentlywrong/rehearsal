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

"""AlloyDB-backed route memory store.

Replaces the in-memory RouteMemoryStore with a persistent AlloyDB backend
using asyncpg. Implements the same public API so tools.py requires no changes
beyond swapping the singleton.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import uuid
import weakref
from datetime import datetime, timezone
from typing import Any

import asyncpg
from google.cloud import secretmanager

from agents.planner_with_memory.memory.schemas import PlannedRoute, SimulationRecord

logger = logging.getLogger(__name__)

_SECRET_TTL_SECONDS = 1800  # 30 minutes
_SM_SECRET = "am-db-password"

# (password, monotonic_timestamp) -- single tuple for atomic cache updates.
_cached: tuple[str, float] | None = None
_sm_client: secretmanager.SecretManagerServiceClient | None = None


def _resolve_sm_project() -> str:
    """Return the Secret Manager project, read lazily from the environment.

    Read at call time (not import) so tests can override via
    ``monkeypatch.setenv`` and operators can rotate without a process restart.
    """
    return (
        os.environ.get("SECRET_MANAGER_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("PROJECT_ID", "")
    )


def _get_sm_client() -> secretmanager.SecretManagerServiceClient:
    """Lazy-singleton for the Secret Manager gRPC client."""
    global _sm_client
    if _sm_client is None:
        _sm_client = secretmanager.SecretManagerServiceClient()
    return _sm_client


def _resolve_password() -> str:
    """Return the AlloyDB password from env var or Secret Manager with TTL cache."""
    global _cached

    env_pw = os.environ.get("ALLOYDB_PASSWORD", "")
    if env_pw:
        return env_pw

    now = time.monotonic()
    if _cached is not None and (now - _cached[1]) < _SECRET_TTL_SECONDS:
        return _cached[0]

    secret_id = os.environ.get("ALLOYDB_SECRET_NAME", _SM_SECRET).strip() or _SM_SECRET
    secret_name = f"projects/{_resolve_sm_project()}/secrets/{secret_id}/versions/latest"
    try:
        response = _get_sm_client().access_secret_version(name=secret_name)
        password = response.payload.data.decode("utf-8").strip()
        _cached = (password, time.monotonic())
        return password
    except Exception:
        if _cached is not None:
            logger.warning("Secret Manager refresh failed; using stale cached password.", exc_info=True)
            return _cached[0]
        raise ValueError(f"Could not fetch AlloyDB password from Secret Manager ({secret_name}).")


def _parse_int_env(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Safely parse an integer environment variable with optional bounds clamping."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("Invalid integer for %s=%r; falling back to %d", name, raw, default)
        return default
    if min_value is not None and value < min_value:
        if max_value is not None:
            logger.warning("Out-of-range %s=%d; falling back to %d", name, value, default)
            return default
        return min_value
    if max_value is not None and value > max_value:
        logger.warning("Out-of-range %s=%d; falling back to %d", name, value, default)
        return default
    return value


def _validate_schema_name(raw_schema: str | None, default: str = "public") -> str:
    """Validate ALLOYDB_SCHEMA as a safe PostgreSQL identifier."""
    schema_name = (raw_schema or default).strip() or default
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema_name):
        raise ValueError(f"Invalid ALLOYDB_SCHEMA identifier: {schema_name!r}")
    return schema_name


def _sanitize_score(score: float | None) -> float | None:
    """Return a finite float score or None if missing/invalid/non-finite."""
    if score is None or isinstance(score, bool):
        return None
    try:
        val = float(score)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(val):
        logger.warning("Ignoring non-finite evaluation_score: %r", score)
        return None
    return val


def _clamp_non_negative_limit(raw: object, default: int, max_limit: int = 500) -> int:
    """Safely coerce and clamp a limit parameter to [0, max_limit]."""
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            return default
        val = int(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return default
        try:
            val = int(s)
        except ValueError:
            try:
                fv = float(s)
            except (ValueError, OverflowError):
                return default
            if not math.isfinite(fv):
                return default
            val = int(fv)
    else:
        return default
    if val <= 0:
        return 0
    return min(val, max_limit)


def _strip_nonfinite(obj: Any) -> Any:
    """Recursively convert NaN/Inf floats to None so JSONB payloads remain valid RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nonfinite(v) for v in obj]
    if isinstance(obj, tuple):
        return [_strip_nonfinite(v) for v in obj]
    return obj


def _json_dumps(obj: Any, *, default: Any = None) -> str:
    """Serialize *obj* to strict JSON without NaN/Infinity tokens rejected by PostgreSQL JSONB."""
    try:
        return json.dumps(obj, allow_nan=False, default=default)
    except ValueError:
        return json.dumps(_strip_nonfinite(obj), allow_nan=False, default=default)


def _get_dsn() -> str:
    """Build a DSN from environment variables with URL-encoded credentials."""
    host = os.environ.get("ALLOYDB_HOST", "").strip()
    if not host:
        raise ValueError(
            "ALLOYDB_HOST environment variable is required for AlloyDBRouteStore. "
            "Set it to the private IP of the AlloyDB instance."
        )
    database = urllib.parse.quote(os.environ.get("ALLOYDB_DATABASE", "postgres"), safe="")
    user = urllib.parse.quote(os.environ.get("ALLOYDB_USER", "postgres"), safe="")
    password = urllib.parse.quote(_resolve_password(), safe="")
    port = _parse_int_env("ALLOYDB_PORT", 5432, min_value=1, max_value=65535)
    host_str = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"postgresql://{user}:{password}@{host_str}:{port}/{database}"


# M14 fix: Connection pool — avoids per-operation TCP+TLS+auth overhead.
# Scoped per running event loop so sequential/multi-thread loops do not collide
# or leak dead-loop pools, while preserving module-level _pool/_pool_lock/_pool_loop
# aliases for test compatibility.
_state_lock = threading.Lock()
_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncpg.Pool] = weakref.WeakKeyDictionary()
_pool_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
_pool: asyncpg.Pool | None = None
_pool_lock: asyncio.Lock | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None


def _cleanup_closed_loop_pools() -> None:
    """Terminate and remove cached pools whose event loop is already closed.

    Caller must hold ``_state_lock``.
    """
    global _pool, _pool_lock, _pool_loop
    for loop_key in [lp for lp in list(_pools) if lp.is_closed()]:
        stale_pool = _pools.pop(loop_key, None)
        _pool_locks.pop(loop_key, None)
        if stale_pool is not None and hasattr(stale_pool, "terminate"):
            try:
                stale_pool.terminate()
            except Exception:
                logger.debug("Failed terminating stale AlloyDB pool for closed loop", exc_info=True)
        if _pool is stale_pool:
            _pool = None
            _pool_lock = None
            _pool_loop = None


async def _get_pool() -> asyncpg.Pool:
    """Lazy-init a per-event-loop connection pool for AlloyDB."""
    global _pool, _pool_lock, _pool_loop
    current_loop = asyncio.get_running_loop()

    with _state_lock:
        if _pool is None and _pool_lock is None and _pool_loop is None:
            _pools.pop(current_loop, None)
            _pool_locks.pop(current_loop, None)
        _cleanup_closed_loop_pools()

        existing = _pools.get(current_loop)
        if existing is not None and not existing.is_closing():
            return existing
        if _pool is not None and _pool_loop is None and not _pool.is_closing():
            return _pool

        lock = _pool_locks.get(current_loop)
        if lock is None:
            lock = asyncio.Lock()
            _pool_locks[current_loop] = lock
            if _pool_loop is None or _pool_loop is current_loop or _pool_loop.is_closed():
                _pool_lock = lock
                _pool_loop = current_loop

    async with lock:
        with _state_lock:
            existing = _pools.get(current_loop)
            if existing is not None and not existing.is_closing():
                return existing
            if _pool is not None and _pool_loop is None and not _pool.is_closing():
                return _pool

        schema_name = _validate_schema_name(os.environ.get("ALLOYDB_SCHEMA"), default="public")
        min_size = _parse_int_env("ALLOYDB_POOL_MIN", 2, min_value=1)
        max_size = _parse_int_env("ALLOYDB_POOL_MAX", 10, min_value=min_size)
        new_pool = await asyncpg.create_pool(
            _get_dsn(),
            min_size=min_size,
            max_size=max_size,
            server_settings={"search_path": f"{schema_name}, public"},
            command_timeout=10,
        )
        with _state_lock:
            _pools[current_loop] = new_pool
            _pool = new_pool
            _pool_lock = lock
            _pool_loop = current_loop
        logger.info("AlloyDB connection pool created (min=%d, max=%d)", min_size, max_size)
        return new_pool


def _decode_jsonb(value):
    """Decode a JSONB column into a Python object.

    asyncpg returns JSONB values as strings unless a codec is
    registered, so callers see the raw JSON text. Handles str/bytes/bytearray
    and logs a warning + returns None on corrupt JSON instead of crashing.
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "JSONB decode failed for row; treating as null: %s",
                str(exc)[:200],
            )
            return None
    return value


def _row_to_route(row: asyncpg.Record) -> PlannedRoute:
    """Convert an asyncpg row from planned_routes to a PlannedRoute."""
    raw_rd = row["route_data"]
    if raw_rd is None:
        raise ValueError("route_data must not be NULL in planned_routes")
    if isinstance(raw_rd, (str, bytes, bytearray)):
        try:
            route_data = json.loads(raw_rd)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Corrupt route_data JSONB: {exc}") from exc
    else:
        route_data = raw_rd
    if not isinstance(route_data, dict):
        raise ValueError("route_data JSONB must decode to a dict")

    decoded_eval = _decode_jsonb(row["eval_result"])
    if decoded_eval is not None and not isinstance(decoded_eval, dict):
        logger.warning(
            "Ignoring non-dict eval_result JSONB (%s) for route %s",
            type(decoded_eval).__name__,
            row["route_id"],
        )
        decoded_eval = None

    return PlannedRoute(
        route_id=row["route_id"],
        route_data=route_data,
        created_at=row["created_at"].replace(tzinfo=timezone.utc)
        if row["created_at"].tzinfo is None
        else row["created_at"],
        evaluation_score=row["eval_score"],
        evaluation_result=decoded_eval,
        # asyncpg.Record has no .get() — KeyError is raised on missing keys.
        # The SELECTs in this module always project owner_id, but be defensive
        # in case a test stubs out a row.
        owner_id=(row["owner_id"] if "owner_id" in row.keys() else None),
    )


async def _load_simulations(conn: asyncpg.Connection, route_id: str) -> list[SimulationRecord]:
    rows = await conn.fetch(
        "SELECT simulation_id, route_id, sim_result, simulated_at "
        "FROM simulation_records WHERE route_id = $1 ORDER BY simulated_at ASC",
        route_id,
    )
    records: list[SimulationRecord] = []
    for r in rows:
        decoded_sim = _decode_jsonb(r["sim_result"])
        if decoded_sim is not None and not isinstance(decoded_sim, dict):
            logger.warning(
                "Unexpected non-dict sim_result type %s; falling back to empty dict",
                type(decoded_sim).__name__,
            )
        sim_result: dict = decoded_sim if isinstance(decoded_sim, dict) else {}
        records.append(
            SimulationRecord(
                simulation_id=r["simulation_id"],
                route_id=r["route_id"],
                simulation_result=sim_result,
                simulated_at=r["simulated_at"].replace(tzinfo=timezone.utc)
                if r["simulated_at"].tzinfo is None
                else r["simulated_at"],
            )
        )
    return records


class AlloyDBRouteStore:
    """AlloyDB-backed store for planned routes and simulation records."""

    # -----------------------------------------------------------------------
    # Public API (mirrors RouteMemoryStore)
    # -----------------------------------------------------------------------

    async def store_route(
        self,
        route_data: dict,
        evaluation_score: float | None = None,
        evaluation_result: dict | None = None,
        owner_id: str | None = None,
    ) -> str:
        """Persist a new planned route and return its UUID.

        ``owner_id`` is stored as-is. ``None`` means "unowned / legacy",
        which keeps the single-tenant call path byte-identical.
        """
        route_id = str(uuid.uuid4())
        safe_score = _sanitize_score(evaluation_score)
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO planned_routes (route_id, route_data, created_at, eval_score, eval_result, owner_id)
                VALUES ($1, $2::jsonb, now(), $3, $4::jsonb, $5)
                """,
                route_id,
                _json_dumps(route_data),
                safe_score,
                _json_dumps(evaluation_result) if evaluation_result is not None else None,
                owner_id,
            )
        return route_id

    async def get_route(
        self,
        route_id: str,
        owner_id: str | None = None,
    ) -> PlannedRoute | None:
        """Retrieve a route by ID, or None if not found / owner mismatch.

        When ``owner_id`` is set, the row must have a matching
        ``owner_id`` to be returned. Unowned legacy rows are NOT
        surfaced under a scoped read — callers that opt into tenancy
        get strict isolation.
        """
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                row = await conn.fetchrow(
                    "SELECT route_id, route_data, created_at, eval_score, eval_result, owner_id "
                    "FROM planned_routes WHERE route_id = $1",
                    route_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT route_id, route_data, created_at, eval_score, eval_result, owner_id "
                    "FROM planned_routes WHERE route_id = $1 AND owner_id = $2",
                    route_id,
                    owner_id,
                )
            if row is None:
                return None
            route = _row_to_route(row)
            route.simulations = await _load_simulations(conn, route_id)
            return route

    async def record_simulation(
        self,
        route_id: str,
        simulation_result: dict,
        owner_id: str | None = None,
    ) -> str | None:
        """Append a simulation record to route_id. Returns sim UUID or None.

        When ``owner_id`` is set, the parent route must be owned by the
        caller; otherwise this returns ``None`` instead of writing a
        record under another user's route.
        """
        pool = await _get_pool()
        async with pool.acquire() as conn:
            # Tenancy gate: verify the caller owns the parent row before
            # writing. We can't rely on the FK alone because every user
            # could see every route under the old single-tenant contract.
            if owner_id is not None:
                owned = await conn.fetchval(
                    "SELECT 1 FROM planned_routes WHERE route_id = $1 AND owner_id = $2",
                    route_id,
                    owner_id,
                )
                if not owned:
                    return None
            # L20 fix: Attempt INSERT directly and catch FK violation,
            # eliminating the TOCTOU race of SELECT-then-INSERT.
            sim_id = str(uuid.uuid4())
            try:
                await conn.execute(
                    """
                    INSERT INTO simulation_records (simulation_id, route_id, sim_result, simulated_at)
                    VALUES ($1, $2, $3::jsonb, now())
                    """,
                    sim_id,
                    route_id,
                    _json_dumps(simulation_result),
                )
                return sim_id
            except asyncpg.ForeignKeyViolationError:
                return None
            except Exception as e:
                # Fallback for driver/stub exceptions representing a foreign-key violation
                if "foreign key" in str(e).lower() or "ForeignKeyViolation" in type(e).__name__:
                    return None
                raise

    async def recall_routes(
        self,
        count: int = 10,
        sort_by: str = "recent",
        owner_id: str | None = None,
    ) -> list[PlannedRoute]:
        """Return up to count routes sorted by recent or best_score.

        When ``owner_id`` is set, only rows owned by that user are
        returned; unowned legacy rows are excluded.
        """
        safe_count = _clamp_non_negative_limit(count, default=10, max_limit=500)
        if safe_count == 0:
            return []
        order = "created_at DESC" if sort_by != "best_score" else "eval_score DESC NULLS LAST"
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                rows = await conn.fetch(
                    f"SELECT route_id, route_data, created_at, eval_score, eval_result, owner_id "
                    f"FROM planned_routes ORDER BY {order} LIMIT $1",
                    safe_count,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT route_id, route_data, created_at, eval_score, eval_result, owner_id "
                    f"FROM planned_routes WHERE owner_id = $1 ORDER BY {order} LIMIT $2",
                    owner_id,
                    safe_count,
                )
            return [_row_to_route(r) for r in rows]

    async def get_best_route(
        self,
        owner_id: str | None = None,
    ) -> PlannedRoute | None:
        """Return the route with the highest evaluation score.

        When ``owner_id`` is set, only the caller's routes are considered.
        """
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                row = await conn.fetchrow(
                    "SELECT route_id, route_data, created_at, eval_score, eval_result, owner_id "
                    "FROM planned_routes WHERE eval_score IS NOT NULL "
                    "ORDER BY eval_score DESC LIMIT 1"
                )
            else:
                row = await conn.fetchrow(
                    "SELECT route_id, route_data, created_at, eval_score, eval_result, owner_id "
                    "FROM planned_routes WHERE eval_score IS NOT NULL AND owner_id = $1 "
                    "ORDER BY eval_score DESC LIMIT 1",
                    owner_id,
                )
            if row is None:
                return None
            route = _row_to_route(row)
            route.simulations = await _load_simulations(conn, route.route_id)
            return route

    async def store_route_idempotent(
        self,
        route_id: str,
        route_data: dict,
        created_at: datetime,
        evaluation_score: float | None = None,
        evaluation_result: dict | None = None,
        owner_id: str | None = None,
    ) -> None:
        """Insert a route preserving its original route_id (used by seed loader)."""
        safe_score = _sanitize_score(evaluation_score)
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO planned_routes (route_id, route_data, created_at, eval_score, eval_result, owner_id)
                VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6)
                ON CONFLICT (route_id) DO NOTHING
                """,
                route_id,
                _json_dumps(route_data),
                created_at,
                safe_score,
                _json_dumps(evaluation_result) if evaluation_result is not None else None,
                owner_id,
            )

    async def close(self) -> None:
        """Drain and close the connection pool for the current event loop."""
        global _pool, _pool_lock, _pool_loop
        current_loop = asyncio.get_running_loop()
        with _state_lock:
            _cleanup_closed_loop_pools()
            pool = _pools.pop(current_loop, None)
            _pool_locks.pop(current_loop, None)
            if pool is None and _pool is not None and _pool_loop in (None, current_loop):
                pool = _pool
            if _pool is pool or _pool_loop in (None, current_loop):
                _pool = None
                _pool_lock = None
                _pool_loop = None
        if pool is not None:
            await pool.close()
            logger.info("AlloyDB connection pool closed.")
