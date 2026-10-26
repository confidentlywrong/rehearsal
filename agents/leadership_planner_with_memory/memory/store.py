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

"""In-memory leadership scenario store.

Provides ``ScenarioMemoryStore``, an in-process repository for planned
leadership scenarios and their simulation history.  Exposes the same async
API as :class:`AlloyDBScenarioStore` so it can be swapped in directly:

    from agents.leadership_planner_with_memory.memory import tools
    from agents.leadership_planner_with_memory.memory.store import ScenarioMemoryStore
    tools._store = ScenarioMemoryStore()

Used by unit tests (no AlloyDB required) and as a local fallback when
``ALLOYDB_HOST`` is unset.  Mirrors the marathon
``agents/planner_with_memory/memory/store.py`` pattern but with
leadership-domain naming (scenarios instead of routes).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from agents.leadership_planner_with_memory.memory.schemas import (
    PlannedScenario,
    SimulationRecord,
)


class ScenarioMemoryStore:
    """Async in-memory store for planned scenarios and simulation records.

    Per-user tenancy (opt-in): every method accepts an ``owner_id``
    keyword that scopes reads/writes to a single user. Backwards
    compatibility:

    * ``owner_id=None`` on a write stores the scenario unowned. Existing
      callers that pass no owner keep storing in the legacy single-tenant
      namespace — `make start` behaviour is byte-identical.
    * ``owner_id=None`` on a read returns every scenario regardless of
      its stored owner (single-tenant view, the local-dev default).
    * ``owner_id="x"`` on a read returns ONLY scenarios whose stored
      ``owner_id`` is exactly ``"x"``. Unowned legacy rows are NOT
      surfaced — once a caller opts into tenancy, the isolation is
      strict. A separate backfill step is required to migrate legacy
      rows to a known owner.
    """

    def __init__(self) -> None:
        self._scenarios: dict[str, PlannedScenario] = {}
        # Per-scenario stakeholder-artifact bundles (Artifacts 1-4 from
        # the leadership_planner_with_memory emit_* tools). Keyed by
        # scenario_id; each value is a dict of the six artifact payloads
        # plus an ``owner_id`` for tenancy.
        self._artifacts: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public async API — mirrors AlloyDBScenarioStore
    # ------------------------------------------------------------------

    async def store_scenario(
        self,
        scenario_data: dict,
        evaluation_score: float | None = None,
        evaluation_result: dict | None = None,
        persona: str | None = None,
        owner_id: str | None = None,
    ) -> str:
        """Persist a new planned scenario and return its UUID."""
        scenario_id = str(uuid.uuid4())
        self._scenarios[scenario_id] = PlannedScenario(
            scenario_id=scenario_id,
            scenario_data=scenario_data,
            created_at=datetime.now(tz=timezone.utc),
            evaluation_score=evaluation_score,
            evaluation_result=evaluation_result,
            persona=persona,
            owner_id=owner_id,
        )
        return scenario_id

    async def get_scenario(
        self,
        scenario_id: str,
        owner_id: str | None = None,
    ) -> PlannedScenario | None:
        """Retrieve a scenario by ID, or ``None`` if not found or owner mismatch."""
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            return None
        if owner_id is not None and scenario.owner_id != owner_id:
            return None
        return scenario

    async def record_simulation(
        self,
        scenario_id: str,
        simulation_result: dict,
        owner_id: str | None = None,
    ) -> str | None:
        """Append a simulation record. Returns sim UUID, or ``None`` if no scenario
        is found or the caller does not own it."""
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            return None
        if owner_id is not None and scenario.owner_id != owner_id:
            return None
        sim_id = str(uuid.uuid4())
        scenario.simulations.append(
            SimulationRecord(
                simulation_id=sim_id,
                scenario_id=scenario_id,
                simulation_result=simulation_result,
                simulated_at=datetime.now(tz=timezone.utc),
            )
        )
        return sim_id

    async def update_evaluation(
        self,
        scenario_id: str,
        evaluation_score: float | None,
        evaluation_result: dict | None,
        owner_id: str | None = None,
    ) -> bool:
        """Backfill evaluation_score / evaluation_result on an existing scenario.

        COALESCE semantics: only overwrites fields whose stored value is
        ``None``. An existing non-null score is never replaced, preserving
        the audit trail of the original evaluation. Returns ``True`` if
        the scenario exists, is owned by ``owner_id`` (or owner_id is
        ``None``), and at least one field was eligible for write.
        """
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            return False
        if owner_id is not None and scenario.owner_id != owner_id:
            return False
        changed = False
        if scenario.evaluation_score is None and evaluation_score is not None:
            scenario.evaluation_score = evaluation_score
            changed = True
        if scenario.evaluation_result is None and evaluation_result is not None:
            scenario.evaluation_result = evaluation_result
            changed = True
        return changed

    async def recall_scenarios(
        self,
        count: int = 10,
        sort_by: str = "recent",
        owner_id: str | None = None,
    ) -> list[PlannedScenario]:
        """Return up to ``count`` scenarios sorted by ``recent`` or ``best_score``.

        When ``owner_id`` is set, only scenarios with the matching owner
        are returned; unowned legacy rows are excluded.

        **Sort-ordering invariant** (must match
        :class:`AlloyDBScenarioStore`): ``best_score`` returns scored
        rows highest-first, unscored rows last. The AlloyDB backend
        achieves this via ``ORDER BY eval_score DESC NULLS LAST``;
        the in-memory backend uses a ``(bool, float)`` tuple key.
        """
        scenarios = list(self._scenarios.values())
        if owner_id is not None:
            scenarios = [s for s in scenarios if s.owner_id == owner_id]
        if sort_by == "best_score":
            scenarios.sort(
                key=lambda s: (
                    s.evaluation_score is not None,
                    s.evaluation_score if s.evaluation_score is not None else 0.0,
                ),
                reverse=True,
            )
        else:
            scenarios.sort(key=lambda s: s.created_at, reverse=True)
        return scenarios[:count]

    async def get_best_scenario(
        self,
        owner_id: str | None = None,
    ) -> PlannedScenario | None:
        """Return the highest-scoring scenario, or ``None`` if none are scored.

        When ``owner_id`` is set, only the caller's scenarios are considered.
        """
        scored = [
            s
            for s in self._scenarios.values()
            if s.evaluation_score is not None and (owner_id is None or s.owner_id == owner_id)
        ]
        if not scored:
            return None
        return max(scored, key=lambda s: s.evaluation_score)  # type: ignore[arg-type]

    async def store_scenario_idempotent(
        self,
        scenario_id: str,
        scenario_data: dict,
        created_at: datetime,
        evaluation_score: float | None = None,
        evaluation_result: dict | None = None,
        owner_id: str | None = None,
    ) -> None:
        """Insert a scenario preserving its original ``scenario_id`` (used by seed loader).

        No-op if a scenario with the same ID already exists.
        """
        if scenario_id in self._scenarios:
            return
        self._scenarios[scenario_id] = PlannedScenario(
            scenario_id=scenario_id,
            scenario_data=scenario_data,
            created_at=created_at,
            evaluation_score=evaluation_score,
            evaluation_result=evaluation_result,
            owner_id=owner_id,
        )

    # ------------------------------------------------------------------
    # Stakeholder-artifact persistence (Artifacts 1-4)
    # ------------------------------------------------------------------

    async def store_artifacts(
        self,
        scenario_id: str,
        artifacts: dict,
        owner_id: str | None = None,
    ) -> bool:
        """Upsert the stakeholder-artifact bundle for ``scenario_id``.

        ``artifacts`` is expected to be a dict with any subset of the six
        canonical keys: ``stakeholder_graph``, ``stakeholder_personas``,
        ``stakeholder_reactions``, ``simulation_timeline``,
        ``simulation_report``, ``stakeholder_interview``. Missing keys
        are preserved from the previous bundle (merge semantics) so a
        caller can flush partial state without erasing earlier artifacts.

        Returns ``True`` if the scenario exists and is owned by the
        caller (or ``owner_id`` is ``None``). Returns ``False`` if the
        scenario is unknown or owned by a different user — matching the
        FK + tenancy contract of the AlloyDB-backed implementation.
        """
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            return False
        if owner_id is not None and scenario.owner_id != owner_id:
            return False
        bundle = self._artifacts.setdefault(scenario_id, {"owner_id": scenario.owner_id})
        # Merge: only overwrite keys explicitly provided by the caller.
        for key in (
            "stakeholder_graph",
            "stakeholder_personas",
            "stakeholder_reactions",
            "simulation_timeline",
            "simulation_report",
            "stakeholder_interview",
        ):
            if key in artifacts and artifacts[key] is not None:
                bundle[key] = artifacts[key]
        return True

    async def get_artifacts(
        self,
        scenario_id: str,
        owner_id: str | None = None,
    ) -> dict | None:
        """Return the stakeholder-artifact bundle for ``scenario_id``.

        Returns ``None`` if the scenario is unknown, the caller does not
        own it, or no artifacts have been stored yet. The returned dict
        contains only the artifact keys that were populated — callers
        should treat missing keys as "not produced" rather than empty.
        """
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            return None
        if owner_id is not None and scenario.owner_id != owner_id:
            return None
        bundle = self._artifacts.get(scenario_id)
        if bundle is None:
            return None
        # Strip the internal owner_id marker before returning.
        return {k: v for k, v in bundle.items() if k != "owner_id"}

    # ------------------------------------------------------------------
    # Scenario bundles (joined scenarios + artifacts)
    # ------------------------------------------------------------------

    async def list_scenario_bundles(
        self,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return scenario metadata joined with each scenario's stakeholder artifacts.

        Ordered newest-first, capped at ``limit``. Same owner_id semantics as
        :meth:`recall_scenarios`: ``None`` returns every scenario
        regardless of stored owner; a non-``None`` value returns only
        the strictly matching rows.

        **Return-shape contract** (shared with :class:`AlloyDBScenarioStore`):
        each dict has keys ``scenario_id``, ``eval_score``, ``created_at``,
        ``stakeholder_graph``, ``stakeholder_personas``,
        ``stakeholder_reactions``. Artifact values are ``dict | None``.

        Each returned dict has the shape::

            {
                "scenario_id": str,
                "eval_score": float | None,
                "created_at": datetime | None,
                "stakeholder_graph": dict | None,
                "stakeholder_personas": dict | None,
                "stakeholder_reactions": dict | None,
            }

        Scenarios without a stored artifact bundle still appear (with
        ``None`` for the artifact fields) so the aggregator can count
        them toward the sample size.
        """
        scenarios = list(self._scenarios.values())
        if owner_id is not None:
            scenarios = [s for s in scenarios if s.owner_id == owner_id]
        scenarios.sort(key=lambda s: s.created_at, reverse=True)
        bundles: list[dict] = []
        for scenario in scenarios[:limit]:
            artifacts = self._artifacts.get(scenario.scenario_id) or {}
            bundles.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "eval_score": scenario.evaluation_score,
                    "created_at": scenario.created_at,
                    "stakeholder_graph": artifacts.get("stakeholder_graph"),
                    "stakeholder_personas": artifacts.get("stakeholder_personas"),
                    "stakeholder_reactions": artifacts.get("stakeholder_reactions"),
                    "simulation_timeline": artifacts.get("simulation_timeline"),
                    "simulation_report": artifacts.get("simulation_report"),
                    "stakeholder_interview": artifacts.get("stakeholder_interview"),
                }
            )
        return bundles
