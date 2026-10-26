# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
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

"""Data schemas for route memory.

Defines the core data structures used by the RouteMemoryStore to persist
planned routes and their simulation history.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SimulationRecord:
    """A record of a single simulation run against a planned route."""

    simulation_id: str
    route_id: str
    simulation_result: dict
    simulated_at: datetime


@dataclass
class PlannedRoute:
    """A planned marathon route with optional evaluation and simulation history."""

    route_id: str
    route_data: dict
    created_at: datetime
    evaluation_score: float | None = None
    evaluation_result: dict | None = None
    simulations: list[SimulationRecord] = field(default_factory=list)
    # Per-user tenancy: identifies the user who owns this route.
    # ``None`` means "unowned / pre-tenancy", which keeps the
    # single-tenant local-dev path byte-identical.
    owner_id: str | None = None
