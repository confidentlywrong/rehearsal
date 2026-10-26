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

"""Tests for Phase 4 features: coaching engine, A2UI cards, influence network."""

import pytest

from agents.simulator.coaching import generate_micro_feedback, build_coaching_card
from agents.simulator.influence_network import (
    InfluenceEdge,
    InfluenceNetwork,
    build_default_influence_edges,
)
from agents.ai_echo.coaching_prompts import (
    get_coaching_prompt,
    get_contradiction_prompt,
    get_resource_crisis_prompt,
)


# ---------------------------------------------------------------------------
# 4A: Micro-Feedback Tests
# ---------------------------------------------------------------------------


class TestMicroFeedback:
    """Tests for the post-tick micro-feedback generator."""

    def test_nominal_state_shows_progressing(self):
        """No alerts, no crises = progressing normally."""
        result = generate_micro_feedback(
            decision_tags={"principles_applied": [], "principles_neglected": []},
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "bt": {"current": 400, "critical_threshold": 40},
                "tbu": {"current": 60, "critical_threshold": 15},
                "owb": {"current": 50, "critical_threshold": 20},
            },
            alerts=[],
            tick=5,
        )
        assert "progressing normally" in result.lower()

    def test_applied_principle_shown(self):
        result = generate_micro_feedback(
            decision_tags={
                "principles_applied": ["Strategic Foresight & Vision Cascading"],
                "principles_neglected": [],
            },
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "bt": {"current": 400, "critical_threshold": 40},
                "tbu": {"current": 60, "critical_threshold": 15},
                "owb": {"current": 50, "critical_threshold": 20},
            },
            alerts=[],
            tick=5,
        )
        assert "✅" in result
        assert "Strategic Foresight" in result

    def test_critical_resource_flagged(self):
        result = generate_micro_feedback(
            decision_tags={"principles_applied": [], "principles_neglected": []},
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "bt": {"current": 400, "critical_threshold": 40},
                "tbu": {"current": 60, "critical_threshold": 15},
                "owb": {"current": 10, "critical_threshold": 20},
            },
            alerts=[],
            tick=5,
        )
        assert "🔴" in result or "CRITICAL" in result

    def test_warning_resource_flagged(self):
        result = generate_micro_feedback(
            decision_tags={"principles_applied": [], "principles_neglected": []},
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "bt": {"current": 400, "critical_threshold": 40},
                "tbu": {"current": 60, "critical_threshold": 15},
                "owb": {"current": 25, "critical_threshold": 20},
            },
            alerts=[],
            tick=5,
        )
        assert "⚠️" in result or "Watch" in result

    def test_contradiction_flagged(self):
        result = generate_micro_feedback(
            decision_tags={"principles_applied": [], "principles_neglected": []},
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "owb": {"current": 50, "critical_threshold": 20},
            },
            alerts=[],
            tick=5,
            contradiction_alert={"principle": "Ethical Leadership", "commitment": "transparency"},
        )
        assert "Say-Do" in result

    def test_alerts_counted(self):
        result = generate_micro_feedback(
            decision_tags={"principles_applied": [], "principles_neglected": []},
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "owb": {"current": 50, "critical_threshold": 20},
            },
            alerts=[{"type": "threshold_breach"}, {"type": "contention"}],
            tick=5,
        )
        assert "2 alert" in result


# ---------------------------------------------------------------------------
# 4B: A2UI Coaching Card Tests
# ---------------------------------------------------------------------------


class TestCoachingCard:
    """Tests for A2UI coaching card generation."""

    def test_no_card_when_nominal(self):
        card = build_coaching_card(
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "bt": {"current": 400, "critical_threshold": 40},
                "tbu": {"current": 60, "critical_threshold": 15},
                "owb": {"current": 50, "critical_threshold": 20},
            },
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=5,
            max_ticks=30,
        )
        assert card is None

    def test_card_on_contradiction(self):
        card = build_coaching_card(
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "owb": {"current": 50, "critical_threshold": 20},
            },
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=5,
            max_ticks=30,
            contradiction_alert={"principle": "Ethical Leadership", "commitment": "transparency"},
        )
        assert card is not None
        assert "surfaceUpdate" in card
        assert "beginRendering" in card
        assert card["surfaceUpdate"]["surfaceId"] == "coaching"

    def test_card_on_resource_warning(self):
        card = build_coaching_card(
            resource_state={
                "lau": {"current": 80, "critical_threshold": 15},
                "owb": {"current": 25, "critical_threshold": 20},  # Early warning
            },
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=10,
            max_ticks=30,
        )
        assert card is not None
        assert "surfaceUpdate" in card

    def test_card_has_valid_a2ui_structure(self):
        card = build_coaching_card(
            resource_state={
                "owb": {"current": 18, "critical_threshold": 20},
            },
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=5,
            max_ticks=30,
        )
        assert card is not None
        components = card["surfaceUpdate"]["components"]
        # All components must have 'id' and 'component' keys
        for comp in components:
            assert "id" in comp
            assert "component" in comp
        # Root must be referenced in beginRendering
        assert card["beginRendering"]["root"] == "coach-card"

    def test_signature_dedup_suppresses_repeat_emission(self):
        """A card with the same trigger/severity as the previous one is suppressed."""
        first = build_coaching_card(
            resource_state={
                "owb": {"current": 25, "critical_threshold": 20},  # warning band
            },
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=5,
            max_ticks=30,
        )
        assert first is not None
        assert "signature" in first

        # Next tick: same warning band, same trigger -> should suppress.
        second = build_coaching_card(
            resource_state={
                "owb": {"current": 24, "critical_threshold": 20},  # still warning
            },
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=6,
            max_ticks=30,
            last_coaching_signature=first["signature"],
        )
        assert second is None, "duplicate-signature card should not re-emit"

    def test_signature_change_re_emits(self):
        """When severity escalates (warning -> critical) the card re-emits."""
        warning = build_coaching_card(
            resource_state={"owb": {"current": 25, "critical_threshold": 20}},
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=5,
            max_ticks=30,
        )
        assert warning is not None
        critical = build_coaching_card(
            resource_state={"owb": {"current": 15, "critical_threshold": 20}},
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=6,
            max_ticks=30,
            last_coaching_signature=warning["signature"],
        )
        assert critical is not None
        assert critical["signature"] != warning["signature"]

    def test_causal_context_includes_delta(self):
        """When a previous snapshot is supplied, the card embeds a delta line."""
        previous = {"owb": {"current": 30, "critical_threshold": 20}}
        card = build_coaching_card(
            resource_state={"owb": {"current": 18, "critical_threshold": 20}},
            decision_tags={
                "principles_applied": [],
                "principles_neglected": ["Stakeholder Engagement & Inclusive Communication"],
            },
            alerts=[],
            tick=7,
            max_ticks=30,
            previous_resource_state=previous,
        )
        assert card is not None
        cause_components = [c for c in card["surfaceUpdate"]["components"] if c["id"] == "coach-cause"]
        assert cause_components, "expected a coach-cause Text component"
        cause_text = cause_components[0]["component"]["Text"]["text"]["literalString"]
        assert "▼" in cause_text or "▲" in cause_text
        assert "Stakeholder Engagement" in cause_text


# ---------------------------------------------------------------------------
# 4C: Influence Network Tests
# ---------------------------------------------------------------------------


class TestInfluenceNetwork:
    """Tests for inter-Echo influence network."""

    def test_propagation_basic(self):
        """Positive source should pull neutral target positive."""
        network = InfluenceNetwork()
        network.add_edge("cto", "data_scientist", 0.6)

        sentiments = {"cto": 0.8, "data_scientist": 0.0}
        deltas = network.propagate(sentiments)

        # CTO (0.8) should pull data_scientist (0.0) positive
        assert "data_scientist" in deltas
        assert deltas["data_scientist"] > 0

    def test_propagation_negative(self):
        """Hostile source should pull neutral target negative."""
        network = InfluenceNetwork()
        network.add_edge("union_rep", "middle_manager", 0.7)

        sentiments = {"union_rep": -0.6, "middle_manager": 0.2}
        deltas = network.propagate(sentiments)

        assert "middle_manager" in deltas
        assert deltas["middle_manager"] < 0

    def test_bidirectional_convergence(self):
        """Two connected echoes should converge over multiple propagations."""
        network = InfluenceNetwork()
        network.add_edge("cfo", "board", 0.8)
        network.add_edge("board", "cfo", 0.8)

        sentiments = {"cfo": -0.5, "board": 0.5}

        for _ in range(20):
            deltas = network.propagate(sentiments)
            for role, delta in deltas.items():
                sentiments[role] = max(-1.0, min(1.0, sentiments[role] + delta))

        # After 20 iterations, they should converge
        assert abs(sentiments["cfo"] - sentiments["board"]) < 0.2

    def test_decay_reduces_weight(self):
        """Alliance fatigue should reduce edge weight over time."""
        edge = InfluenceEdge("a", "b", 0.5)
        original = edge.weight

        edge.decay(0.01)
        assert edge.weight < original
        assert edge.weight == pytest.approx(0.49, abs=0.001)

    def test_serialization_roundtrip(self):
        network = InfluenceNetwork()
        network.add_edge("cto", "ds", 0.6)
        network.add_edge("cfo", "board", 0.8)

        data = network.to_dict_list()
        restored = InfluenceNetwork.from_dict_list(data)

        assert len(restored) == 2
        assert restored.edges[0].source == "cto"
        assert restored.edges[1].weight == pytest.approx(0.8)

    def test_from_quest_config(self):
        edges = [
            {"source": "cto", "target": "ds", "weight": 0.6},
            {"from": "cfo", "to": "board", "weight": 0.8},  # Alt keys
        ]
        network = InfluenceNetwork.from_quest_config(edges)
        assert len(network) == 2

    def test_default_edges_include_coach(self):
        """Default edges should include executive_coach → all others."""
        configs = [
            {"role": "cto"},
            {"role": "cfo"},
            {"role": "executive_coach"},
        ]
        edges = build_default_influence_edges(configs)
        coach_edges = [e for e in edges if e["source"] == "executive_coach"]
        assert len(coach_edges) == 2  # coach → cto, coach → cfo

    def test_default_edges_match_known_relationships(self):
        """Default edges should include known organizational relationships."""
        configs = [
            {"role": "cto"},
            {"role": "data_scientist_lead"},
            {"role": "cfo"},
            {"role": "board_member"},
            {"role": "executive_coach"},
        ]
        edges = build_default_influence_edges(configs)
        edge_pairs = {(e["source"], e["target"]) for e in edges}
        assert ("cto", "data_scientist_lead") in edge_pairs
        assert ("cfo", "board_member") in edge_pairs

    def test_empty_network_propagation(self):
        """Empty network should produce no deltas."""
        network = InfluenceNetwork()
        deltas = network.propagate({"cto": 0.5})
        assert deltas == {}


# ---------------------------------------------------------------------------
# Coaching Prompts Library Tests
# ---------------------------------------------------------------------------


class TestCoachingPrompts:
    """Tests for the Socratic coaching prompts library."""

    def test_all_principles_have_prompts(self):
        from agents.ai_echo.coaching_prompts import COACHING_PROMPTS
        from agents.simulator.quest_loader import LEADERSHIP_PRINCIPLES

        for principle in LEADERSHIP_PRINCIPLES:
            assert principle in COACHING_PROMPTS, f"Missing prompts for: {principle}"
            assert len(COACHING_PROMPTS[principle]) >= 2

    def test_get_coaching_prompt_deterministic(self):
        p1 = get_coaching_prompt("Strategic Foresight & Vision Cascading", tick=0)
        p2 = get_coaching_prompt("Strategic Foresight & Vision Cascading", tick=0)
        assert p1 == p2  # Same tick = same prompt

    def test_get_coaching_prompt_cycles(self):
        p0 = get_coaching_prompt("Strategic Foresight & Vision Cascading", tick=0)
        p1 = get_coaching_prompt("Strategic Foresight & Vision Cascading", tick=1)
        assert p0 != p1  # Different ticks = different prompts

    def test_contradiction_prompt_substitution(self):
        prompt = get_contradiction_prompt("transparency", tick=0)
        assert "transparency" in prompt

    def test_resource_crisis_prompt_exists(self):
        for resource in ["lau", "bt", "tbu", "owb"]:
            prompt = get_resource_crisis_prompt(resource, tick=0)
            assert len(prompt) > 10
