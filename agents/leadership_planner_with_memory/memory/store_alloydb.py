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

"""AlloyDB-backed leadership scenario memory store.

Replaces the in-memory dict store with a persistent AlloyDB backend
using asyncpg. Implements the same public API so tools.py requires no changes
beyond swapping the singleton.

Mirrors agents/planner_with_memory/memory/store_alloydb.py but with
leadership-domain table names (leadership_scenarios, leadership_simulation_records).
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

from agents.leadership_planner_with_memory.memory.schemas import PlannedScenario, SimulationRecord

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
            # Policy: serve stale secret on transient SM outage so
            # running queries don't hard-fail.  Log at ERROR so
            # monitoring can alert on sustained staleness.
            logger.error("Secret Manager refresh failed; serving stale cached password.", exc_info=True)
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
            "ALLOYDB_HOST environment variable is required for AlloyDBScenarioStore. "
            "Set it to the private IP of the AlloyDB instance."
        )
    database = urllib.parse.quote(os.environ.get("ALLOYDB_DATABASE", "postgres"), safe="")
    user = urllib.parse.quote(os.environ.get("ALLOYDB_USER", "postgres"), safe="")
    password = urllib.parse.quote(_resolve_password(), safe="")
    port = _parse_int_env("ALLOYDB_PORT", 5432, min_value=1, max_value=65535)
    host_str = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"postgresql://{user}:{password}@{host_str}:{port}/{database}"


# _get_conn() was removed — all queries now use the connection pool
# returned by _get_pool() below.


# ---------------------------------------------------------------------------
# Connection pool (replaces per-query asyncpg.connect for stability)
# ---------------------------------------------------------------------------
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
    """Lazy-init a per-event-loop connection pool for AlloyDB.

    Mirrors agents/planner_with_memory/memory/store_alloydb.py.
    Using a pool avoids the TCP connect/TLS handshake overhead per query
    that previously caused connection storms and AlloyDB proxy crashes.
    """
    global _pool, _pool_lock, _pool_loop
    current_loop = asyncio.get_running_loop()

    with _state_lock:
        # Honor explicit test resets of module globals (`_pool = None; _pool_lock = None; _pool_loop = None`)
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


def _row_to_scenario(row: asyncpg.Record) -> PlannedScenario:
    """Convert an asyncpg row from leadership_scenarios to a PlannedScenario.

    Checks ``"persona" in row.keys()`` and ``"owner_id" in row.keys()``
    rather than calling ``row.get(...)`` so ``Record`` stubs lacking
    ``.get()`` work reliably alongside ``asyncpg.Record``.
    """
    raw_sd = row["scenario_data"]
    if raw_sd is None:
        raise ValueError("scenario_data must not be NULL in leadership_scenarios")
    if isinstance(raw_sd, (str, bytes, bytearray)):
        try:
            scenario_data = json.loads(raw_sd)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Corrupt scenario_data JSONB: {exc}") from exc
    else:
        scenario_data = raw_sd
    if not isinstance(scenario_data, dict):
        raise ValueError("scenario_data JSONB must decode to a dict")

    decoded_eval = _decode_jsonb(row["eval_result"])
    if decoded_eval is not None and not isinstance(decoded_eval, dict):
        logger.warning(
            "Ignoring non-dict eval_result JSONB (%s) for scenario %s",
            type(decoded_eval).__name__,
            row["scenario_id"],
        )
        decoded_eval = None

    row_keys = row.keys()
    return PlannedScenario(
        scenario_id=row["scenario_id"],
        scenario_data=scenario_data,
        created_at=row["created_at"].replace(tzinfo=timezone.utc)
        if row["created_at"].tzinfo is None
        else row["created_at"],
        evaluation_score=row["eval_score"],
        evaluation_result=decoded_eval,
        persona=(row["persona"] if "persona" in row_keys else None),
        owner_id=(row["owner_id"] if "owner_id" in row_keys else None),
    )


async def _load_simulations(conn: asyncpg.Connection, scenario_id: str) -> list[SimulationRecord]:
    rows = await conn.fetch(
        "SELECT simulation_id, scenario_id, sim_result, simulated_at "
        "FROM leadership_simulation_records WHERE scenario_id = $1 ORDER BY simulated_at ASC",
        scenario_id,
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
                scenario_id=r["scenario_id"],
                simulation_result=sim_result,
                simulated_at=r["simulated_at"].replace(tzinfo=timezone.utc)
                if r["simulated_at"].tzinfo is None
                else r["simulated_at"],
            )
        )
    return records


class AlloyDBScenarioStore:
    """AlloyDB-backed store for planned leadership scenarios and simulation records."""

    # -----------------------------------------------------------------------
    # Public API (mirrors AlloyDBRouteStore)
    # -----------------------------------------------------------------------

    async def store_scenario(
        self,
        scenario_data: dict,
        evaluation_score: float | None = None,
        evaluation_result: dict | None = None,
        persona: str | None = None,
        owner_id: str | None = None,
    ) -> str:
        """Persist a new planned scenario and return its UUID.

        ``owner_id`` is stored as-is. ``None`` means "unowned / legacy",
        which keeps the single-tenant call path byte-identical.
        """
        scenario_id = str(uuid.uuid4())
        safe_score = _sanitize_score(evaluation_score)
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO leadership_scenarios (scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id)
                VALUES ($1, $2::jsonb, now(), $3, $4::jsonb, $5, $6)
                """,
                scenario_id,
                _json_dumps(scenario_data),
                safe_score,
                _json_dumps(evaluation_result) if evaluation_result is not None else None,
                persona,
                owner_id,
            )
        return scenario_id

    async def get_scenario(
        self,
        scenario_id: str,
        owner_id: str | None = None,
    ) -> PlannedScenario | None:
        """Retrieve a scenario by ID, or None if not found / owner mismatch.

        When ``owner_id`` is set, the row must have a matching
        ``owner_id`` to be returned. Unowned legacy rows are NOT
        surfaced under a scoped read — callers that opt into tenancy
        get strict isolation.
        """
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                row = await conn.fetchrow(
                    "SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    "FROM leadership_scenarios WHERE scenario_id = $1",
                    scenario_id,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    "FROM leadership_scenarios WHERE scenario_id = $1 AND owner_id = $2",
                    scenario_id,
                    owner_id,
                )
            if row is None:
                return None
            scenario = _row_to_scenario(row)
            scenario.simulations = await _load_simulations(conn, scenario_id)
            return scenario

    async def record_simulation(
        self,
        scenario_id: str,
        simulation_result: dict,
        owner_id: str | None = None,
    ) -> str | None:
        """Append a simulation record to scenario_id. Returns sim UUID or None
        if no scenario exists OR the caller does not own it."""
        pool = await _get_pool()
        async with pool.acquire() as conn:
            # Tenancy gate: verify the caller owns the parent row before
            # writing.
            if owner_id is not None:
                owned = await conn.fetchval(
                    "SELECT 1 FROM leadership_scenarios WHERE scenario_id = $1 AND owner_id = $2",
                    scenario_id,
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
                    INSERT INTO leadership_simulation_records (simulation_id, scenario_id, sim_result, simulated_at)
                    VALUES ($1, $2, $3::jsonb, now())
                    """,
                    sim_id,
                    scenario_id,
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

    async def recall_scenarios(
        self,
        count: int = 10,
        sort_by: str = "recent",
        owner_id: str | None = None,
    ) -> list[PlannedScenario]:
        """Return up to count scenarios sorted by recent or best_score.

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
                    f"SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    f"FROM leadership_scenarios ORDER BY {order} LIMIT $1",
                    safe_count,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    f"FROM leadership_scenarios WHERE owner_id = $1 ORDER BY {order} LIMIT $2",
                    owner_id,
                    safe_count,
                )
            return [_row_to_scenario(r) for r in rows]

    async def get_best_scenario(
        self,
        owner_id: str | None = None,
    ) -> PlannedScenario | None:
        """Return the scenario with the highest evaluation score.

        When ``owner_id`` is set, only the caller's scenarios are
        considered.
        """
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                row = await conn.fetchrow(
                    "SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    "FROM leadership_scenarios WHERE eval_score IS NOT NULL "
                    "ORDER BY eval_score DESC LIMIT 1"
                )
            else:
                row = await conn.fetchrow(
                    "SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    "FROM leadership_scenarios WHERE eval_score IS NOT NULL AND owner_id = $1 "
                    "ORDER BY eval_score DESC LIMIT 1",
                    owner_id,
                )
            if row is None:
                return None
            scenario = _row_to_scenario(row)
            scenario.simulations = await _load_simulations(conn, scenario.scenario_id)
            return scenario

    async def store_scenario_idempotent(
        self,
        scenario_id: str,
        scenario_data: dict,
        created_at: datetime,
        evaluation_score: float | None = None,
        evaluation_result: dict | None = None,
        owner_id: str | None = None,
    ) -> None:
        """Insert a scenario preserving its original scenario_id (used by seed loader)."""
        safe_score = _sanitize_score(evaluation_score)
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO leadership_scenarios (scenario_id, scenario_data, created_at, eval_score, eval_result, owner_id)
                VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6)
                ON CONFLICT (scenario_id) DO NOTHING
                """,
                scenario_id,
                _json_dumps(scenario_data),
                created_at,
                safe_score,
                _json_dumps(evaluation_result) if evaluation_result is not None else None,
                owner_id,
            )

    async def update_evaluation(
        self,
        scenario_id: str,
        evaluation_score: float | None,
        evaluation_result: dict | None,
        owner_id: str | None = None,
    ) -> bool:
        """Backfill evaluation_score / eval_result on an existing scenario.

        COALESCE semantics: only writes fields whose current DB value is
        NULL. An existing non-null score is never overwritten, preserving
        the audit trail of the original evaluation. Returns ``True`` if a
        row was updated (i.e., at least one field was eligible for write
        AND the caller owns the scenario when ``owner_id`` is set).
        """
        safe_score = _sanitize_score(evaluation_score)
        # No-op if caller has nothing to offer.
        if safe_score is None and evaluation_result is None:
            return False
        serialized_result = _json_dumps(evaluation_result) if evaluation_result is not None else None
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                row = await conn.fetchrow(
                    """
                    UPDATE leadership_scenarios
                       SET eval_score  = COALESCE(eval_score,  $2),
                           eval_result = COALESCE(eval_result, $3::jsonb)
                     WHERE scenario_id = $1
                       AND (eval_score IS NULL OR eval_result IS NULL)
                     RETURNING scenario_id
                    """,
                    scenario_id,
                    safe_score,
                    serialized_result,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE leadership_scenarios
                       SET eval_score  = COALESCE(eval_score,  $2),
                           eval_result = COALESCE(eval_result, $3::jsonb)
                     WHERE scenario_id = $1
                       AND owner_id = $4
                       AND (eval_score IS NULL OR eval_result IS NULL)
                     RETURNING scenario_id
                    """,
                    scenario_id,
                    safe_score,
                    serialized_result,
                    owner_id,
                )
            return row is not None

    # ------------------------------------------------------------------
    # Stakeholder-artifact persistence (Artifacts 1-4)
    # ------------------------------------------------------------------

    _ARTIFACT_KEYS: tuple[str, ...] = (
        "stakeholder_graph",
        "stakeholder_personas",
        "stakeholder_reactions",
        "simulation_timeline",
        "simulation_report",
        "stakeholder_interview",
    )

    async def store_artifacts(
        self,
        scenario_id: str,
        artifacts: dict,
        owner_id: str | None = None,
    ) -> bool:
        """Upsert the stakeholder-artifact bundle for ``scenario_id``.

        Only keys present in ``artifacts`` with a non-None value are
        written -- ``COALESCE`` preserves any previously stored values
        for keys the caller omits, so a partial flush never erases
        earlier artifacts. Returns ``True`` iff the upsert touched a row
        (i.e. the scenario exists and -- when ``owner_id`` is given --
        is owned by the caller).
        """
        encoded = {
            key: (_json_dumps(artifacts[key]) if artifacts.get(key) is not None else None)
            for key in self._ARTIFACT_KEYS
        }
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO leadership_stakeholder_artifacts (
                        scenario_id,
                        stakeholder_graph,
                        stakeholder_personas,
                        stakeholder_reactions,
                        simulation_timeline,
                        simulation_report,
                        stakeholder_interview,
                        owner_id,
                        created_at,
                        updated_at
                    )
                    SELECT s.scenario_id, $2::jsonb, $3::jsonb, $4::jsonb,
                           $5::jsonb, $6::jsonb, $7::jsonb,
                           s.owner_id, now(), now()
                      FROM leadership_scenarios s
                     WHERE s.scenario_id = $1
                    ON CONFLICT (scenario_id) DO UPDATE
                       SET stakeholder_graph     = COALESCE(EXCLUDED.stakeholder_graph,
                                                             leadership_stakeholder_artifacts.stakeholder_graph),
                           stakeholder_personas  = COALESCE(EXCLUDED.stakeholder_personas,
                                                             leadership_stakeholder_artifacts.stakeholder_personas),
                           stakeholder_reactions = COALESCE(EXCLUDED.stakeholder_reactions,
                                                             leadership_stakeholder_artifacts.stakeholder_reactions),
                           simulation_timeline   = COALESCE(EXCLUDED.simulation_timeline,
                                                             leadership_stakeholder_artifacts.simulation_timeline),
                           simulation_report     = COALESCE(EXCLUDED.simulation_report,
                                                             leadership_stakeholder_artifacts.simulation_report),
                           stakeholder_interview = COALESCE(EXCLUDED.stakeholder_interview,
                                                             leadership_stakeholder_artifacts.stakeholder_interview),
                           updated_at            = now()
                     RETURNING scenario_id
                    """,
                    scenario_id,
                    encoded["stakeholder_graph"],
                    encoded["stakeholder_personas"],
                    encoded["stakeholder_reactions"],
                    encoded["simulation_timeline"],
                    encoded["simulation_report"],
                    encoded["stakeholder_interview"],
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO leadership_stakeholder_artifacts (
                        scenario_id,
                        stakeholder_graph,
                        stakeholder_personas,
                        stakeholder_reactions,
                        simulation_timeline,
                        simulation_report,
                        stakeholder_interview,
                        owner_id,
                        created_at,
                        updated_at
                    )
                    SELECT s.scenario_id, $2::jsonb, $3::jsonb, $4::jsonb,
                           $5::jsonb, $6::jsonb, $7::jsonb,
                           s.owner_id, now(), now()
                      FROM leadership_scenarios s
                     WHERE s.scenario_id = $1
                       AND s.owner_id    = $8
                    ON CONFLICT (scenario_id) DO UPDATE
                       SET stakeholder_graph     = COALESCE(EXCLUDED.stakeholder_graph,
                                                             leadership_stakeholder_artifacts.stakeholder_graph),
                           stakeholder_personas  = COALESCE(EXCLUDED.stakeholder_personas,
                                                             leadership_stakeholder_artifacts.stakeholder_personas),
                           stakeholder_reactions = COALESCE(EXCLUDED.stakeholder_reactions,
                                                             leadership_stakeholder_artifacts.stakeholder_reactions),
                           simulation_timeline   = COALESCE(EXCLUDED.simulation_timeline,
                                                             leadership_stakeholder_artifacts.simulation_timeline),
                           simulation_report     = COALESCE(EXCLUDED.simulation_report,
                                                             leadership_stakeholder_artifacts.simulation_report),
                           stakeholder_interview = COALESCE(EXCLUDED.stakeholder_interview,
                                                             leadership_stakeholder_artifacts.stakeholder_interview),
                           updated_at            = now()
                     RETURNING scenario_id
                    """,
                    scenario_id,
                    encoded["stakeholder_graph"],
                    encoded["stakeholder_personas"],
                    encoded["stakeholder_reactions"],
                    encoded["simulation_timeline"],
                    encoded["simulation_report"],
                    encoded["stakeholder_interview"],
                    owner_id,
                )
            return row is not None

    async def get_artifacts(
        self,
        scenario_id: str,
        owner_id: str | None = None,
    ) -> dict | None:
        """Return the stakeholder-artifact bundle for ``scenario_id``.

        Returns ``None`` if no row exists or the caller does not own it.
        Only keys with non-NULL values are included in the returned
        dict -- callers should treat missing keys as "not produced".
        """
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                row = await conn.fetchrow(
                    """
                    SELECT stakeholder_graph,
                           stakeholder_personas,
                           stakeholder_reactions,
                           simulation_timeline,
                           simulation_report,
                           stakeholder_interview
                      FROM leadership_stakeholder_artifacts
                     WHERE scenario_id = $1
                    """,
                    scenario_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    SELECT stakeholder_graph,
                           stakeholder_personas,
                           stakeholder_reactions,
                           simulation_timeline,
                           simulation_report,
                           stakeholder_interview
                      FROM leadership_stakeholder_artifacts
                     WHERE scenario_id = $1
                       AND owner_id    = $2
                    """,
                    scenario_id,
                    owner_id,
                )
            if row is None:
                return None
            result: dict = {}
            for key in self._ARTIFACT_KEYS:
                decoded = _decode_jsonb(row[key])
                if decoded is None:
                    continue
                result[key] = decoded
            return result if result else None

    # ------------------------------------------------------------------
    # Scenario bundles (joined scenarios + artifacts)
    # ------------------------------------------------------------------

    async def list_scenario_bundles(
        self,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return scenario metadata joined with each scenario's stakeholder artifacts.

        Ordered newest-first,
        capped at ``limit`` (default 100 -- enough to compound learning
        across a very active user without bloating the planner prompt).
        Uses a ``LEFT JOIN`` on ``leadership_stakeholder_artifacts`` so
        scenarios without stored artifacts still contribute to the sample
        size. Same owner_id semantics as :meth:`recall_scenarios`:
        ``None`` returns every scenario; a non-``None`` value returns only
        the strictly matching rows.

        **Return-shape contract** (shared with :class:`ScenarioMemoryStore`):
        each dict has keys ``scenario_id``, ``eval_score``, ``created_at``,
        ``stakeholder_graph``, ``stakeholder_personas``,
        ``stakeholder_reactions``, ``simulation_timeline``,
        ``simulation_report``, and ``stakeholder_interview``. Artifact
        values are ``dict | list | None``; AlloyDB decodes via
        ``_decode_jsonb``, in-memory returns raw Python objects.
        """
        safe_limit = _clamp_non_negative_limit(limit, default=100, max_limit=500)
        if safe_limit == 0:
            return []
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                rows = await conn.fetch(
                    """
                    SELECT ls.scenario_id,
                           ls.eval_score,
                           ls.created_at,
                           a.stakeholder_graph,
                           a.stakeholder_personas,
                           a.stakeholder_reactions,
                           a.simulation_timeline,
                           a.simulation_report,
                           a.stakeholder_interview
                      FROM leadership_scenarios ls
                      LEFT JOIN leadership_stakeholder_artifacts a
                             ON ls.scenario_id = a.scenario_id
                     ORDER BY ls.created_at DESC
                     LIMIT $1
                    """,
                    safe_limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT ls.scenario_id,
                           ls.eval_score,
                           ls.created_at,
                           a.stakeholder_graph,
                           a.stakeholder_personas,
                           a.stakeholder_reactions,
                           a.simulation_timeline,
                           a.simulation_report,
                           a.stakeholder_interview
                      FROM leadership_scenarios ls
                      LEFT JOIN leadership_stakeholder_artifacts a
                             ON ls.scenario_id = a.scenario_id
                            AND a.owner_id     = $1
                     WHERE ls.owner_id = $1
                     ORDER BY ls.created_at DESC
                     LIMIT $2
                    """,
                    owner_id,
                    safe_limit,
                )
            return [
                {
                    "scenario_id": row["scenario_id"],
                    "eval_score": row["eval_score"],
                    "created_at": (
                        row["created_at"].replace(tzinfo=timezone.utc)
                        if row["created_at"] is not None and row["created_at"].tzinfo is None
                        else row["created_at"]
                    ),
                    "stakeholder_graph": _decode_jsonb(row["stakeholder_graph"]),
                    "stakeholder_personas": _decode_jsonb(row["stakeholder_personas"]),
                    "stakeholder_reactions": _decode_jsonb(row["stakeholder_reactions"]),
                    "simulation_timeline": _decode_jsonb(row["simulation_timeline"]),
                    "simulation_report": _decode_jsonb(row["simulation_report"]),
                    "stakeholder_interview": _decode_jsonb(row["stakeholder_interview"]),
                }
                for row in rows
            ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Drain and close the connection pool.

        Call from the ADK agent's shutdown hook so local-dev restarts
        don't leak TCP connections to the AlloyDB proxy.
        """
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


def _decode_jsonb(value):
    """Decode a JSONB column into a Python object.

    asyncpg returns JSONB values as strings unless a codec is
    registered, so callers see the raw JSON text. This helper mirrors
    the defensive parsing already used in ``get_artifacts`` so
    downstream aggregation code always sees dicts/lists.
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
