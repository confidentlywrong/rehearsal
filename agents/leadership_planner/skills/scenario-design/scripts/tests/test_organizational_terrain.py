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

"""Tests for Organizational Terrain modifiers in the DCNS engine.

Tests cover:
  - Venue type factors (Places API)
  - Engagement friction (Routes API)
  - Weather pressure modifiers (Weather API)
  - Graceful degradation (all params None)
  - Combined modifier interaction
"""

import sys
from pathlib import Path

import pytest

# Add the scripts directory to the path so we can import dcns_engine
_scripts_dir = str(Path(__file__).resolve().parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import dcns_engine  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_stakeholder(
    role: str, resistance: float = 0.3, influence: float = 0.5, sentiment: float = 0.5, tier: int = 2
) -> dict:
    """Build a minimal stakeholder dict for testing."""
    return {
        "role": role,
        "tier": tier,
        "persona": f"Test {role}",
        "domain_principles": ["efficiency", "innovation"],
        "affinity": [0.5, 0.5, 0.5],
        "initial_resistance": resistance,
        "initial_influence": influence,
        "initial_sentiment": sentiment,
    }


_RESOURCES = {
    "OWB": {"initial": 70, "critical_threshold": 25},
    "Trust": {"initial": 60},
    "Morale": {"initial": 65},
}

# Each stakeholder needs a DISTINCT affinity vector so _influence_distance > 0.
_CEO = _make_stakeholder("ceo", resistance=0.2, influence=0.9, sentiment=0.7, tier=1)
_CEO["affinity"] = [0.9, 0.3, 0.5]

_CTO = _make_stakeholder("cto", resistance=0.3, influence=0.7, sentiment=0.6, tier=1)
_CTO["affinity"] = [0.3, 0.9, 0.6]

_LEGAL = _make_stakeholder("general_counsel", resistance=0.6, influence=0.5, sentiment=0.3, tier=2)
_LEGAL["affinity"] = [0.2, 0.4, 0.9]

_UNION = _make_stakeholder("union_rep", resistance=0.7, influence=0.4, sentiment=0.2, tier=2)
_UNION["affinity"] = [0.7, 0.1, 0.3]


# ---------------------------------------------------------------------------
# 1. Venue Type Factors (Places API)
# ---------------------------------------------------------------------------


class TestVenueTypeFactors:
    """Verify that venue type modifiers affect edge costs."""

    def test_government_increases_time_cost(self):
        """Government venues should have higher time cost (×1.3)."""
        base_cost = dcns_engine._edge_cost(_CEO, _LEGAL, _RESOURCES)
        gov_cost = dcns_engine._edge_cost(
            _CEO,
            _LEGAL,
            _RESOURCES,
            venue_types={"general_counsel": "government"},
        )
        # Time cost (index 0) should be ~30% higher
        assert gov_cost[0] > base_cost[0] * 1.25
        assert gov_cost[0] < base_cost[0] * 1.35
        # Capital cost should be unchanged (no venue capital factor)
        assert gov_cost[1] == pytest.approx(base_cost[1], rel=1e-6)

    def test_university_reduces_time_and_morale(self):
        """University venues should reduce time (×0.9) and morale cost (×0.85)."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        uni_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            venue_types={"cto": "university"},
        )
        assert uni_cost[0] < base_cost[0]  # time reduced
        assert uni_cost[2] < base_cost[2]  # morale reduced

    def test_startup_fastest_time(self):
        """Startup venues should have the lowest time factor (×0.85)."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        startup_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            venue_types={"cto": "startup"},
        )
        assert startup_cost[0] < base_cost[0] * 0.90

    def test_hospital_fast_time_high_morale(self):
        """Hospital venues: fastest time (×0.7) but highest morale cost (×1.2)."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        hospital_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            venue_types={"cto": "hospital"},
        )
        assert hospital_cost[0] < base_cost[0]  # faster
        assert hospital_cost[2] > base_cost[2]  # higher morale cost

    def test_unknown_venue_type_no_effect(self):
        """Unknown venue types should have no effect (factor = 1.0)."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        unknown_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            venue_types={"cto": "spaceship"},
        )
        assert unknown_cost == pytest.approx(base_cost, rel=1e-6)


# ---------------------------------------------------------------------------
# 2. Engagement Friction (Routes API)
# ---------------------------------------------------------------------------


class TestEngagementFriction:
    """Verify that travel time friction modifies edge costs."""

    def test_friction_multiplier_thresholds(self):
        """Verify all 5 travel-time brackets return correct multipliers."""
        assert dcns_engine._friction_multiplier(3.0) == 0.85  # co-located
        assert dcns_engine._friction_multiplier(8.0) == 1.0  # accessible
        assert dcns_engine._friction_multiplier(15.0) == 1.15  # deliberate
        assert dcns_engine._friction_multiplier(25.0) == 1.35  # different worlds
        assert dcns_engine._friction_multiplier(45.0) == 1.5  # silo

    def test_friction_none_returns_neutral(self):
        """None travel time should return neutral multiplier (1.0)."""
        assert dcns_engine._friction_multiplier(None) == 1.0

    def test_colocated_reduces_cost(self):
        """Co-located stakeholders (≤5 min) should have reduced costs."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        close_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            travel_matrix={"ceo→cto": 4.0},
        )
        assert close_cost[0] < base_cost[0]  # friction 0.85 reduces time
        assert close_cost[1] < base_cost[1]  # friction reduces capital too

    def test_distant_increases_cost(self):
        """Distant stakeholders (20-30 min) should have increased costs."""
        base_cost = dcns_engine._edge_cost(_CEO, _UNION, _RESOURCES)
        far_cost = dcns_engine._edge_cost(
            _CEO,
            _UNION,
            _RESOURCES,
            travel_matrix={"ceo→union_rep": 25.0},
        )
        assert far_cost[0] > base_cost[0]  # friction 1.35 increases time
        assert far_cost[1] > base_cost[1]  # friction increases capital

    def test_reverse_key_lookup(self):
        """Travel matrix should work with reverse key (b→a when a→b missing)."""
        forward_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            travel_matrix={"ceo→cto": 12.0},
        )
        reverse_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            travel_matrix={"cto→ceo": 12.0},
        )
        assert forward_cost == pytest.approx(reverse_cost, rel=1e-6)


# ---------------------------------------------------------------------------
# 3. Weather Pressure (Weather API)
# ---------------------------------------------------------------------------


class TestWeatherPressure:
    """Verify that weather modifiers affect edge costs."""

    def test_burning_platform_faster_but_costlier(self):
        """Extreme heat: time_factor=0.8 (faster), morale_factor=1.3 (costlier)."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        heat_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            weather={"time_factor": 0.8, "morale_factor": 1.3, "capital_factor": 1.0, "distance_factor": 1.0},
        )
        assert heat_cost[0] < base_cost[0]  # faster decisions
        assert heat_cost[2] > base_cost[2]  # higher morale cost

    def test_storm_increases_all_costs(self):
        """Storm: everything more expensive."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        storm_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            weather={"time_factor": 1.2, "capital_factor": 1.15, "morale_factor": 1.1, "distance_factor": 1.0},
        )
        assert storm_cost[0] > base_cost[0]
        assert storm_cost[1] > base_cost[1]
        assert storm_cost[2] > base_cost[2]

    def test_fog_amplifies_distance(self):
        """Fog: distance_factor=1.15 should amplify influence distance."""
        base_cost = dcns_engine._edge_cost(_CEO, _UNION, _RESOURCES)
        fog_cost = dcns_engine._edge_cost(
            _CEO,
            _UNION,
            _RESOURCES,
            weather={"time_factor": 1.05, "capital_factor": 1.0, "morale_factor": 1.1, "distance_factor": 1.15},
        )
        # All costs should increase because base distance is amplified
        assert fog_cost[0] > base_cost[0]

    def test_clear_weather_no_effect(self):
        """Clear weather (all factors 1.0) should produce identical costs."""
        base_cost = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        clear_cost = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            weather={"time_factor": 1.0, "capital_factor": 1.0, "morale_factor": 1.0, "distance_factor": 1.0},
        )
        assert clear_cost == pytest.approx(base_cost, rel=1e-6)


# ---------------------------------------------------------------------------
# 4. Graceful Degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Verify that None params produce identical results to baseline."""

    def test_edge_cost_none_params(self):
        """All None terrain params should equal baseline edge cost."""
        base = dcns_engine._edge_cost(_CEO, _CTO, _RESOURCES)
        terrain = dcns_engine._edge_cost(
            _CEO,
            _CTO,
            _RESOURCES,
            venue_types=None,
            travel_matrix=None,
            weather=None,
        )
        assert terrain == pytest.approx(base, rel=1e-6)

    def test_influence_graph_none_params(self):
        """Influence graph with None terrain params should equal baseline."""
        stakeholders = [_CEO, _CTO, _LEGAL]
        base_adj = dcns_engine._build_influence_graph(stakeholders, _RESOURCES)
        terrain_adj = dcns_engine._build_influence_graph(
            stakeholders,
            _RESOURCES,
            venue_types=None,
            travel_matrix=None,
            weather=None,
        )
        for node in base_adj:
            for (b_neighbor, b_cost), (t_neighbor, t_cost) in zip(base_adj[node], terrain_adj[node]):
                assert b_neighbor == t_neighbor
                assert b_cost == pytest.approx(t_cost, rel=1e-6)


# ---------------------------------------------------------------------------
# 5. Combined Modifiers
# ---------------------------------------------------------------------------


class TestCombinedModifiers:
    """Verify that all three modifiers interact correctly."""

    def test_combined_effect_multiplicative(self):
        """All three modifiers should multiply together."""
        base_cost = dcns_engine._edge_cost(_CEO, _LEGAL, _RESOURCES)

        combined_cost = dcns_engine._edge_cost(
            _CEO,
            _LEGAL,
            _RESOURCES,
            venue_types={"general_counsel": "government"},  # time ×1.3
            travel_matrix={"ceo→general_counsel": 22.0},  # friction ×1.35
            weather={
                "time_factor": 0.8,
                "capital_factor": 1.0,
                "morale_factor": 1.3,
                "distance_factor": 1.0,
            },  # time ×0.8
        )

        # Time: base × 1.3 (gov) × 1.35 (friction) × 0.8 (weather) ≈ base × 1.404
        expected_time_ratio = 1.3 * 1.35 * 0.8
        actual_time_ratio = combined_cost[0] / base_cost[0]
        assert actual_time_ratio == pytest.approx(expected_time_ratio, rel=0.01)

    def test_pathway_differs_with_terrain(self):
        """A full pathway should produce different costs with terrain modifiers."""
        stakeholders = [
            {
                **_make_stakeholder("ceo", resistance=0.2, influence=0.9, sentiment=0.7, tier=1),
                "affinity": [0.9, 0.3, 0.5],
            },
            {
                **_make_stakeholder("cto", resistance=0.3, influence=0.7, sentiment=0.6, tier=1),
                "affinity": [0.3, 0.9, 0.6],
            },
            {
                **_make_stakeholder("cfo", resistance=0.25, influence=0.8, sentiment=0.65, tier=1),
                "affinity": [0.6, 0.5, 0.8],
            },
            {
                **_make_stakeholder("general_counsel", resistance=0.6, influence=0.5, sentiment=0.3, tier=2),
                "affinity": [0.2, 0.4, 0.9],
            },
            {
                **_make_stakeholder("union_rep", resistance=0.7, influence=0.4, sentiment=0.2, tier=2),
                "affinity": [0.7, 0.1, 0.3],
            },
        ]

        baseline = dcns_engine._generate_consensus_pathway_pca(
            stakeholders,
            _RESOURCES,
            scenario_type="transformation",
            runner_count=5,
            seed=42,
        )

        terrain = dcns_engine._generate_consensus_pathway_pca(
            stakeholders,
            _RESOURCES,
            scenario_type="transformation",
            runner_count=5,
            seed=42,
            venue_types={
                "cto": "university",
                "general_counsel": "government",
                "union_rep": "union_hall",
            },
            travel_matrix={
                "ceo→cto": 12.0,
                "ceo→union_rep": 22.0,
                "cfo→ceo": 6.0,
            },
            weather_modifier={
                "time_factor": 0.8,
                "capital_factor": 1.0,
                "morale_factor": 1.3,
                "resistance_delta": 0.0,
                "distance_factor": 1.0,
                "perturbation_range": [0.7, 1.5],
            },
        )

        # Costs should differ
        baseline_cost = baseline["cost"]
        terrain_cost = terrain["cost"]
        assert baseline_cost["time"] != pytest.approx(terrain_cost["time"], rel=0.01)
