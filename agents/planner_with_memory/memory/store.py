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

"""In-memory route memory store.

Provides ``RouteMemoryStore``, an in-memory repository for planned routes and
their simulation history. Production deployments use ``AlloyDBRouteStore``
(``store_alloydb.py``) for persistent PostgreSQL/AlloyDB storage, while
``RouteMemoryStore`` serves as the in-memory store and unit-test double.
"""

import math
import uuid
from datetime import datetime, timezone

from agents.planner_with_memory.memory.schemas import PlannedRoute, SimulationRecord


def _sanitize_score(score: float | None) -> float | None:
    if score is None or isinstance(score, bool):
        return None
    try:
        val = float(score)
    except (TypeError, ValueError, OverflowError):
        return None
    return val if math.isfinite(val) else None


class RouteMemoryStore:
    """In-memory store for planned routes and simulation records."""

    def __init__(self) -> None:
        self._routes: dict[str, PlannedRoute] = {}
        # Load pre-generated seed plans from disk
        from agents.planner_with_memory.memory.seeds import load_seeds

        load_seeds(self)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store_route(
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
        route = PlannedRoute(
            route_id=route_id,
            route_data=route_data,
            created_at=datetime.now(tz=timezone.utc),
            evaluation_score=_sanitize_score(evaluation_score),
            evaluation_result=evaluation_result,
            owner_id=owner_id,
        )
        self._routes[route_id] = route
        return route_id

    def get_route(
        self,
        route_id: str,
        owner_id: str | None = None,
    ) -> PlannedRoute | None:
        """Retrieve a route by ID, or ``None`` if not found / owner mismatch.

        When ``owner_id`` is set, the route must have a matching
        ``owner_id`` to be returned. Unowned legacy rows are NOT
        surfaced under a scoped read — callers that opt into tenancy
        get strict isolation.
        """
        route = self._routes.get(route_id)
        if route is None:
            return None
        if owner_id is not None and route.owner_id != owner_id:
            return None
        return route

    def record_simulation(
        self,
        route_id: str,
        simulation_result: dict,
        owner_id: str | None = None,
    ) -> str | None:
        """Append a simulation record to *route_id*.

        Returns the simulation UUID on success, ``None`` if the route does
        not exist OR the caller does not own it when ``owner_id`` is set.
        """
        route = self._routes.get(route_id)
        if route is None:
            return None
        if owner_id is not None and route.owner_id != owner_id:
            return None

        sim_id = str(uuid.uuid4())
        record = SimulationRecord(
            simulation_id=sim_id,
            route_id=route_id,
            simulation_result=simulation_result,
            simulated_at=datetime.now(tz=timezone.utc),
        )
        route.simulations.append(record)
        return sim_id

    def recall_routes(
        self,
        count: int = 10,
        sort_by: str = "recent",
        owner_id: str | None = None,
    ) -> list[PlannedRoute]:
        """Return up to *count* routes, sorted by *sort_by*.

        When ``owner_id`` is set, only routes owned by that user are
        returned; unowned legacy rows are excluded.

        Args:
            count: Maximum number of routes to return.
            sort_by: ``"recent"`` (newest first) or ``"best_score"``
                (highest evaluation_score first; unscored routes sort last).
            owner_id: Per-user tenancy scope; ``None`` returns all routes.
        """
        if count is None or isinstance(count, bool):
            safe_count = 10
        elif isinstance(count, int):
            safe_count = max(0, count)
        elif isinstance(count, float):
            safe_count = max(0, int(count)) if math.isfinite(count) else 10
        else:
            try:
                safe_count = max(0, int(count))
            except (ValueError, TypeError, OverflowError):
                safe_count = 10
        if safe_count == 0:
            return []
        if owner_id is None:
            routes = list(self._routes.values())
        else:
            routes = [r for r in self._routes.values() if r.owner_id == owner_id]
        if sort_by == "best_score":
            routes.sort(
                key=lambda r: (
                    _sanitize_score(r.evaluation_score) is not None,
                    _sanitize_score(r.evaluation_score) or 0.0,
                ),
                reverse=True,
            )
        else:  # "recent"
            routes.sort(key=lambda r: r.created_at, reverse=True)
        return routes[:min(safe_count, len(routes))]

    def get_best_route(self, owner_id: str | None = None) -> PlannedRoute | None:
        """Return the route with the highest evaluation score, or ``None``.

        Routes without an evaluation score are excluded. When ``owner_id``
        is set, only the caller's routes are considered.
        """
        if owner_id is None:
            scored = [r for r in self._routes.values() if _sanitize_score(r.evaluation_score) is not None]
        else:
            scored = [
                r
                for r in self._routes.values()
                if _sanitize_score(r.evaluation_score) is not None and r.owner_id == owner_id
            ]
        if not scored:
            return None
        return max(scored, key=lambda r: _sanitize_score(r.evaluation_score) or 0.0)
