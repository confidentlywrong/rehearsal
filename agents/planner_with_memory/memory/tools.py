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

"""ADK FunctionTool wrappers for route memory operations.

Marathon planner intentionally retains the pgvector auto-summary path
(``_auto_store_summary``) — Phase 2D scope was leadership-only. See
``leadership_planner_with_memory/memory/tools.py`` for the deleted
counterpart.

Each function accepts JSON strings from the LLM, delegates to the
AlloyDBRouteStore, and returns a structured dict for A2A compliance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import math
import os

logger = logging.getLogger(__name__)

from google.adk.tools.tool_context import ToolContext  # noqa: E402

from agents.planner_with_memory.memory import embeddings as _embeddings  # noqa: E402
from agents.planner_with_memory.memory.schemas import PlannedRoute  # noqa: E402
from agents.planner_with_memory.memory.store_alloydb import (  # noqa: E402
    AlloyDBRouteStore,
    _decode_jsonb,
    _get_dsn,
    _json_dumps,
    _validate_schema_name,
)
from agents.utils import simulation_registry  # noqa: E402
from agents.utils import tenant_keys  # noqa: E402
from agents.utils.local_ai import is_local_gemma_active  # noqa: E402

# Module-level singleton, shared across all tool invocations within the same
# process. Tests may replace _store directly for isolation.
_store = AlloyDBRouteStore()


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _route_to_dict(route: PlannedRoute) -> dict:
    """Convert a PlannedRoute to a JSON-serialisable dict."""
    return {
        "route_id": route.route_id,
        "route_data": route.route_data,
        "created_at": route.created_at.isoformat(),
        "evaluation_score": route.evaluation_score,
        "evaluation_result": route.evaluation_result,
        "simulations": [
            {
                "simulation_id": sim.simulation_id,
                "route_id": sim.route_id,
                "simulation_result": sim.simulation_result,
                "simulated_at": sim.simulated_at.isoformat(),
            }
            for sim in route.simulations
        ],
    }


# EMBEDDING_BACKEND picks who computes embeddings:
#   - "alloydb_ai" (default): AlloyDB's ai.embedding() extension does it in SQL.
#   - "vertex_ai": compute client-side via google-genai and pass as $N::vector.
#     Required for OSS Cloud SQL / local Postgres (no ai.embedding extension).
# Auto-derived to "vertex_ai" when USE_ALLOYDB=false.
def _use_alloydb_flag() -> str:
    """Return the normalized USE_ALLOYDB env var string (defaults to 'true')."""
    return os.environ.get("USE_ALLOYDB", "true").strip().lower()


def _clamp_non_negative_limit(raw: object, default: int, max_limit: int) -> int:
    """Safely coerce and clamp a limit parameter to [0, max_limit].

    Falls back to ``default`` on ``None``, ``bool``, non-numeric strings,
    and non-finite floats (NaN/Inf) instead of raising ValueError/OverflowError.
    """
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
        except (ValueError, OverflowError):
            try:
                fv = float(s)
            except (TypeError, ValueError, OverflowError):
                return default
            if not math.isfinite(fv):
                return default
            if fv <= 0:
                return 0
            if fv >= max_limit:
                return max_limit
            val = int(fv)
    else:
        return default
    if val <= 0:
        return 0
    return min(val, max_limit)


def _resolve_embedding_backend() -> str:
    if is_local_gemma_active():
        return "vertex_ai"
    backend = os.environ.get("EMBEDDING_BACKEND", "").strip().lower()
    if backend == "vertexai":
        return "vertex_ai"
    if backend:
        if backend not in ("alloydb_ai", "vertex_ai"):
            raise ValueError(
                f"Invalid EMBEDDING_BACKEND: {backend!r}. Expected 'alloydb_ai' or 'vertex_ai'."
            )
        return backend
    return "vertex_ai" if _use_alloydb_flag() == "false" else "alloydb_ai"


def _to_pgvector(values: list[float]) -> str:
    """Format a float list as pgvector text literal (``[v1,v2,...]``).

    asyncpg's default list encoder produces PostgreSQL array syntax
    (``{v1,v2,...}``), which the ``::vector`` cast rejects. pgvector wants
    bracket notation, so we hand-encode rather than rely on a codec
    registration (callers come from short-lived connections where
    ``register_vector(conn)`` would add per-call overhead).
    """
    if not values:
        raise ValueError("Embedding vector must not be empty.")
    clean: list[str] = []
    for v in values:
        if isinstance(v, bool):
            raise ValueError("Embedding vector values must be finite numbers, got bool.")
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Embedding vector contains invalid value: {v!r}") from exc
        if not math.isfinite(f):
            raise ValueError(f"Embedding vector contains non-finite value: {v!r}")
        clean.append(str(f))
    return "[" + ",".join(clean) + "]"


# Anonymous / fixture user_ids that should NEVER be treated as a real
# tenant owner. If session.user_id matches one of these, owner_id
# resolution falls back to None (legacy single-tenant path).
_ANONYMOUS_USER_IDS = frozenset(
    {"anonymous", "default", "user", "local-dev", "simulation", "simulation_user", ""}
)


def _resolve_owner_id(tool_context: ToolContext | None) -> str | None:
    """Return the per-user owner_id for the current tool invocation.

    Per-user isolation is enabled whenever ``SAAS_MODE=on`` OR
    ``PLANNER_PER_USER_ISOLATION=on``. When both flags are off, every
    store call uses ``owner_id=None`` (legacy single-tenant path).

    Resolution order when isolation is enabled:
      1. ``tool_context.session.user_id``
      2. ``tool_context.state`` (``user_id``, ``acting_user_id``, ``userId``,
         ``tenant.user_id``)
      3. ``simulation_registry.get_current_user_id()``

    When ``SAAS_MODE=on`` and no valid non-anonymous ``user_id`` can be
    resolved (or component validation fails), raises ``PermissionError`` /
    ``ValueError`` instead of returning ``None`` (fail closed). When
    ``SAAS_MODE=off`` and only ``PLANNER_PER_USER_ISOLATION=on``, returns
    ``None`` for missing/anonymous sessions to preserve legacy compatibility.
    """
    saas_on = tenant_keys._saas_mode_enabled()
    flag_on = os.environ.get("PLANNER_PER_USER_ISOLATION", "").strip().lower() == "on"
    if not saas_on and not flag_on:
        return None

    candidates: list[object] = []
    if tool_context is not None:
        session = getattr(tool_context, "session", None)
        if session is not None:
            candidates.append(getattr(session, "user_id", None))
        state = getattr(tool_context, "state", None)
        if state is not None and hasattr(state, "get"):
            for key in ("user_id", "acting_user_id", "userId"):
                candidates.append(state.get(key))
            tenant = state.get("tenant")
            if isinstance(tenant, Mapping):
                candidates.append(tenant.get("user_id"))
    candidates.append(simulation_registry.get_current_user_id())

    for raw in candidates:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned or cleaned.lower() in _ANONYMOUS_USER_IDS:
            continue
        if saas_on:
            tenant_keys._validate_component(cleaned, "user_id")
        return cleaned

    if saas_on:
        raise PermissionError("Authenticated user_id is required for memory access when SAAS_MODE=on")
    return None


# ---------------------------------------------------------------------------
# Auto-store simulation summary (fires inside record_simulation)
# NOTE: The leadership-domain equivalent was removed in Phase 2D;
#       marathon retains this intentionally (legacy/frozen scope).
# ---------------------------------------------------------------------------


async def _auto_store_summary(
    route_id: str,
    parsed_result: dict,
    tool_context: ToolContext,
) -> bool:
    """Best-effort store of a simulation summary for later vector search.

    Extracts prompt/city from ``tool_context.state`` and builds a combined
    summary string from the prompt + simulation result.  Silently no-ops if
    AlloyDB is unavailable, if running in local Postgres mode, or on any
    error -- this should not block the main ``record_simulation`` flow.
    """
    import logging
    import uuid

    import asyncpg

    logger = logging.getLogger(__name__)

    host = os.environ.get("ALLOYDB_HOST", "").strip()
    if not host:
        return True  # No database host configured

    # Extract context from session state (set by the agent during planning)
    raw_state = getattr(tool_context, "state", None) if tool_context is not None else None
    state = raw_state if raw_state is not None and hasattr(raw_state, "get") else {}
    prompt = state.get("user_prompt", state.get("prompt", ""))
    city = state.get("city", "")

    # If prompt is empty, try to build one from the result
    if not prompt:
        prompt = parsed_result.get("prompt", parsed_result.get("description", "simulation run"))

    try:
        # Build a combined summary for embedding
        result_summary = _json_dumps(parsed_result, default=str)[:500]
        summary_text = f"Prompt: {prompt}. City: {city or 'unknown'}. Result: {result_summary}"

        schema = _validate_schema_name(os.environ.get("ALLOYDB_SCHEMA"), default="public")
        dsn = _get_dsn()

        backend = _resolve_embedding_backend()
        embedding_vec: list[float] | None = None
        if backend == "vertex_ai":
            try:
                embedding_vec = await _embeddings.compute_embedding(summary_text)
            except Exception as exc:
                logger.debug("Auto-store embedding failed (non-fatal): %s", exc)

        conn = await asyncpg.connect(dsn, server_settings={"search_path": f"{schema}, public"})
        try:
            owner_id = _resolve_owner_id(tool_context)
            if embedding_vec is not None:
                await conn.execute(
                    """
                    INSERT INTO simulation_summaries
                        (summary_id, city, prompt, summary, route_id, sim_result, created_at, embedding, owner_id)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, now(), $7::vector, $8)
                    """,
                    str(uuid.uuid4()),
                    city or None,
                    prompt,
                    summary_text,
                    route_id,
                    _json_dumps(parsed_result, default=str),
                    _to_pgvector(embedding_vec),
                    owner_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO simulation_summaries
                        (summary_id, city, prompt, summary, route_id, sim_result, created_at, owner_id)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, now(), $7)
                    """,
                    str(uuid.uuid4()),
                    city or None,
                    prompt,
                    summary_text,
                    route_id,
                    _json_dumps(parsed_result, default=str),
                    owner_id,
                )
            logger.info("Auto-stored simulation summary for route %s", route_id)
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning("Failed to auto-store simulation summary (non-fatal): %s", exc)
        return False

    return True


# ---------------------------------------------------------------------------
# ADK tool functions
# ---------------------------------------------------------------------------


async def store_route(tool_context: ToolContext) -> dict:
    """Persist the currently-active marathon route from session state.

    Reads ``marathon_route`` (and optional ``evaluation_result``) from
    ``tool_context.state``.  Large payloads never cross the LLM boundary
    as function arguments — the LLM passes nothing here.

    Returns:
        ``{"status": "success", "route_id": "<uuid>"}`` on success, or
        ``{"status": "error", "message": "..."}`` if the required state is missing.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if state is None or not hasattr(state, "get"):
        return {
            "status": "error",
            "message": "No session state available.",
        }
    route_data = state.get("marathon_route")
    if not isinstance(route_data, dict) or not route_data:
        return {
            "status": "error",
            "message": (
                "No marathon_route in session state. "
                "Call plan_marathon_route (or get_route(activate_route=True)) first."
            ),
        }

    eval_result = state.get("evaluation_result")
    eval_result_dict: dict | None = eval_result if isinstance(eval_result, dict) else None
    eval_score: float | None = None
    if eval_result_dict is not None:
        score = eval_result_dict.get("overall_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            try:
                parsed_score = float(score)
            except (TypeError, ValueError, OverflowError):
                parsed_score = float("nan")
            if math.isfinite(parsed_score):
                eval_score = parsed_score

    try:
        owner_id = _resolve_owner_id(tool_context)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}

    route_id = await _store.store_route(
        route_data=route_data,
        evaluation_score=eval_score,
        evaluation_result=eval_result_dict,
        owner_id=owner_id,
    )
    state["active_route_id"] = route_id
    return {"status": "success", "route_id": route_id}


async def record_simulation(route_id: str, tool_context: ToolContext) -> dict:
    """Record the most recent simulation result against an existing route.

    Reads ``simulation_result`` from ``tool_context.state`` (set by
    ``submit_plan_to_simulator`` after a successful simulator call).
    The LLM passes only the ``route_id``; the large simulation payload
    never crosses the LLM boundary as a function argument.

    Also automatically stores a summary into ``simulation_summaries`` for
    later vector search (no separate tool call needed).  The vector-corpus
    write is best-effort — a transient failure is logged but does **not**
    fail the tool; the primary simulation record is the source of truth.

    Returns:
        ``{"status": "success", "simulation_id": "<uuid>"}`` on success,
        ``{"status": "error", ...}`` if state is missing or route_id unknown.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if state is None or not hasattr(state, "get"):
        return {
            "status": "error",
            "message": "No session state available.",
        }
    parsed_result = state.get("simulation_result")
    if not isinstance(parsed_result, dict):
        return {
            "status": "error",
            "message": (
                "No simulation_result in session state. Call submit_plan_to_simulator(action='execute') first."
            ),
        }

    try:
        owner_id = _resolve_owner_id(tool_context)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}

    sim_id = await _store.record_simulation(
        route_id=route_id,
        simulation_result=parsed_result,
        owner_id=owner_id,
    )
    if sim_id is None:
        return {
            "status": "error",
            "message": f"Route not found: {route_id}",
        }

    # --- Auto-store simulation summary for semantic recall ---
    stored_ok = await _auto_store_summary(
        route_id=route_id,
        parsed_result=parsed_result,
        tool_context=tool_context,
    )
    if not stored_ok:
        logger.warning(
            "record_simulation: vector-corpus write failed for %s (non-fatal)",
            route_id,
        )

    logger.info("record_simulation: attached result %s to route %s", sim_id, route_id)
    return {"status": "success", "route_id": route_id, "simulation_id": sim_id}


async def recall_routes(
    tool_context: ToolContext,
    count: int = 10,
    sort_by: str = "recent",
) -> dict:
    """Query stored routes.

    Args:
        tool_context: ADK tool context.
        count: Maximum number of routes to return.
        sort_by: ``"recent"`` or ``"best_score"``.

    Returns:
        ``{"status": "success", "count": N, "routes": [...]}``.
    """
    try:
        owner_id = _resolve_owner_id(tool_context)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "message": str(exc), "count": 0, "routes": []}

    safe_count = _clamp_non_negative_limit(count, default=10, max_limit=500)
    routes = await _store.recall_routes(
        count=safe_count,
        sort_by=sort_by,
        owner_id=owner_id,
    )
    serialised = [_route_to_dict(r) for r in routes]
    return {"status": "success", "count": len(serialised), "routes": serialised}


async def get_route(
    route_id: str,
    tool_context: ToolContext,
    activate_route: bool = False,
) -> dict:
    """Retrieve a specific route by UUID.

    Args:
        route_id: UUID of the route.
        tool_context: ADK tool context.
        activate_route: If True, load the route's GeoJSON into session state
            (``tool_context.state["marathon_route"]``).

    Returns:
        ``{"status": "success", "route": {...}}`` or
        ``{"status": "error", "message": "..."}``.
    """
    try:
        owner_id = _resolve_owner_id(tool_context)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}

    route = await _store.get_route(route_id, owner_id=owner_id)
    if route is None:
        return {"status": "error", "message": f"Route not found: {route_id}"}

    result = {"status": "success", "route": _route_to_dict(route)}

    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if activate_route and state is not None and hasattr(state, "__setitem__"):
        state["marathon_route"] = route.route_data
        state["active_route_id"] = route.route_id
        if isinstance(route.route_data, dict):
            state["route_name"] = route.route_data.get(
                "name", route.route_data.get("theme", "Stored Route")
            )
        else:
            state["route_name"] = "Stored Route"
        if route.evaluation_score is not None:
            state["evaluation_score"] = route.evaluation_score
        if route.evaluation_result is not None:
            state["evaluation_result"] = route.evaluation_result
        result["activated"] = True

    return result


async def get_best_route(tool_context: ToolContext) -> dict:
    """Return the highest-scoring route.

    Args:
        tool_context: ADK tool context.

    Returns:
        ``{"status": "success", "route": {...}}`` or
        ``{"status": "error", "message": "..."}``.
    """
    try:
        owner_id = _resolve_owner_id(tool_context)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}

    route = await _store.get_best_route(owner_id=owner_id)
    if route is None:
        return {
            "status": "error",
            "message": "No scored routes found.",
        }
    return {"status": "success", "route": _route_to_dict(route)}


async def get_planned_routes_data(
    tool_context: ToolContext,
    route_ids: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """Retrieve route data and evaluation results for batch display.

    If route_ids are provided, fetches those specific routes. Otherwise
    fetches recent routes up to ``limit``.  Returns structured route data
    so the LLM can compose A2UI cards via ``validate_and_emit_a2ui``.

    Args:
        route_ids: UUIDs to fetch. If None, fetches recent routes.
        tool_context: ADK tool context.
        limit: Max routes when route_ids is None.

    Returns:
        ``{"status": "success", "count": N, "routes": [...]}``
        or ``{"status": "error", "message": "..."}``.
    """
    try:
        owner_id = _resolve_owner_id(tool_context)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}

    if route_ids:
        routes = list(await asyncio.gather(*(_store.get_route(rid, owner_id=owner_id) for rid in route_ids)))
        routes = [r for r in routes if r]
    else:
        safe_limit = _clamp_non_negative_limit(limit, default=5, max_limit=500)
        routes = await _store.recall_routes(
            count=safe_limit,
            sort_by="recent",
            owner_id=owner_id,
        )

    if not routes:
        return {"status": "error", "message": "No routes found."}

    route_dicts = []
    for route in routes:
        name = "Route"
        distance = "\u2014"
        if isinstance(route.route_data, dict):
            name = route.route_data.get("name", route.route_data.get("theme", f"Route {route.route_id[:8]}"))
            distance = route.route_data.get("total_distance_miles", route.route_data.get("distance", "\u2014"))

        route_dicts.append(
            {
                "route_id": route.route_id,
                "name": name,
                "distance": distance,
                "evaluation_score": route.evaluation_score,
                "created_at": route.created_at.isoformat() if route.created_at else None,
            }
        )

    return {
        "status": "success",
        "count": len(route_dicts),
        "routes": route_dicts,
    }


async def get_local_and_traffic_rules(
    query: str,
    tool_context: ToolContext,
    city: str | None = None,
    limit: int = 5,
) -> dict:
    """Search local traffic rules relevant to marathon planning.

    Performs a vector similarity search on the AlloyDB ``rules`` table
    using the query text.  Optionally filters by city.

    Returns an explicit error if ``ALLOYDB_HOST`` is unset, if running
    in local Postgres mode (``USE_ALLOYDB=false``) without
    ``EMBEDDING_BACKEND=vertex_ai``, or if the database / embedding call
    fails.

    Args:
        query: Natural-language description of what you are looking for, e.g.
            ``"restrictions for running a race on the Las Vegas strip"``.
        tool_context: ADK tool context.
        city: Optional city name to filter results (e.g. ``"Las Vegas"``).
        limit: Maximum number of rule chunks to return (default 5).

    Returns:
        ``{"status": "success", "rules": [{"city": ..., "text": ...}]}``
        or ``{"status": "error", "message": "..."}``
    """
    import asyncpg

    host = os.environ.get("ALLOYDB_HOST", "").strip()
    if not host:
        return {
            "status": "error",
            "message": "ALLOYDB_HOST is not configured. Cannot query rules.",
        }

    backend = _resolve_embedding_backend()
    use_alloydb = _use_alloydb_flag()

    if use_alloydb == "false" and backend != "vertex_ai":
        return {
            "status": "error",
            "message": (
                "Cannot query rules in local Postgres mode (USE_ALLOYDB=false) "
                "without EMBEDDING_BACKEND=vertex_ai; ai.embedding() is unavailable."
            ),
        }

    schema = _validate_schema_name(os.environ.get("ALLOYDB_SCHEMA"), default="public")

    safe_limit = _clamp_non_negative_limit(limit, default=5, max_limit=100)
    if safe_limit == 0:
        return {
            "status": "success",
            "count": 0,
            "rules": [],
            "message": "No rules requested (limit=0).",
        }

    dsn = _get_dsn()

    try:
        conn = await asyncpg.connect(dsn, server_settings={"search_path": f"{schema}, public"})
        try:
            if backend == "vertex_ai":
                from agents.planner_with_memory.memory import embeddings

                query_vec = _to_pgvector(await embeddings.compute_embedding(query))
                if city:
                    rows = await conn.fetch(
                        """
                        SELECT city, text,
                               (embedding <=> $1::vector) AS distance
                        FROM rules
                        WHERE city = $2
                        ORDER BY distance ASC
                        LIMIT $3
                        """,
                        query_vec,
                        city,
                        safe_limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT city, text,
                               (embedding <=> $1::vector) AS distance
                        FROM rules
                        ORDER BY distance ASC
                        LIMIT $2
                        """,
                        query_vec,
                        safe_limit,
                    )
            else:
                if city:
                    rows = await conn.fetch(
                        """
                        SELECT city, text,
                               (embedding <=> ai.embedding('gemini-embedding-001', $1)::vector) AS distance
                        FROM rules
                        WHERE city = $2
                        ORDER BY distance ASC
                        LIMIT $3
                        """,
                        query,
                        city,
                        safe_limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT city, text,
                               (embedding <=> ai.embedding('gemini-embedding-001', $1)::vector) AS distance
                        FROM rules
                        ORDER BY distance ASC
                        LIMIT $2
                        """,
                        query,
                        safe_limit,
                    )
        finally:
            await conn.close()
    except Exception as exc:
        exc_str = str(exc)
        if backend != "vertex_ai" and ("ai.embedding" in exc_str or "function ai." in exc_str):
            return {
                "status": "error",
                "message": (
                    "ai.embedding() is unavailable on this PostgreSQL instance; "
                    f"set EMBEDDING_BACKEND=vertex_ai for client-side embeddings. ({exc})"
                ),
            }
        return {"status": "error", "message": f"Failed to query rules: {exc}"}

    rules = [{"city": r["city"], "text": r["text"]} for r in rows]

    message = "No local traffic rules found."
    if rules:
        message = "Found the following rules:\n" + "\n\n".join([f"- {r['city']}:\n{r['text']}" for r in rules])

    return {
        "status": "success",
        "count": len(rules),
        "rules": rules,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Simulation history tools (RAG over past simulations)
# ---------------------------------------------------------------------------



async def recall_past_simulations(
    query: str,
    tool_context: ToolContext,
    city: str | None = None,
    limit: int = 2,
) -> dict:
    """Retrieve the most relevant past simulation summaries via semantic search.

    Performs a vector similarity search on the ``simulation_summaries`` table
    using the query text.  Returns the top N most similar past simulation
    summaries so the agent can reference prior runs.

    Returns an explicit error if ``ALLOYDB_HOST`` is unset, if running
    in local Postgres mode (``USE_ALLOYDB=false``) without
    ``EMBEDDING_BACKEND=vertex_ai``, or if the database / embedding call
    fails.

    Args:
        query: Natural-language description of what you are planning, e.g.
            ``"marathon in Las Vegas for 10000 runners"``.
        tool_context: ADK tool context.
        city: Optional city name to filter results.
        limit: Maximum number of past simulations to return (default 2).

    Returns:
        ``{"status": "success", "count": N, "simulations": [...]}`` where each
        simulation contains ``city``, ``prompt``, ``summary``, ``sim_result``,
        and ``created_at``, or ``{"status": "error", "message": "..."}``.
    """
    import asyncpg

    # Persist the user's prompt and city in session state so that
    # _auto_store_summary (inside record_simulation) can build a
    # summary later.
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if state is not None and hasattr(state, "__setitem__"):
        state["user_prompt"] = query
        if city:
            state["city"] = city

    host = os.environ.get("ALLOYDB_HOST", "").strip()
    if not host:
        return {
            "status": "error",
            "message": "ALLOYDB_HOST is not configured. Cannot query past simulations.",
        }

    backend = _resolve_embedding_backend()
    use_alloydb = _use_alloydb_flag()

    if use_alloydb == "false" and backend != "vertex_ai":
        return {
            "status": "error",
            "message": (
                "Cannot query past simulations in local Postgres mode (USE_ALLOYDB=false) "
                "without EMBEDDING_BACKEND=vertex_ai; ai.embedding() is unavailable."
            ),
        }

    schema = _validate_schema_name(os.environ.get("ALLOYDB_SCHEMA"), default="public")

    safe_limit = _clamp_non_negative_limit(limit, default=2, max_limit=100)
    if safe_limit == 0:
        return {
            "status": "success",
            "count": 0,
            "simulations": [],
            "message": "No past simulations requested (limit=0).",
        }

    dsn = _get_dsn()

    # Per-user tenancy: when an owner_id is resolved, fold an extra
    # `owner_id = $N` predicate into every branch. When owner_id is None
    # (legacy single-tenant path), the queries are byte-identical to the
    # pre-tenancy version.
    try:
        owner_id = _resolve_owner_id(tool_context)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "message": str(exc), "count": 0, "simulations": []}

    try:
        conn = await asyncpg.connect(dsn, server_settings={"search_path": f"{schema}, public"})
        try:
            if backend == "vertex_ai":
                from agents.planner_with_memory.memory import embeddings

                query_vec = _to_pgvector(await embeddings.compute_embedding(query))
                if city and owner_id is not None:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> $1::vector) AS distance
                        FROM simulation_summaries
                        WHERE city = $2 AND owner_id = $3
                        ORDER BY distance ASC
                        LIMIT $4
                        """,
                        query_vec,
                        city,
                        owner_id,
                        safe_limit,
                    )
                elif city:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> $1::vector) AS distance
                        FROM simulation_summaries
                        WHERE city = $2
                        ORDER BY distance ASC
                        LIMIT $3
                        """,
                        query_vec,
                        city,
                        safe_limit,
                    )
                elif owner_id is not None:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> $1::vector) AS distance
                        FROM simulation_summaries
                        WHERE owner_id = $2
                        ORDER BY distance ASC
                        LIMIT $3
                        """,
                        query_vec,
                        owner_id,
                        safe_limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> $1::vector) AS distance
                        FROM simulation_summaries
                        ORDER BY distance ASC
                        LIMIT $2
                        """,
                        query_vec,
                        safe_limit,
                    )
            else:
                if city and owner_id is not None:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> ai.embedding('gemini-embedding-001', $1)::vector) AS distance
                        FROM simulation_summaries
                        WHERE city = $2 AND owner_id = $3
                        ORDER BY distance ASC
                        LIMIT $4
                        """,
                        query,
                        city,
                        owner_id,
                        safe_limit,
                    )
                elif city:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> ai.embedding('gemini-embedding-001', $1)::vector) AS distance
                        FROM simulation_summaries
                        WHERE city = $2
                        ORDER BY distance ASC
                        LIMIT $3
                        """,
                        query,
                        city,
                        safe_limit,
                    )
                elif owner_id is not None:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> ai.embedding('gemini-embedding-001', $1)::vector) AS distance
                        FROM simulation_summaries
                        WHERE owner_id = $2
                        ORDER BY distance ASC
                        LIMIT $3
                        """,
                        query,
                        owner_id,
                        safe_limit,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT city, prompt, summary, sim_result, created_at,
                               (embedding <=> ai.embedding('gemini-embedding-001', $1)::vector) AS distance
                        FROM simulation_summaries
                        ORDER BY distance ASC
                        LIMIT $2
                        """,
                        query,
                        safe_limit,
                    )
        finally:
            await conn.close()
    except Exception as exc:
        exc_str = str(exc)
        if backend != "vertex_ai" and ("ai.embedding" in exc_str or "function ai." in exc_str):
            return {
                "status": "error",
                "message": (
                    "ai.embedding() is unavailable on this PostgreSQL instance; "
                    f"set EMBEDDING_BACKEND=vertex_ai for client-side embeddings. ({exc})"
                ),
            }
        return {"status": "error", "message": f"Failed to query past simulations: {exc}"}

    simulations = []
    sim_parts = []
    for i, r in enumerate(rows, 1):
        date_str = r["created_at"].isoformat() if r["created_at"] else "Unknown"
        sim_result = _decode_jsonb(r["sim_result"])
        simulations.append(
            {
                "city": r["city"],
                "prompt": r["prompt"],
                "summary": r["summary"],
                "sim_result": sim_result,
                "created_at": date_str,
            }
        )
        sim_parts.append(
            f"Simulation {i}:\n"
            f"  City: {r['city']}\n"
            f"  Prompt: {r['prompt']}\n"
            f"  Summary: {r['summary']}\n"
            f"  Result: {sim_result}\n"
            f"  Date: {date_str}"
        )
    simulations_text = "\n\n".join(sim_parts)

    message = (
        "No past simulations found."
        if not sim_parts
        else "Found the following past simulations:\n\n" + simulations_text
    )

    return {"status": "success", "count": len(simulations), "simulations": simulations, "message": message}
