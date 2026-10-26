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

"""Data schemas for leadership scenario memory.

Defines the core data structures used by the AlloyDBScenarioStore to persist
planned scenarios and their simulation history.

Mirrors agents/planner_with_memory/memory/schemas.py but with
leadership-domain naming (scenarios instead of routes).
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SimulationRecord:
    """A record of a single simulation run against a planned scenario."""

    simulation_id: str
    scenario_id: str
    simulation_result: dict
    simulated_at: datetime


@dataclass
class PlannedScenario:
    """A planned leadership scenario with optional evaluation and simulation history.

    ``owner_id`` identifies the user who owns this scenario in the per-user
    tenancy model. ``None`` means "unowned / legacy single-tenant data" and is
    visible to every caller (the local-dev default). When set, only the
    matching owner can read, update, or simulate against this scenario.
    """

    scenario_id: str
    scenario_data: dict
    created_at: datetime
    evaluation_score: float | None = None
    evaluation_result: dict | None = None
    persona: str | None = None
    simulations: list[SimulationRecord] = field(default_factory=list)
    owner_id: str | None = None
