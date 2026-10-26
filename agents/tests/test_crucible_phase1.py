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

"""Tests for Phase 1: Core Agent Transformation.

Covers process_decision_round, decision classifier, autopilot callback,
and reactive callback logic. All tests are offline — no LLM calls.
"""


# ---------------------------------------------------------------------------
# process_decision_round Tests
# ---------------------------------------------------------------------------


class TestProcessDecisionRound:
    """Validate the core per-tick Echo state update logic."""

    def _make_state(self, **overrides):
        """Create a minimal Echo state dict for testing."""
        state = {
            "role": "cfo",
            "tier": 2,
            "persona": "Conservative CFO",
            "domain_principles": ["Resource Optimization & Strategic Allocation"],
            "sentiment": 0.4,
            "resistance": 0.5,
            "influence": 0.8,
            "urgency": 0.2,
            "defected": False,
            "aligned": False,
            "stance": "supportive",
            "has_hidden_agenda": False,
            "agenda_trigger_tick": None,
            "agenda_revealed": False,
            "communication_receptivity": 0.7,
            "influence_susceptibility": 0.3,
            "principle_affinity": [0.3, 0.5, 0.4, 0.6, 0.2, 0.3, 0.9],
            "echo_status": "active",
        }
        state.update(overrides)
        return state

    def test_sentiment_increases_on_domain_applied(self):
        """Sentiment increases when domain principle is applied."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(sentiment=0.4)
        result = process_decision_round(
            state=state,
            tick=5,
            decision_tags={
                "principles_applied": ["Resource Optimization & Strategic Allocation"],
                "principles_neglected": [],
            },
            inner_thought="Good budget decision",
        )

        assert state["sentiment"] > 0.4
        assert result["stance"] in ("supportive", "neutral", "resistant", "hostile")

    def test_sentiment_decreases_on_domain_neglected(self):
        """Sentiment decreases when domain principle is neglected."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(sentiment=0.4)
        process_decision_round(
            state=state,
            tick=5,
            decision_tags={
                "principles_applied": [],
                "principles_neglected": ["Resource Optimization & Strategic Allocation"],
            },
        )

        assert state["sentiment"] < 0.4

    def test_contradiction_alert_drops_sentiment(self):
        """Contradiction alert causes additional sentiment drop."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(sentiment=0.4)
        process_decision_round(
            state=state,
            tick=5,
            contradiction_alert={"text": "You said X but did Y"},
        )

        assert state["sentiment"] < 0.4

    def test_affinity_drifts_on_applied_principle(self):
        """Principle affinity increases when principle is applied."""
        from agents.ai_echo.process_decision_round import process_decision_round

        initial_affinity = [0.3, 0.5, 0.4, 0.6, 0.2, 0.3, 0.9]
        state = self._make_state(principle_affinity=list(initial_affinity))

        process_decision_round(
            state=state,
            tick=5,
            decision_tags={
                "principles_applied": ["Resource Optimization & Strategic Allocation"],
                "principles_neglected": ["Ethical Leadership & Governance"],
            },
        )

        # Principle 6 (applied) should increase
        assert state["principle_affinity"][6] > initial_affinity[6]
        # Principle 3 (neglected) should decrease
        assert state["principle_affinity"][3] < initial_affinity[3]

    def test_defection_on_extreme_negative(self):
        """Echo defects when sentiment drops below -0.8 and resistance is high."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(sentiment=-0.85, resistance=0.85)
        process_decision_round(state=state, tick=10)

        assert state["defected"] is True
        assert state["echo_status"] == "defected"

    def test_alignment_on_extreme_positive(self):
        """Echo aligns when sentiment is very high and resistance is low."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(sentiment=0.85, resistance=0.05)
        process_decision_round(
            state=state,
            tick=10,
            decision_tags={
                "principles_applied": ["Resource Optimization & Strategic Allocation"],
                "principles_neglected": [],
            },
        )

        assert state["aligned"] is True

    def test_terminal_state_returns_early(self):
        """Defected Echo returns terminal result without updating state."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(defected=True, sentiment=-0.9)
        result = process_decision_round(state=state, tick=15)

        assert result["echo_status"] == "defected"
        assert result["defected"] is True

    def test_hidden_agenda_revelation(self):
        """Hidden agenda activates at trigger tick."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(
            has_hidden_agenda=True,
            agenda_trigger_tick=20,
            agenda_revealed=False,
            sentiment=0.6,
        )

        process_decision_round(state=state, tick=20)

        assert state["agenda_revealed"] is True
        assert state["sentiment"] < 0.6  # Agenda revelation drops sentiment

    def test_autonomous_action_on_high_resistance(self):
        """High resistance triggers escalation autonomous action."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(resistance=0.8)
        result = process_decision_round(state=state, tick=5)

        assert result["autonomous_action"] is not None
        assert "escalate" in result["autonomous_action"].lower()

    def test_resource_claims_on_urgency(self):
        """High urgency + low OWB generates resource claims."""
        from agents.ai_echo.process_decision_round import process_decision_round

        state = self._make_state(urgency=0.8)
        result = process_decision_round(
            state=state,
            tick=5,
            resource_state={"owb": {"current": 20}},
        )

        assert len(result["resource_claims"]) > 0


# ---------------------------------------------------------------------------
# Decision Classifier Tests
# ---------------------------------------------------------------------------


class TestDecisionClassifier:
    """Validate the heuristic decision classifier."""

    def test_classify_budget_decision(self):
        """Budget-related decision maps to Resource Optimization."""
        from agents.simulator.decision_classifier import classify_decision_heuristic

        result = classify_decision_heuristic("Allocate 40% of budget to AI R&D and invest in cloud infrastructure")

        assert "Resource Optimization & Strategic Allocation" in result["principles_applied"]
        assert result["confidence"] > 0.0
        assert result["classification_method"] == "heuristic"

    def test_classify_ethics_decision(self):
        """Ethics-related decision maps to Ethical Leadership."""
        from agents.simulator.decision_classifier import classify_decision_heuristic

        result = classify_decision_heuristic(
            "Conduct a thorough ethical review and bias audit before deploying the AI system"
        )

        assert "Ethical Leadership & Governance" in result["principles_applied"]

    def test_classify_stakeholder_decision(self):
        """Stakeholder engagement maps correctly."""
        from agents.simulator.decision_classifier import classify_decision_heuristic

        result = classify_decision_heuristic(
            "Host a town hall meeting to engage all stakeholders and listen to feedback"
        )

        assert "Stakeholder Engagement & Inclusive Communication" in result["principles_applied"]

    def test_classify_empty_input(self):
        """Empty input returns zero-confidence classification."""
        from agents.simulator.decision_classifier import classify_decision_heuristic

        result = classify_decision_heuristic("")

        assert result["confidence"] == 0.0
        assert result["principles_applied"] == []

    def test_resource_impact_suggestion(self):
        """Budget decisions suggest BT impact."""
        from agents.simulator.decision_classifier import classify_decision_heuristic

        result = classify_decision_heuristic("Invest $2M in hiring new AI talent")

        assert "bt" in result["resource_impact_suggestion"] or "tbu" in result["resource_impact_suggestion"]

    def test_apply_resource_impacts(self):
        """Resource impacts modify state correctly."""
        from agents.simulator.decision_classifier import apply_resource_impacts

        resources = {
            "lau": {"current": 90, "initial": 100, "regen_per_tick": 5, "critical_threshold": 20},
            "bt": {"current": 500, "initial": 500, "regen_per_tick": 0, "critical_threshold": 50},
        }

        apply_resource_impacts(resources, {"bt": -30})

        assert resources["bt"]["current"] == 470  # 500 + 0 regen - 30 impact
        assert resources["lau"]["current"] == 95  # 90 + 5 regen (clamped at initial=100)

    def test_apply_resource_clamps_to_zero(self):
        """Resources don't go below zero."""
        from agents.simulator.decision_classifier import apply_resource_impacts

        resources = {
            "bt": {"current": 10, "initial": 500, "regen_per_tick": 0, "critical_threshold": 50},
        }

        apply_resource_impacts(resources, {"bt": -50})

        assert resources["bt"]["current"] == 0

    def test_black_swan_tick_trigger(self):
        """Black Swan fires on correct tick."""
        from agents.simulator.decision_classifier import apply_black_swan_events

        resources = {
            "owb": {"current": 70, "initial": 70},
        }

        events = [
            {
                "trigger": {"type": "tick", "tick": 15},
                "event": "Competitor launches rival product",
                "resource_impact": {"owb": -10},
            }
        ]

        triggered = apply_black_swan_events(
            tick=15,
            resource_state=resources,
            black_swan_events=events,
            triggered_history=[],
        )

        assert len(triggered) == 1
        assert resources["owb"]["current"] == 60

    def test_black_swan_condition_trigger(self):
        """Black Swan fires on condition."""
        from agents.simulator.decision_classifier import apply_black_swan_events

        resources = {
            "owb": {"current": 25, "initial": 70},
            "tbu": {"current": 80, "initial": 80},
        }

        events = [
            {
                "trigger": {"type": "condition", "when": "OWB < 30"},
                "event": "Key talent exodus",
                "resource_impact": {"tbu": -20},
            }
        ]

        triggered = apply_black_swan_events(
            tick=10,
            resource_state=resources,
            black_swan_events=events,
            triggered_history=[],
        )

        assert len(triggered) == 1
        assert resources["tbu"]["current"] == 60

    def test_black_swan_no_duplicate(self):
        """Black Swan doesn't fire twice."""
        from agents.simulator.decision_classifier import apply_black_swan_events

        resources = {"owb": {"current": 25, "initial": 70}}

        events = [
            {
                "trigger": {"type": "condition", "when": "OWB < 30"},
                "event": "Key talent exodus",
                "resource_impact": {},
            }
        ]

        history = ["Key talent exodus"]
        triggered = apply_black_swan_events(
            tick=10,
            resource_state=resources,
            black_swan_events=events,
            triggered_history=history,
        )

        assert len(triggered) == 0


# ---------------------------------------------------------------------------
# Autopilot Callback Tests
# ---------------------------------------------------------------------------


class TestAutopilotCallback:
    """Validate the Tier 3 deterministic callback."""

    def test_pick_inner_thought_by_stance(self):
        """Inner thought selection varies by stance."""
        from agents.ai_echo_autopilot.autopilot import _pick_inner_thought

        hostile_state = {"stance": "hostile", "defected": False, "aligned": False}
        supportive_state = {"stance": "supportive", "defected": False, "aligned": False}

        hostile_thought = _pick_inner_thought(hostile_state, 0)
        supportive_thought = _pick_inner_thought(supportive_state, 0)

        assert hostile_thought != supportive_thought

    def test_defected_thought(self):
        """Defected Echo always returns defection thought."""
        from agents.ai_echo_autopilot.autopilot import _pick_inner_thought

        state = {"defected": True, "stance": "hostile"}
        thought = _pick_inner_thought(state, 5)

        assert "no longer support" in thought.lower()

    def test_aligned_thought(self):
        """Aligned Echo always returns alignment thought."""
        from agents.ai_echo_autopilot.autopilot import _pick_inner_thought

        state = {"aligned": True, "defected": False, "stance": "supportive"}
        thought = _pick_inner_thought(state, 5)

        assert "committed" in thought.lower()


# ---------------------------------------------------------------------------
# Reactive Callback Logic Tests
# ---------------------------------------------------------------------------


class TestReactiveCallback:
    """Validate the Tier 2 selective LLM activation logic."""

    def test_domain_relevance_detection(self):
        """Reactive callback detects domain-relevant decisions."""
        # Test the core logic without ADK wiring
        domain_principles = {"Resource Optimization & Strategic Allocation"}
        decision_tags = {
            "principles_applied": ["Resource Optimization & Strategic Allocation"],
            "principles_neglected": [],
        }

        relevant = domain_principles & (
            set(decision_tags.get("principles_applied", [])) | set(decision_tags.get("principles_neglected", []))
        )

        assert len(relevant) > 0

    def test_non_domain_is_deterministic(self):
        """Non-domain decisions should use deterministic path."""
        domain_principles = {"Resource Optimization & Strategic Allocation"}
        decision_tags = {
            "principles_applied": ["Ethical Leadership & Governance"],
            "principles_neglected": ["Adaptive Decision-Making & Agility"],
        }

        relevant = domain_principles & (
            set(decision_tags.get("principles_applied", [])) | set(decision_tags.get("principles_neglected", []))
        )

        assert len(relevant) == 0  # Should use deterministic path
