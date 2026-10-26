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

"""Unit tests for the DCNS mathematical engine."""

import sys
import os

import pytest

# Add the scripts directory to the path so we can import dcns_engine directly
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "skills",
        "scenario-design",
        "scripts",
    ),
)
import dcns_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TEMPLATES = [
    {
        "role": "cto",
        "tier": 1,
        "domain_principles": ["Adaptive Decision-Making & Agility", "Strategic Foresight"],
        "affinity": [0.8, 0.9, 0.5, 0.6, 0.3, 0.7, 0.6],
        "initial_sentiment": 0.7,
        "initial_resistance": 0.2,
        "initial_influence": 0.8,
        "persona": "CTO driving AI transformation.",
    },
    {
        "role": "ethics_lead",
        "tier": 1,
        "domain_principles": ["Ethical Leadership & Governance"],
        "affinity": [0.4, 0.5, 0.7, 0.9, 0.6, 0.5, 0.4],
        "initial_sentiment": 0.5,
        "initial_resistance": 0.4,
        "initial_influence": 0.7,
        "persona": "Ethics lead focused on AI governance.",
    },
    {
        "role": "cfo",
        "tier": 1,
        "domain_principles": ["Resource Optimization & Strategic Allocation"],
        "affinity": [0.5, 0.3, 0.4, 0.3, 0.2, 0.4, 0.9],
        "initial_sentiment": 0.3,
        "initial_resistance": 0.6,
        "initial_influence": 0.85,
        "persona": "CFO focused on ROI.",
    },
    {
        "role": "data_scientist",
        "tier": 2,
        "domain_principles": ["Adaptive Decision-Making & Agility"],
        "affinity": [0.6, 0.8, 0.4, 0.5, 0.3, 0.6, 0.5],
        "initial_sentiment": 0.8,
        "initial_resistance": 0.1,
        "initial_influence": 0.5,
        "persona": "Data scientist eager for AI tools.",
    },
    {
        "role": "union_rep",
        "tier": 3,
        "domain_principles": ["Empowerment & Cultivating Distributed Leadership"],
        "affinity": [0.3, 0.4, 0.6, 0.4, 0.7, 0.6, 0.4],
        "initial_sentiment": 0.4,
        "initial_resistance": 0.5,
        "initial_influence": 0.3,
        "persona": "Union rep concerned about job security.",
    },
    {
        "role": "consultant",
        "tier": 3,
        "domain_principles": ["Strategic Foresight"],
        "affinity": [0.7, 0.7, 0.3, 0.4, 0.3, 0.5, 0.7],
        "initial_sentiment": 0.7,
        "initial_resistance": 0.1,
        "initial_influence": 0.3,
        "persona": "External consultant.",
    },
]

SAMPLE_RESOURCES = {
    "LAU": {"initial": 100, "regen_per_tick": 5, "critical_threshold": 20},
    "BT": {"initial": 500, "regen_per_tick": 0, "critical_threshold": 50},
    "TBU": {"initial": 80, "regen_per_tick": 2, "critical_threshold": 15},
    "OWB": {"initial": 70, "regen_per_tick": -1, "critical_threshold": 25},
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInfluenceDistance:
    def test_returns_value_in_unit_range(self):
        for i, s1 in enumerate(SAMPLE_TEMPLATES):
            for j, s2 in enumerate(SAMPLE_TEMPLATES):
                d = dcns_engine._influence_distance(s1, s2)
                assert 0.0 <= d <= 1.0, f"distance({i},{j})={d} out of [0,1]"

    def test_self_distance_is_zero(self):
        for s in SAMPLE_TEMPLATES:
            assert dcns_engine._influence_distance(s, s) == pytest.approx(0.0, abs=1e-12)

    def test_shared_principles_reduce_distance(self):
        s1 = SAMPLE_TEMPLATES[0]  # CTO — shares "Adaptive" with data_scientist
        s2 = SAMPLE_TEMPLATES[3]  # data_scientist
        s3 = SAMPLE_TEMPLATES[2]  # CFO — no shared principles with CTO
        d_shared = dcns_engine._influence_distance(s1, s2)
        d_no_share = dcns_engine._influence_distance(s1, s3)
        assert d_shared < d_no_share


class TestEdgeCost:
    def test_returns_three_tuple(self):
        cost = dcns_engine._edge_cost(SAMPLE_TEMPLATES[0], SAMPLE_TEMPLATES[1], SAMPLE_RESOURCES)
        assert len(cost) == 3
        assert all(c >= 0 for c in cost)

    def test_resistant_targets_cost_more(self):
        low_resist = SAMPLE_TEMPLATES[3]  # resistance=0.1
        high_resist = SAMPLE_TEMPLATES[2]  # resistance=0.6
        source = SAMPLE_TEMPLATES[0]
        c_low = dcns_engine._edge_cost(source, low_resist, SAMPLE_RESOURCES)
        c_high = dcns_engine._edge_cost(source, high_resist, SAMPLE_RESOURCES)
        assert c_high[0] > c_low[0]  # time cost should be higher


class TestBuildInfluenceGraph:
    def test_adjacency_completeness(self):
        adj = dcns_engine._build_influence_graph(SAMPLE_TEMPLATES, SAMPLE_RESOURCES)
        n = len(SAMPLE_TEMPLATES)
        assert len(adj) == n
        for i in range(n):
            assert len(adj[i]) == n - 1  # fully connected minus self


class TestScaleInfluenceGraph:
    def test_small_count_no_scaling(self):
        import random

        scaled = dcns_engine._scale_influence_graph(SAMPLE_TEMPLATES, 10, random.Random(42))
        assert len(scaled) == len(SAMPLE_TEMPLATES)

    def test_medium_count_adds_intermediaries(self):
        import random

        scaled = dcns_engine._scale_influence_graph(SAMPLE_TEMPLATES, 500, random.Random(42))
        assert len(scaled) > len(SAMPLE_TEMPLATES)

    def test_large_count_adds_frontline(self):
        import random

        scaled = dcns_engine._scale_influence_graph(SAMPLE_TEMPLATES, 1000, random.Random(42))
        assert len(scaled) > len(SAMPLE_TEMPLATES) + 5  # intermediaries + frontline


class TestProjectToGeo:
    def test_within_las_vegas_bounds(self):
        coords = dcns_engine._project_to_geo(SAMPLE_TEMPLATES)
        assert len(coords) == len(SAMPLE_TEMPLATES)
        b = dcns_engine.LAS_VEGAS_BOUNDS
        for lon, lat in coords:
            assert b["lon_min"] <= lon <= b["lon_max"], f"lon {lon} out of bounds"
            assert b["lat_min"] <= lat <= b["lat_max"], f"lat {lat} out of bounds"


class TestConsensusPathway:
    @pytest.fixture
    def network_path(self):
        return os.path.join(
            os.path.dirname(__file__),
            "..",
            "skills",
            "scenario-design",
            "assets",
            "network.json",
        )

    def test_generates_single_pathway(self, network_path):
        result = dcns_engine.generate_consensus_pathway(
            SAMPLE_TEMPLATES, SAMPLE_RESOURCES, "transformation", seed=42, network_path=network_path
        )
        assert "geojson" in result
        assert "pathway" in result
        assert "cost" in result
        assert "consensus_score" in result

    def test_has_valid_geojson(self, network_path):
        result = dcns_engine.generate_consensus_pathway(
            SAMPLE_TEMPLATES, SAMPLE_RESOURCES, "transformation", seed=42, network_path=network_path
        )
        fc = result["geojson"]
        assert fc["type"] == "FeatureCollection"
        lines = [f for f in fc["features"] if f["geometry"]["type"] == "LineString"]
        assert len(lines) >= 1, "missing LineString"
        assert len(lines[0]["geometry"]["coordinates"]) > 10, "Should have many road coordinates"

    def test_station_types_match_frontend(self, network_path):
        result = dcns_engine.generate_consensus_pathway(
            SAMPLE_TEMPLATES, SAMPLE_RESOURCES, "transformation", seed=42, network_path=network_path
        )
        allowed = {"water_station", "medical_tent", "crowd_zone", "station"}
        types = {
            f["properties"]["type"]
            for f in result["geojson"]["features"]
            if "type" in f.get("properties", {}) and f["properties"]["type"] in allowed | {"start", "finish"}
        }
        assert types <= allowed, f"unexpected types: {types - allowed}"

    def test_consensus_score_positive(self, network_path):
        result = dcns_engine.generate_consensus_pathway(
            SAMPLE_TEMPLATES, SAMPLE_RESOURCES, "transformation", seed=42, network_path=network_path
        )
        assert result["consensus_score"] > 0, "zero consensus"

    def test_crisis_has_lower_target(self):
        """Crisis scenarios should achieve consensus more easily (lower bar)."""
        assert dcns_engine._CONSENSUS_TARGETS["crisis"] < dcns_engine._CONSENSUS_TARGETS["transformation"]
        assert dcns_engine._CONSENSUS_TARGETS["transformation"] < dcns_engine._CONSENSUS_TARGETS["merger"]
