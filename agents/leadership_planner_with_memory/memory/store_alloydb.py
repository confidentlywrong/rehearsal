# Copyright 2026 The Confidently Wrong Authors
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
import os
import time
import uuid
from datetime import datetime, timezone

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

    secret_name = f"projects/{_resolve_sm_project()}/secrets/{_SM_SECRET}/versions/latest"
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


def _get_dsn() -> str:
    """Build a DSN from environment variables."""
    host = os.environ.get("ALLOYDB_HOST")
    if not host:
        raise ValueError(
            "ALLOYDB_HOST environment variable is required for AlloyDBScenarioStore. "
            "Set it to the private IP of the AlloyDB instance."
        )
    database = os.environ.get("ALLOYDB_DATABASE", "postgres")
    user = os.environ.get("ALLOYDB_USER", "postgres")
    password = _resolve_password()
    port = int(os.environ.get("ALLOYDB_PORT", "5432"))
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


# _get_conn() was removed — all queries now use the connection pool
# returned by _get_pool() below.


# ---------------------------------------------------------------------------
# Connection pool (replaces per-query asyncpg.connect for stability)
# ---------------------------------------------------------------------------
_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> asyncpg.Pool:
    """Lazy-init a connection pool for AlloyDB.

    Mirrors agents/planner_with_memory/memory/store_alloydb.py.
    Using a pool avoids the TCP connect/TLS handshake overhead per query
    that previously caused connection storms and AlloyDB proxy crashes.
    """
    global _pool
    if _pool is not None and not _pool.is_closing():
        return _pool
    async with _pool_lock:
        # Double-check after acquiring lock
        if _pool is not None and not _pool.is_closing():
            return _pool
        schema_name = os.environ.get("ALLOYDB_SCHEMA", "public")
        min_size = int(os.environ.get("ALLOYDB_POOL_MIN", "2"))
        max_size = int(os.environ.get("ALLOYDB_POOL_MAX", "10"))
        _pool = await asyncpg.create_pool(
            _get_dsn(),
            min_size=min_size,
            max_size=max_size,
            server_settings={"search_path": f"{schema_name}, public"},
            command_timeout=10,
        )
        logger.info("AlloyDB connection pool created (min=%d, max=%d)", min_size, max_size)
        return _pool


def _row_to_scenario(row: asyncpg.Record) -> PlannedScenario:
    """Convert an asyncpg row from leadership_scenarios to a PlannedScenario.

    Uses ``.get()`` for ``persona`` and ``owner_id`` because these
    columns were added in later migrations and some SELECT statements
    don't project them.
    """
    return PlannedScenario(
        scenario_id=row["scenario_id"],
        scenario_data=json.loads(row["scenario_data"])
        if isinstance(row["scenario_data"], str)
        else row["scenario_data"],
        created_at=row["created_at"].replace(tzinfo=timezone.utc)
        if row["created_at"].tzinfo is None
        else row["created_at"],
        evaluation_score=row["eval_score"],
        evaluation_result=json.loads(row["eval_result"]) if isinstance(row["eval_result"], str) else row["eval_result"],
        persona=row.get("persona"),
        owner_id=row.get("owner_id"),
    )


async def _load_simulations(conn: asyncpg.Connection, scenario_id: str) -> list[SimulationRecord]:
    rows = await conn.fetch(
        "SELECT simulation_id, scenario_id, sim_result, simulated_at "
        "FROM leadership_simulation_records WHERE scenario_id = $1 ORDER BY simulated_at ASC",
        scenario_id,
    )
    return [
        SimulationRecord(
            simulation_id=r["simulation_id"],
            scenario_id=r["scenario_id"],
            simulation_result=json.loads(r["sim_result"]) if isinstance(r["sim_result"], str) else r["sim_result"],
            simulated_at=r["simulated_at"].replace(tzinfo=timezone.utc)
            if r["simulated_at"].tzinfo is None
            else r["simulated_at"],
        )
        for r in rows
    ]


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
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO leadership_scenarios (scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id)
                VALUES ($1, $2::jsonb, now(), $3, $4::jsonb, $5, $6)
                """,
                scenario_id,
                json.dumps(scenario_data),
                evaluation_score,
                json.dumps(evaluation_result) if evaluation_result is not None else None,
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
                    json.dumps(simulation_result),
                )
                return sim_id
            except Exception as e:
                # asyncpg raises ForeignKeyViolationError for missing scenario_id
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
        order = "created_at DESC" if sort_by != "best_score" else "eval_score DESC NULLS LAST"
        pool = await _get_pool()
        async with pool.acquire() as conn:
            if owner_id is None:
                rows = await conn.fetch(
                    f"SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    f"FROM leadership_scenarios ORDER BY {order} LIMIT $1",
                    count,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT scenario_id, scenario_data, created_at, eval_score, eval_result, persona, owner_id "
                    f"FROM leadership_scenarios WHERE owner_id = $1 ORDER BY {order} LIMIT $2",
                    owner_id,
                    count,
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
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO leadership_scenarios (scenario_id, scenario_data, created_at, eval_score, eval_result, owner_id)
                VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6)
                ON CONFLICT (scenario_id) DO NOTHING
                """,
                scenario_id,
                json.dumps(scenario_data),
                created_at,
                evaluation_score,
                json.dumps(evaluation_result) if evaluation_result is not None else None,
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
        # No-op if caller has nothing to offer.
        if evaluation_score is None and evaluation_result is None:
            return False
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
                    evaluation_score,
                    json.dumps(evaluation_result) if evaluation_result is not None else None,
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
                    evaluation_score,
                    json.dumps(evaluation_result) if evaluation_result is not None else None,
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
            key: (json.dumps(artifacts[key]) if artifacts.get(key) is not None else None)
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
                value = row[key]
                if value is None:
                    continue
                # asyncpg returns JSONB as str unless a codec is registered;
                # decode defensively so callers see Python objects either way.
                if isinstance(value, (str, bytes, bytearray)):
                    try:
                        result[key] = json.loads(value)
                    except (TypeError, ValueError):
                        result[key] = value
                else:
                    result[key] = value
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
        ``stakeholder_reactions``. Artifact values are ``dict | None``;
        AlloyDB decodes via ``_decode_jsonb``, in-memory returns raw dicts.
        """
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
                    limit,
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
                    limit,
                )
            return [
                {
                    "scenario_id": row["scenario_id"],
                    "eval_score": row["eval_score"],
                    "created_at": row["created_at"],
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
        global _pool
        if _pool is not None:
            await _pool.close()
            _pool = None
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
