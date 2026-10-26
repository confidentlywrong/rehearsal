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

"""Tests for the quest loader, ECSI, truth engine, and echo initialization.

All tests are offline — no GCP credentials or LLM calls required.
"""

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Quest Loader Tests
# ---------------------------------------------------------------------------


class TestQuestLoader:
    """Validate quest YAML loading and Pydantic schema validation."""

    def test_load_ai_disruptor_quest(self):
        """Load the canonical quest and verify all fields parsed correctly."""
        from agents.simulator.quest_loader import load_quest_from_yaml

        quest = load_quest_from_yaml(
            "ai_powered_disruptor",
            quest_dir=Path(__file__).resolve().parents[2] / "configs" / "quests",
        )

        assert quest.id == "ai_powered_disruptor"
        assert quest.max_ticks == 30
        assert quest.difficulty == "intermediate"
        assert "LAU" in quest.resources
        assert "BT" in quest.resources
        assert "TBU" in quest.resources
        assert "OWB" in quest.resources
        assert quest.resources["LAU"].initial == 100
        assert quest.resources["OWB"].regen_per_tick == -1
        assert len(quest.echoes) >= 5
        assert len(quest.black_swan_events) >= 3
        assert len(quest.principles) == 7

    def test_quest_echo_tiers(self):
        """Verify echo tier distribution across all tiers."""
        from agents.simulator.quest_loader import load_quest_from_yaml

        quest = load_quest_from_yaml(
            "ai_powered_disruptor",
            quest_dir=Path(__file__).resolve().parents[2] / "configs" / "quests",
        )

        tiers = {e.tier for e in quest.echoes}
        assert 1 in tiers, "Must have Tier 1 (Autonomous) echoes"
        assert 2 in tiers, "Must have Tier 2 (Reactive) echoes"
        assert 3 in tiers, "Must have Tier 3 (Deterministic) echoes"

    def test_quest_black_swan_triggers(self):
        """Verify both tick-based and condition-based triggers are present."""
        from agents.simulator.quest_loader import load_quest_from_yaml

        quest = load_quest_from_yaml(
            "ai_powered_disruptor",
            quest_dir=Path(__file__).resolve().parents[2] / "configs" / "quests",
        )

        trigger_types = {e.trigger.type for e in quest.black_swan_events}
        assert "tick" in trigger_types
        assert "condition" in trigger_types

    def test_quest_validation_missing_resource(self):
        """Schema rejects quest YAML missing required resources."""
        from agents.simulator.quest_loader import QuestConfig

        with pytest.raises(Exception):
            QuestConfig(
                id="test",
                name="Test",
                resources={"LAU": {"initial": 100, "regen_per_tick": 0}},
                echoes=[
                    {
                        "role": "test",
                        "persona": "Test",
                        "affinity": [0.5] * 7,
                    }
                ],
            )

    def test_quest_validation_bad_affinity_length(self):
        """Schema rejects echo with wrong affinity vector length."""
        from agents.simulator.quest_loader import EchoConfig

        with pytest.raises(Exception):
            EchoConfig(
                role="test",
                persona="Test",
                affinity=[0.5, 0.5],  # Too short
            )

    def test_inject_quest_into_state(self):
        """Verify state injection populates all required keys."""
        from agents.simulator.quest_loader import inject_quest_into_state, load_quest_from_yaml

        quest = load_quest_from_yaml(
            "ai_powered_disruptor",
            quest_dir=Path(__file__).resolve().parents[2] / "configs" / "quests",
        )

        state = {}
        inject_quest_into_state(state, quest)

        assert state["quest_id"] == "ai_powered_disruptor"
        assert state["max_ticks"] == 30
        assert state["current_tick"] == 0
        assert "lau" in state["resources"]
        assert "bt" in state["resources"]
        assert "tbu" in state["resources"]
        assert "owb" in state["resources"]
        assert state["resources"]["lau"]["current"] == 100
        assert len(state["echo_configs"]) == len(quest.echoes)
        assert len(state["black_swan_events"]) >= 3
        assert state["tick_snapshots"] == []
        assert state["declared_commitments"] == []
        assert state["contradiction_log"] == []
        assert len(state["principle_scores"]) == 7


# ---------------------------------------------------------------------------
# Echo Initialization Tests
# ---------------------------------------------------------------------------


class TestEchoInitialization:
    """Validate AI Echo profile generation from quest config."""

    def test_initialize_echo_basic(self):
        """Initialize an Echo and verify all state keys are set."""
        from agents.ai_echo.initialization import initialize_echo

        state = {}
        config = {
            "role": "cfo",
            "tier": 2,
            "persona": "Conservative CFO focused on ROI",
            "domain_principles": ["Resource Optimization & Strategic Allocation"],
            "affinity": [0.3, 0.5, 0.4, 0.6, 0.2, 0.3, 0.9],
            "initial_sentiment": 0.4,
            "initial_resistance": 0.5,
            "initial_influence": 0.8,
        }

        initialize_echo(state, "session-123", config)

        assert state["role"] == "cfo"
        assert state["tier"] == 2
        assert state["defected"] is False
        assert state["aligned"] is False
        assert state["echo_status"] == "active"
        assert len(state["principle_affinity"]) == 7
        assert state["stance"] in ("supportive", "neutral", "resistant", "hostile")

        # Verify jitter is applied (values should be close but not exactly equal)
        assert abs(state["sentiment"] - 0.4) < 0.2
        assert abs(state["resistance"] - 0.5) < 0.2

    def test_initialize_echo_deterministic(self):
        """Same session_id + role produces identical profiles."""
        from agents.ai_echo.initialization import initialize_echo

        config = {
            "role": "cto",
            "affinity": [0.9, 0.8, 0.3, 0.4, 0.3, 0.5, 0.5],
            "initial_sentiment": 0.8,
            "initial_resistance": 0.1,
            "initial_influence": 0.7,
            "persona": "Visionary CTO",
        }

        state1 = {}
        state2 = {}
        initialize_echo(state1, "session-abc", config)
        initialize_echo(state2, "session-abc", config)

        assert state1["sentiment"] == state2["sentiment"]
        assert state1["resistance"] == state2["resistance"]
        assert state1["principle_affinity"] == state2["principle_affinity"]

    def test_different_sessions_produce_different_profiles(self):
        """Different session_ids produce different Echo profiles."""
        from agents.ai_echo.initialization import initialize_echo

        config = {
            "role": "cfo",
            "affinity": [0.5] * 7,
            "initial_sentiment": 0.5,
            "initial_resistance": 0.3,
            "persona": "Test CFO",
        }

        state1 = {}
        state2 = {}
        initialize_echo(state1, "session-111", config)
        initialize_echo(state2, "session-222", config)

        # At least some values should differ due to different seeds
        differs = (
            state1["sentiment"] != state2["sentiment"]
            or state1["resistance"] != state2["resistance"]
            or state1["principle_affinity"] != state2["principle_affinity"]
        )
        assert differs, "Different sessions should produce different profiles"

    def test_sentiment_to_stance_mapping(self):
        """Verify stance labels map correctly from sentiment values."""
        from agents.ai_echo.initialization import sentiment_to_stance

        assert sentiment_to_stance(-0.8) == "hostile"
        assert sentiment_to_stance(-0.2) == "resistant"
        assert sentiment_to_stance(0.1) == "neutral"
        assert sentiment_to_stance(0.5) == "supportive"

    def test_hidden_agenda_fields(self):
        """Verify hidden agenda fields are set from config."""
        from agents.ai_echo.initialization import initialize_echo

        config = {
            "role": "board_member",
            "affinity": [0.5] * 7,
            "has_hidden_agenda": True,
            "agenda_trigger_tick": 20,
            "initial_sentiment": 0.6,
            "persona": "Board member",
        }

        state = {}
        initialize_echo(state, "session-x", config)

        assert state["has_hidden_agenda"] is True
        assert state["agenda_trigger_tick"] == 20
        assert state["agenda_revealed"] is False


# ---------------------------------------------------------------------------
# ECSI Tests
# ---------------------------------------------------------------------------


class TestECSI:
    """Validate the Emergent Critical Section Identifier."""

    def test_threshold_breach_warning(self):
        """Detect warning when resource crosses critical threshold."""
        from agents.simulator.ecsi import detect_contention

        resources = {
            "lau": {"current": 18, "critical_threshold": 20},
            "bt": {"current": 200, "critical_threshold": 50},
        }

        alerts = detect_contention(
            tick_snapshot={"tick": 5},
            quest_config={},
            resource_state=resources,
        )

        assert len(alerts) == 1
        assert alerts[0].alert_type == "threshold"
        assert alerts[0].severity == "warning"
        assert "LAU" in alerts[0].affected_resources

    def test_threshold_breach_critical(self):
        """Detect critical alert when resource drops well below threshold."""
        from agents.simulator.ecsi import detect_contention

        resources = {
            "owb": {"current": 5, "critical_threshold": 25},
        }

        alerts = detect_contention(
            tick_snapshot={"tick": 10},
            quest_config={},
            resource_state=resources,
        )

        assert len(alerts) == 1
        assert alerts[0].severity == "critical"

    def test_sentiment_cascade(self):
        """Detect when 3+ Echoes are hostile."""
        from agents.simulator.ecsi import detect_contention

        sentiments = {
            "echo-1": {"role": "cfo", "stance": "hostile"},
            "echo-2": {"role": "union_rep", "stance": "hostile"},
            "echo-3": {"role": "hr_director", "stance": "hostile"},
            "echo-4": {"role": "cto", "stance": "supportive"},
        }

        alerts = detect_contention(
            tick_snapshot={"tick": 15, "echo_sentiments": sentiments},
            quest_config={},
            resource_state={},
        )

        cascade_alerts = [a for a in alerts if a.alert_type == "cascade"]
        assert len(cascade_alerts) == 1
        assert cascade_alerts[0].severity == "critical"

    def test_no_cascade_with_two_hostile(self):
        """No cascade alert with fewer than 3 hostile Echoes."""
        from agents.simulator.ecsi import detect_contention

        sentiments = {
            "echo-1": {"role": "cfo", "stance": "hostile"},
            "echo-2": {"role": "union_rep", "stance": "hostile"},
            "echo-3": {"role": "cto", "stance": "supportive"},
        }

        alerts = detect_contention(
            tick_snapshot={"tick": 5, "echo_sentiments": sentiments},
            quest_config={},
            resource_state={},
        )

        cascade_alerts = [a for a in alerts if a.alert_type == "cascade"]
        assert len(cascade_alerts) == 0

    def test_resource_contention(self):
        """Detect when 2+ Echoes claim the same resource."""
        from agents.simulator.ecsi import detect_contention

        echo_actions = [
            {"role": "cto", "resource_claims": ["tbu"]},
            {"role": "hr_director", "resource_claims": ["tbu"]},
        ]

        alerts = detect_contention(
            tick_snapshot={"tick": 8, "echo_actions": echo_actions},
            quest_config={},
            resource_state={},
        )

        contention_alerts = [a for a in alerts if a.alert_type == "contention"]
        assert len(contention_alerts) == 1
        assert "TBU" in contention_alerts[0].affected_resources

    def test_hidden_agenda_trigger(self):
        """Detect when hidden agenda trigger tick is reached."""
        from agents.simulator.ecsi import detect_contention

        state = {"revealed_agendas": []}
        echo_configs = [
            {"role": "board_member", "has_hidden_agenda": True, "agenda_trigger_tick": 20},
        ]

        alerts = detect_contention(
            tick_snapshot={"tick": 20},
            quest_config={"echo_configs": echo_configs},
            resource_state={},
            state=state,
        )

        agenda_alerts = [a for a in alerts if a.alert_type == "agenda"]
        assert len(agenda_alerts) == 1
        assert "board_member" in state["revealed_agendas"]

    def test_hidden_agenda_fires_once(self):
        """Hidden agenda alert doesn't fire twice for the same Echo."""
        from agents.simulator.ecsi import detect_contention

        state = {"revealed_agendas": ["board_member"]}
        echo_configs = [
            {"role": "board_member", "has_hidden_agenda": True, "agenda_trigger_tick": 20},
        ]

        alerts = detect_contention(
            tick_snapshot={"tick": 21},
            quest_config={"echo_configs": echo_configs},
            resource_state={},
            state=state,
        )

        agenda_alerts = [a for a in alerts if a.alert_type == "agenda"]
        assert len(agenda_alerts) == 0

    def test_empty_state_no_alerts(self):
        """No alerts generated from empty tick data."""
        from agents.simulator.ecsi import detect_contention

        alerts = detect_contention(
            tick_snapshot={"tick": 0},
            quest_config={},
            resource_state={},
        )

        assert alerts == []


# ---------------------------------------------------------------------------
# Truth Engine Tests
# ---------------------------------------------------------------------------


class TestTruthEngine:
    """Validate the Contradiction Detector."""

    def test_extract_commitment_keyword(self):
        """Extract commitment from participant text."""
        from agents.simulator.truth_engine import extract_commitments

        principles = [
            "Strategic Foresight & Vision Cascading",
            "Adaptive Decision-Making & Agility",
            "Stakeholder Engagement & Inclusive Communication",
            "Ethical Leadership & Governance",
            "Empowerment & Cultivating Distributed Leadership",
            "Organizational Resilience & Change Agility",
            "Resource Optimization & Strategic Allocation",
        ]

        commitments = extract_commitments(
            "I commit to ethical AI governance and responsible innovation",
            current_tick=3,
            principles=principles,
        )

        assert len(commitments) >= 1
        # Should map to principle index 3 (Ethical Leadership)
        assert any(c.principle_index == 3 for c in commitments)

    def test_extract_commitment_priority(self):
        """Extract commitment from 'my priority is' pattern."""
        from agents.simulator.truth_engine import extract_commitments

        principles = ["P0", "P1", "P2", "P3", "P4", "P5", "P6"]

        commitments = extract_commitments(
            "My priority is empowering the team and building autonomy",
            current_tick=5,
            principles=principles,
        )

        assert len(commitments) >= 1
        # Should map to principle index 4 (Empowerment)
        assert any(c.principle_index == 4 for c in commitments)

    def test_no_commitment_in_plain_text(self):
        """No false positives from non-commitment text."""
        from agents.simulator.truth_engine import extract_commitments

        commitments = extract_commitments(
            "Let's proceed with the budget review for Q3.",
            current_tick=1,
            principles=["P0"] * 7,
        )

        assert len(commitments) == 0

    def test_detect_contradiction(self):
        """Detect contradiction when commitment is neglected."""
        from agents.simulator.truth_engine import detect_contradictions

        principles = [
            "Strategic Foresight & Vision Cascading",
            "Adaptive Decision-Making & Agility",
            "Stakeholder Engagement & Inclusive Communication",
            "Ethical Leadership & Governance",
            "Empowerment & Cultivating Distributed Leadership",
            "Organizational Resilience & Change Agility",
            "Resource Optimization & Strategic Allocation",
        ]

        commitments = [
            {
                "text": "ethical governance",
                "principle_index": 3,
                "principle_name": "Ethical Leadership & Governance",
                "tick_declared": 3,
                "active": True,
            }
        ]

        decision_tags = {
            "principles_applied": ["Resource Optimization & Strategic Allocation"],
            "principles_neglected": ["Ethical Leadership & Governance"],
            "raw_decision": "Skip the ethics review to meet the deadline",
        }

        contradictions = detect_contradictions(
            commitments=commitments,
            decision_tags=decision_tags,
            current_tick=5,
            principles=principles,
        )

        assert len(contradictions) == 1
        assert contradictions[0].violated_principle == "Ethical Leadership & Governance"
        assert contradictions[0].severity == "critical"  # within 5 ticks

    def test_no_contradiction_when_aligned(self):
        """No contradiction when committed principle is applied."""
        from agents.simulator.truth_engine import detect_contradictions

        commitments = [
            {
                "text": "ethical governance",
                "principle_index": 3,
                "principle_name": "Ethical Leadership & Governance",
                "tick_declared": 3,
                "active": True,
            }
        ]

        decision_tags = {
            "principles_applied": ["Ethical Leadership & Governance"],
            "principles_neglected": [],
        }

        contradictions = detect_contradictions(
            commitments=commitments,
            decision_tags=decision_tags,
            current_tick=5,
            principles=["P"] * 7,
        )

        assert len(contradictions) == 0

    def test_severity_based_on_age(self):
        """Recent commitments produce critical severity, older ones warning."""
        from agents.simulator.truth_engine import _compute_severity

        assert _compute_severity(2) == "critical"
        assert _compute_severity(5) == "critical"
        assert _compute_severity(6) == "warning"
        assert _compute_severity(15) == "warning"

    def test_find_best_responder(self):
        """Find the Echo with highest affinity to violated principle."""
        from agents.simulator.truth_engine import Contradiction, find_best_responder

        contradictions = [
            Contradiction(
                commitment_text="ethical governance",
                violated_principle="Ethical Leadership & Governance",
                violated_principle_index=3,
                evidence="test",
                tick_detected=5,
                tick_declared=3,
            )
        ]

        echo_configs = [
            {"role": "cfo", "affinity": [0.3, 0.5, 0.4, 0.2, 0.2, 0.3, 0.9]},
            {"role": "head_ai_ethics", "affinity": [0.5, 0.4, 0.7, 0.95, 0.6, 0.5, 0.3]},
            {"role": "cto", "affinity": [0.9, 0.8, 0.3, 0.1, 0.3, 0.5, 0.5]},
        ]

        best = find_best_responder(contradictions, echo_configs)
        assert best == "head_ai_ethics"

    def test_state_accumulation(self):
        """Verify commitments and contradictions accumulate in state."""
        from agents.simulator.truth_engine import (
            Commitment,
            Contradiction,
            update_state_with_commitments,
            update_state_with_contradictions,
        )

        state = {"declared_commitments": [], "contradiction_log": []}

        # Add commitments
        update_state_with_commitments(
            state,
            [
                Commitment(
                    text="ethics first", principle_index=3, principle_name="Ethical Leadership", tick_declared=1
                ),
            ],
        )
        assert len(state["declared_commitments"]) == 1

        # Add contradictions
        update_state_with_contradictions(
            state,
            [
                Contradiction(
                    commitment_text="ethics first",
                    violated_principle="Ethical Leadership",
                    violated_principle_index=3,
                    evidence="skipped review",
                    tick_detected=5,
                    tick_declared=1,
                ),
            ],
        )
        assert len(state["contradiction_log"]) == 1
        assert state["contradiction_log"][0]["violated_principle"] == "Ethical Leadership"
