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

"""Regression tests for simulator, quest loader, ECSI, ARI, longitudinal, and AI Echo bug fixes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.ai_echo.agent import (
    ECHO_STATIC_INSTRUCTION,
    _echo_before_agent_callback,
    process_decision_round_tool,
)
from agents.ai_echo.coaching_prompts import get_coaching_prompt
from agents.ai_echo.initialization import _echo_seed
from agents.ai_echo.process_decision_round import process_decision_round
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
from agents.ai_echo_reactive.callback import reactive_echo_callback
from agents.simulator.longitudinal import (
    SimulationRunRecord,
    recommend_next_quest,
    replay_from_tick,
)
from agents.simulator.quest_loader import inject_quest_into_state, load_quest_from_yaml
from google.genai import types

_SIM_SKILLS = pathlib.Path(__file__).parents[1] / "skills"


def _load_skill_module(module_name: str, skill_subdir: str):
    path = _SIM_SKILLS / skill_subdir / "tools.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pre_race_tools = _load_skill_module("sim_audit_pre_race", "preparing-the-race")
tick_tools = _load_skill_module("sim_audit_ticks", "advancing-race-ticks")
post_race_tools = _load_skill_module("sim_audit_post_race", "completing-the-race")


def _make_ctx(state: dict | None = None, session_id: str = "sim-audit-1") -> MagicMock:
    ctx = MagicMock()
    ctx.state = state if state is not None else {}
    ctx.session = MagicMock()
    ctx.session.id = session_id
    ctx.invocation_id = "inv-audit"
    ctx.agent_name = "simulator"
    ctx.actions = MagicMock()
    ctx.actions.escalate = False
    return ctx


class TestQuestLoaderByQuestId:
    """load_quest_from_yaml must resolve quests whose quest.id differs from filename."""

    def test_loads_budget_cuts_by_canonical_id(self):
        quest = load_quest_from_yaml("budget_cuts_2027")
        assert quest.id == "budget_cuts_2027"
        assert len(quest.echoes) >= 1

    def test_loads_hypergrowth_by_canonical_id(self):
        quest = load_quest_from_yaml("scaling_hypergrowth_2028")
        assert quest.id == "scaling_hypergrowth_2028"
        assert len(quest.echoes) >= 1

    def test_unknown_quest_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="nonexistent_quest_9999"):
            load_quest_from_yaml("nonexistent_quest_9999")


class TestEchoSessionTrackingInPreRace:
    """spawn_runners, start_race_collector, and fire_start_gun must track Echo session IDs."""

    @pytest.mark.asyncio
    async def test_spawn_collector_and_start_gun_include_echo_sessions(self):
        state = {
            "simulation_ready": True,
            "simulation_id": "sim-echo-track",
            "runner_count": 2,
            "max_ticks": 10,
            "simulation_config": {"runner_type": "leadership_runner", "runner_count": 2},
            "echo_configs": [
                {"role": "cfo", "tier": 1, "persona": "CFO"},
                {"role": "cto", "tier": 2, "persona": "CTO"},
            ],
        }
        ctx = _make_ctx(state=state)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "sessions": [
                    {"sessionId": "r-1", "agentType": "leadership_runner"},
                    {"sessionId": "r-2", "agentType": "leadership_runner"},
                    {"sessionId": "e-1", "agentType": "ai_echo"},
                    {"sessionId": "e-2", "agentType": "ai_echo_reactive"},
                ]
            }
        )
        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=False)

        mock_http_session = MagicMock()
        mock_http_session.post = MagicMock(return_value=mock_post_cm)
        mock_client_cm = MagicMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_http_session)
        mock_client_cm.__aexit__ = AsyncMock(return_value=False)

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock()

        with (
            patch("aiohttp.ClientSession", return_value=mock_client_cm),
            patch.object(pre_race_tools.oidc_auth, "get_id_token", return_value=None),
            patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis),
        ):
            spawn_res = await pre_race_tools.spawn_runners(count=2, tool_context=ctx)

        assert spawn_res["status"] == "success"
        assert ctx.state["runner_session_ids"] == ["r-1", "r-2"]
        assert ctx.state["echo_session_ids"] == ["e-1", "e-2"]
        assert ctx.state["runner_count"] == 2

        with patch("agents.simulator.collector.RaceCollector.start", new_callable=AsyncMock) as mock_start:
            coll_res = await pre_race_tools.start_race_collector(tool_context=ctx)
            assert coll_res["status"] == "success"
            mock_start.assert_awaited_once_with(
                session_id="sim-audit-1",
                runner_session_ids={"r-1", "r-2", "e-1", "e-2"},
                skip_pubsub=False,
            )

        with (
            patch("agents.simulator.collector.RaceCollector.is_running", return_value=True),
            patch.object(pre_race_tools, "wait_for_runners_ready", new_callable=AsyncMock) as mock_wait,
            patch.object(pre_race_tools, "publish_to_runners", new_callable=AsyncMock),
        ):
            gun_res = await pre_race_tools.fire_start_gun(tool_context=ctx)
            assert gun_res["status"] == "success"
            waited_ids = mock_wait.call_args[0][0]
            assert set(waited_ids) == {"r-1", "r-2", "e-1", "e-2"}


class TestAdvanceTickEchoAndSnapshotIntegrity:
    """advance_tick must wire ECSI cascade/contention, _echo_states, deep-copied resources, and 0..10 friction."""

    @pytest.mark.asyncio
    async def test_ecsi_cascade_contention_friction_and_narrative(self):
        quest = load_quest_from_yaml("ai_powered_disruptor")
        state: dict = {
            "simulation_config": {"tick_interval_seconds": 0, "total_race_hours": 6.0},
            "tick_snapshots": [],
            "runner_session_ids": ["r-1"],
        }
        inject_quest_into_state(state, quest)
        state["current_tick"] = 1
        state["max_ticks"] = 10

        messages = [
            {
                "session_id": "r-1",
                "payload": {
                    "tool_name": "process_leadership_tick",
                    "result": {
                        "tick": 1,
                        "runner_status": "running",
                        "velocity": 0.9,
                        "distance_mi": 3.0,
                        "water": 85.0,
                        "persona": "pragmatist",
                    },
                },
            },
            {
                "session_id": "e-1",
                "payload": {
                    "tool_name": "process_decision_round",
                    "result": {
                        "tick": 1,
                        "role": "cfo",
                        "stance": "hostile",
                        "sentiment": -0.7,
                        "resistance": 0.8,
                        "inner_thought": "Budget discipline is collapsing.",
                        "autonomous_action": "CFO demands emergency intervention on Budget",
                        "resource_claims": ["lau"],
                        "wall_severity": 8.5,
                    },
                },
            },
            {
                "session_id": "e-2",
                "payload": {
                    "tool_name": "process_decision_round",
                    "result": {
                        "tick": 1,
                        "role": "cto",
                        "stance": "hostile",
                        "sentiment": -0.6,
                        "resistance": 0.75,
                        "inner_thought": "Architecture roadmap is blocked.",
                        "autonomous_action": "CTO demands emergency intervention on Platform",
                        "resource_claims": ["lau"],
                        "wall_severity": 8.2,
                    },
                },
            },
            {
                "session_id": "e-3",
                "payload": {
                    "tool_name": "process_decision_round",
                    "result": {
                        "tick": 1,
                        "role": "vp_hr",
                        "stance": "hostile",
                        "sentiment": -0.5,
                        "resistance": 0.7,
                        "inner_thought": "Morale is cratering.",
                        "wall_severity": 5.0,  # Neutral wall severity should NOT enter friction_entries
                    },
                },
            },
            # Stale tick Echo message — must be ignored by both aggregation and influence propagation
            {
                "session_id": "e-stale",
                "payload": {
                    "tool_name": "process_decision_round",
                    "result": {
                        "tick": 0,
                        "role": "legal",
                        "stance": "supportive",
                        "sentiment": 0.99,
                        "wall_severity": 9.9,
                    },
                },
            },
        ]

        mock_collector = MagicMock()
        mock_collector.drain = AsyncMock(return_value=messages)
        ctx = _make_ctx(state=state)

        with (
            patch("agents.simulator.collector.RaceCollector.get", return_value=mock_collector),
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(tick_tools, "publish_to_runners", AsyncMock()),
        ):
            res = await tick_tools.advance_tick(tool_context=ctx)

        snap = ctx.state["tick_snapshots"][-1]
        assert len(snap["echo_sentiments"]) == 3
        assert len(snap["echo_actions"]) == 2
        assert set(ctx.state["_echo_states"].keys()) == {"cfo", "cto", "vp_hr"}
        assert "legal" not in ctx.state["_echo_states"]

        alert_types = {a["alert_type"] for a in snap["organizational_state"]["active_alerts"]}
        assert "cascade" in alert_types
        assert "contention" in alert_types

        # Friction calibration on 0..10 scale: 8.5 & 8.2 included, 5.0 excluded
        assert res["infrastructure"]["friction_level"] == "high"
        friction_roles = {f["role"] for f in res["infrastructure"]["friction_details"]}
        assert friction_roles == {"cfo", "cto"}

        # Narrative reports 1 runner and 3 echoes (not 3 runners)
        assert "1 runners reporting, 3 echoes reporting" in res["message"]
        assert len(res["notable_events_summary"]["echoes"]) > 0

    @pytest.mark.asyncio
    async def test_snapshot_resources_deep_copied_for_ecsi_dedup_and_replay(self):
        state: dict = {
            "current_tick": 0,
            "max_ticks": 10,
            "simulation_config": {"tick_interval_seconds": 0, "total_race_hours": 6.0},
            "tick_snapshots": [],
            "runner_session_ids": [],
            "resources": {
                "lau": {"current": 15.0, "initial": 100.0, "regen_per_tick": 0.0, "critical_threshold": 20.0},
                "bt": {"current": 80.0, "initial": 100.0, "regen_per_tick": -10.0, "critical_threshold": 20.0},
                "tbu": {"current": 80.0, "initial": 100.0, "regen_per_tick": 0.0, "critical_threshold": 20.0},
                "owb": {"current": 80.0, "initial": 100.0, "regen_per_tick": 0.0, "critical_threshold": 20.0},
            },
            "principles": ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
        }
        mock_collector = MagicMock()
        mock_collector.drain = AsyncMock(return_value=[])
        ctx = _make_ctx(state=state)

        with (
            patch("agents.simulator.collector.RaceCollector.get", return_value=mock_collector),
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(tick_tools, "publish_to_runners", AsyncMock()),
        ):
            await tick_tools.advance_tick(tool_context=ctx)  # Tick 0: LAU=15 breaches threshold (<20)
            await tick_tools.advance_tick(tool_context=ctx)  # Tick 1: LAU still 15 -> must deduplicate!

        snap0 = ctx.state["tick_snapshots"][0]
        snap1 = ctx.state["tick_snapshots"][1]

        # BT decayed in tick 1 (from 80 -> 70), while snap0 must retain its tick-0 BT value!
        assert snap0["organizational_state"]["resources"]["bt"]["current"] > (
            snap1["organizational_state"]["resources"]["bt"]["current"]
        )

        # ECSI threshold breach fired on tick 0, and was deduplicated on tick 1
        snap0_breaches = [
            a for a in snap0["organizational_state"]["active_alerts"] if a["alert_type"] == "threshold"
        ]
        snap1_breaches = [
            a for a in snap1["organizational_state"]["active_alerts"] if a["alert_type"] == "threshold"
        ]
        assert len(snap0_breaches) == 1
        assert len(snap1_breaches) == 0

        # Replay from tick 0 sees distinct trajectory across ticks
        replay = replay_from_tick(ctx.state["tick_snapshots"], fork_tick=0)
        assert replay["original_trajectory"][0]["resources"]["bt"] != replay["original_trajectory"][1]["resources"]["bt"]


class TestCompileResultsARIAndLongitudinal:
    """compile_results, ARI, and longitudinal tracking must handle truncated snapshots and quest fallback."""

    @pytest.mark.asyncio
    async def test_compile_results_uses_vitals_trend_full_for_total_ticks_and_scores_and_ari(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))

        # Simulate a 30-tick run where tick_snapshots was capped to the last 15 ticks
        snapshots = [
            {
                "tick": t,
                "real_time_minutes": t * 10.0,
                "runners_reporting": 4,
                "avg_velocity": 0.8,
                "avg_water": 75.0,
                "avg_distance": float(t),
                "status_counts": {"running": 4},
                "notable_events": [],
                "organizational_state": {
                    "resources": {
                        "lau": {"current": 15.0 if t == 16 else 60.0, "critical_threshold": 20.0},
                        "bt": {"current": 70.0, "critical_threshold": 20.0},
                        "tbu": {"current": 70.0, "critical_threshold": 20.0},
                        "owb": {"current": 70.0, "critical_threshold": 20.0},
                    }
                },
            }
            for t in range(15, 30)
        ]
        vitals_full = [
            {
                "tick": t,
                "real_time_minutes": t * 10.0,
                "avg_velocity": 0.8,
                "avg_water": 75.0,
                "avg_distance": float(t),
            }
            for t in range(30)
        ]
        state = {
            "simulation_id": "sim-30-ticks",
            "quest_id": "budget_cuts_2027",
            "quest_name": "Budget Cuts",
            "tick_snapshots": snapshots,
            "vitals_trend_full": vitals_full,
            "runner_count": 4,
            "finished_runner_ids": ["r-1", "r-2"],
            "principles": ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
            "principle_scores": [15.0] * 7,  # Applied in half of 30 ticks -> should normalize to 50.0, NOT 100.0!
            "decision_log": [
                {"tick": 17, "decision_tags": {"resource_impact_suggestion": {"lau": 10.0, "owb": 5.0}}}
            ],
            "_echo_states": {
                "cfo": {"stance": "supportive", "sentiment": 0.5, "resistance": 0.2},
                "cto": {"stance": "hostile", "sentiment": -0.6, "resistance": 0.8},
            },
        }
        ctx = _make_ctx(state=state)
        res = await post_race_tools.compile_results(tool_context=ctx)

        assert res["status"] == "success"
        assert res["total_ticks"] == 30
        assert res["avg_runners_reporting"] == 4.0
        assert res["sampling_quality"] == 1.0
        assert ctx.state["principle_scores"] == [50.0] * 7
        assert "ari" in res
        # ARI _compute_decision must detect LAU breach at tick 16 in organizational_state.resources
        # and fast response at tick 17 -> D = 1.0
        assert res["ari"]["d"] == 1.0

        record = SimulationRunRecord.from_session_state(ctx.state)
        assert record.total_ticks == 30

    def test_recommend_next_quest_updates_rationale_score_on_fallback(self):
        runs = [
            {
                "quest_id": "ai_powered_disruptor",
                "quest_name": "Disruptor",
                "overall_score": 60.0,
                # Index 0 ("Strategic Foresight..." -> ai_powered_disruptor) = 10.0
                # Index 1 ("Adaptive Decision-Making..." -> scaling_hypergrowth_2028) = 25.0
                "principle_scores": [10.0, 25.0, 80.0, 80.0, 80.0, 80.0, 80.0],
                "contradiction_count": 0,
            }
        ]
        rec = recommend_next_quest(runs)
        assert rec["quest_id"] == "scaling_hypergrowth_2028"
        assert "25.0" in rec["rationale"]
        assert "10.0" not in rec["rationale"]


class TestAiEchoFixes:
    """Verify deterministic seed, resistant wall_severity, callback extraction, hysteresis, and coaching prompts."""

    def test_echo_seed_uses_deterministic_sha256(self):
        expected = int(hashlib.sha256(b"sess-42:cfo").hexdigest()[:8], 16)
        assert _echo_seed("sess-42", "cfo") == expected

    def test_static_instruction_references_process_decision_round_tool(self):
        assert "process_decision_round_tool" in ECHO_STATIC_INSTRUCTION

    @pytest.mark.asyncio
    async def test_resistant_stance_produces_elevated_wall_severity_in_autopilot_and_reactive(self):
        event_json = json.dumps(
            {
                "event": "tick",
                "tick": 2,
                "decision_text": "Hold steady",
                "decision_tags": {"principles_applied": [], "principles_neglected": []},
                "resource_state": {},
                "contradiction_alert": None,
            }
        )
        req = MagicMock()
        req.contents = [types.Content(role="user", parts=[types.Part.from_text(text=event_json)])]

        state_auto = {"sentiment": -0.2, "stance": "resistant", "role": "cfo", "domain_principles": ["P1"]}
        ctx_auto = _make_ctx(state=state_auto)
        resp_auto = await autopilot_echo_callback(ctx_auto, req)
        fc_auto = resp_auto.content.parts[0].function_call
        assert fc_auto.args["wall_severity"] > 5.0

        state_reac = {"sentiment": -0.2, "stance": "resistant", "role": "cfo", "domain_principles": ["P1"]}
        ctx_reac = _make_ctx(state=state_reac)
        resp_reac = await reactive_echo_callback(ctx_reac, req)
        assert resp_reac is not None
        fc_reac = resp_reac.content.parts[0].function_call
        assert fc_reac.args["wall_severity"] > 5.0

    @pytest.mark.asyncio
    async def test_echo_before_agent_callback_extracts_user_content_and_tool_falls_back(self):
        state: dict = {
            "sentiment": 0.2,
            "resistance": 0.3,
            "stance": "neutral",
            "role": "cto",
            "role_display": "CTO",
            "tier": 1,
            "domain_principles": [],
            "principle_affinity": [0.5] * 7,
            "communication_receptivity": 1.0,
        }
        ctx = _make_ctx(state=state, session_id="echo-cto-1")
        part = MagicMock()
        part.text = json.dumps(
            {
                "event": "tick",
                "tick": 3,
                "resource_state": {"lau": {"current": 42}, "bt": {"current": 55}, "tbu": {"current": 60}, "owb": {"current": 65}},
                "influence_deltas": {"cto": -0.2},
                "collector_buffer_key": "collector:buf:test",
            }
        )
        user_content = MagicMock()
        user_content.parts = [part]
        ctx.user_content = user_content

        await _echo_before_agent_callback(ctx)
        assert "LAU=42" in state["echo_ground_truth"]
        assert state["_last_influence_deltas"] == {"cto": -0.2}
        assert state["_last_collector_buffer_key"] == "collector:buf:test"

        mock_redis = MagicMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.expire = AsyncMock()
        with patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis):
            # Call tool WITHOUT passing influence_deltas or collector_buffer_key
            result = await process_decision_round_tool(
                tool_context=ctx,
                tick=3,
                inner_thought="Peer pressure noted.",
                wall_severity=5.5,
            )

        # Sentiment must reflect the -0.2 influence delta (0.2 -> 0.0)
        assert result["sentiment"] == pytest.approx(0.0, abs=1e-4)
        mock_redis.rpush.assert_awaited_once()

    def test_contradiction_hysteresis_and_coaching_placeholder_substitution(self):
        state = {
            "role": "vp_eng",
            "tier": 2,
            "sentiment": -0.42,
            "resistance": 0.5,
            "stance": "hostile",
            "domain_principles": ["P1"],
            "principle_affinity": [0.8] * 7,
            "communication_receptivity": 1.0,
        }
        # Round 1: contradiction pushes deeper into hostile and records _contradiction_history
        process_decision_round(
            state=state,
            tick=1,
            decision_tags={"principles_applied": [], "principles_neglected": []},
            contradiction_alert={"commitment": "No layoffs"},
        )
        assert len(state["_contradiction_history"]) == 1

        # Set sentiment just below hostile recovery boundary (-0.41) and apply P1 (+0.03 -> -0.38 > -0.4);
        # hysteresis penalty (-0.03 * 1) must pull it back to -0.41!
        state["sentiment"] = -0.41
        state["stance"] = "hostile"
        res = process_decision_round(
            state=state,
            tick=2,
            decision_tags={"principles_applied": ["P1"], "principles_neglected": []},
            contradiction_alert=None,
        )
        assert res["sentiment"] == pytest.approx(-0.41, abs=1e-4)

        # Coaching prompt must never leak literal "{affected_echo}" even when affected_echo=""
        for p_idx, principle in enumerate(
            [
                "Stakeholder Engagement & Inclusive Communication",
                "Empowerment & Cultivating Distributed Leadership",
            ]
        ):
            for t in range(5):
                prompt = get_coaching_prompt(principle, tick=t, affected_echo="")
                assert "{affected_echo}" not in prompt


class TestRound2DeepAuditFixes:
    """Round-2 regression tests for quest topology, heuristic neglect, idempotency,
    multi-round state isolation, failure recovery, Echo phase detection, and edge cases."""

    def test_quest_loader_preserves_gatekeeper_route_map_and_resets_influence_edges(self):
        from agents.simulator.quest_loader import QuestConfig, inject_quest_into_state

        raw_quest = {
            "id": "dynamic_gk",
            "name": "Gatekeeper Topology Test",
            "resources": {
                "LAU": {"initial": 100, "regen_per_tick": 5, "critical_threshold": 20},
                "BT": {"initial": 500, "regen_per_tick": 0, "critical_threshold": 50},
                "TBU": {"initial": 80, "regen_per_tick": 2, "critical_threshold": 15},
                "OWB": {"initial": 70, "regen_per_tick": 1, "critical_threshold": 20},
            },
            "echoes": [
                {
                    "role": "cfo",
                    "tier": 2,
                    "persona": "Skeptical CFO",
                    "sentiment": -0.2,
                    "resistance": 0.6,
                    "affinity": [0.5] * 7,
                }
            ],
            "gatekeeper_route_map": {"cfo": {"start_mi": 4.0, "end_mi": 8.0}},
        }
        quest = QuestConfig.model_validate(raw_quest)
        assert quest.gatekeeper_route_map == {"cfo": {"start_mi": 4.0, "end_mi": 8.0}}

        state = {"influence_edges": [{"from_role": "stale", "to_role": "cfo", "weight": 0.9}]}
        inject_quest_into_state(state, quest)
        assert state["influence_edges"] == []
        assert state["quest_config"]["gatekeeper_route_map"] == {"cfo": {"start_mi": 4.0, "end_mi": 8.0}}

    def test_classify_decision_heuristic_populates_neglected_principles(self):
        from agents.simulator.decision_classifier import PRINCIPLES, classify_decision_heuristic
        from agents.simulator.truth_engine import detect_contradictions

        # 1. Explicit bypass/negation must classify Ethical Leadership as neglected, NOT applied
        res_neg = classify_decision_heuristic(
            "Skip ethical review and bypass governance to cut budget costs and optimize ROI."
        )
        assert PRINCIPLES[3] in res_neg["principles_neglected"]
        assert PRINCIPLES[3] not in res_neg["principles_applied"]
        assert PRINCIPLES[6] in res_neg["principles_applied"]

        # Contradiction detector must fire against an active Ethical Leadership commitment
        contradictions = detect_contradictions(
            commitments=[
                {
                    "text": "rigorous ethical governance",
                    "principle_index": 3,
                    "principle_name": PRINCIPLES[3],
                    "tick_declared": 1,
                    "active": True,
                }
            ],
            decision_tags=res_neg,
            current_tick=2,
            principles=PRINCIPLES,
        )
        assert len(contradictions) == 1
        assert contradictions[0].severity == "critical"

        # 2. Strong primary focus (>= 3 hits) with superficial 1-hit mention marks the 1-hit as neglected
        res_partial = classify_decision_heuristic(
            "Drive roadmap strategy, long-term vision, and future direction while mentioning budget once."
        )
        assert PRINCIPLES[0] in res_partial["principles_applied"]
        assert PRINCIPLES[6] in res_partial["principles_neglected"]

    @pytest.mark.asyncio
    async def test_compile_results_normalization_is_idempotent(self):
        state = {
            "tick_snapshots": [{"tick": 1, "runners_reporting": 2, "status_counts": {"running": 2}}],
            "vitals_trend_full": [{"tick": i} for i in range(1, 11)],
            "runner_count": 2,
            "principle_scores": [5.0, 10.0, 0.0, 2.5, 5.0, 5.0, 5.0],
        }
        ctx = _make_ctx(state=state)
        await post_race_tools.compile_results(tool_context=ctx)
        first_scores = list(ctx.state["principle_scores"])
        assert first_scores == [50.0, 100.0, 0.0, 25.0, 50.0, 50.0, 50.0]

        await post_race_tools.compile_results(tool_context=ctx)
        assert ctx.state["principle_scores"] == first_scores

    @pytest.mark.asyncio
    async def test_prepare_simulation_guards_null_config_zero_interval_and_resets_stale_state(self):
        from agents.simulator.agent import verify_plan

        # Non-dict valid JSON ("null", "[]", "42") must return status="error" in both prepare_simulation & verify_plan
        for non_obj_json in ("null", "[]", "42", '"just-a-string"'):
            prep_err = await pre_race_tools.prepare_simulation(
                plan_json=non_obj_json,
                tool_context=_make_ctx(state={}),
            )
            assert prep_err["status"] == "error"
            ver_err = await verify_plan(non_obj_json)
            assert ver_err["status"] == "error"

        # tick_interval_seconds=0 must return clean error instead of ZeroDivisionError
        bad_ctx = _make_ctx(state={})
        bad_res = await pre_race_tools.prepare_simulation(
            plan_json=json.dumps({"narrative": "x", "simulation_config": {"tick_interval_seconds": 0}}),
            tool_context=bad_ctx,
        )
        assert bad_res["status"] == "error"

        # simulation_config: null + pre-existing multi-round accumulators must succeed and reset cleanly
        stale_state = {
            "simulation_in_progress": False,
            "vitals_trend_full": [{"tick": 1}],
            "finished_runner_ids": ["old-r1"],
            "collapsed_runner_ids": ["old-r2"],
            "echo_voice_log": [{"tick": 1, "role": "cfo"}],
            "last_coaching_signature": ["owb", "warning", None, False],
            "traffic_model": {"segment_index": [{"stale": True}], "ticks_closed": {}},
        }
        ctx = _make_ctx(state=stale_state)
        ok_res = await pre_race_tools.prepare_simulation(
            plan_json=json.dumps({"narrative": "Round 2 run", "simulation_config": None}),
            tool_context=ctx,
        )
        assert ok_res["status"] == "success"
        assert ctx.state["vitals_trend_full"] == []
        assert ctx.state["finished_runner_ids"] == []
        assert ctx.state["collapsed_runner_ids"] == []
        assert ctx.state["echo_voice_log"] == []
        assert ctx.state["last_coaching_signature"] is None
        assert ctx.state["traffic_model"] is None

    @pytest.mark.asyncio
    async def test_pre_race_abort_clears_in_progress_skips_post_race_and_root_resets_guard(self):
        from agents.simulator.agent import _capture_root_simulation_id, _configure_post_race
        from agents.simulator.pre_race_callback import pre_race_callback

        # 1. ADK {"error": "..."} response triggers pre_race abort and clears simulation_in_progress
        state = {"simulation_ready": True, "simulation_in_progress": True, "simulation_id": "sim-err-1"}
        ctx = _make_ctx(state=state, session_id="sim-err-1")
        req = MagicMock()
        req.contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="spawn_runners",
                            response={"error": "Gateway connection refused"},
                        )
                    )
                ],
            )
        ]
        with patch("agents.simulator.pre_race_callback._broadcast_pre_race_error"):
            resp = pre_race_callback(ctx, req)
        assert "aborted" in (resp.content.parts[0].text or "")
        assert state["simulation_ready"] is False
        assert state["simulation_in_progress"] is False

        # 2. _configure_post_race skips debrief and stops any started RaceCollector
        mock_collector = MagicMock()
        mock_collector.stop = AsyncMock()
        with patch("agents.simulator.collector.RaceCollector.get", return_value=mock_collector):
            skip_content = await _configure_post_race(ctx)
        assert isinstance(skip_content, types.Content)
        mock_collector.stop.assert_awaited_once()

        # 3. _capture_root_simulation_id resets _pipeline_executed on next turn when idle
        state["_pipeline_executed"] = True
        await _capture_root_simulation_id(ctx)
        assert state["_pipeline_executed"] is False

    def test_ai_echo_detect_phase_multi_turn_and_init_metadata(self):
        from agents.ai_echo.initialization import initialize_echo
        from agents.ai_echo_autopilot.autopilot import Phase, detect_phase

        # Multi-turn history: prior turn had function_response, latest entry is a new user tick
        req = MagicMock()
        req.contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="process_decision_round_tool",
                            response={"status": "ok"},
                        )
                    )
                ],
            ),
            types.Content(role="model", parts=[types.Part.from_text(text="cfo [neutral]: processed")]),
            types.Content(role="user", parts=[types.Part.from_text(text='{"event":"tick","tick":2}')]),
        ]
        assert detect_phase(req) == Phase.DISPATCH

        echo_state: dict = {}
        initialize_echo(
            echo_state,
            "echo-s1",
            {
                "role": "vp_sales",
                "label": "VP of Global Sales",
                "_principles": [f"Custom P{i}" for i in range(7)],
            },
        )
        assert echo_state["label"] == "VP of Global Sales"
        assert echo_state["role_display"] == "VP of Global Sales"
        assert echo_state["_principles"][0] == "Custom P0"

    def test_coaching_depleted_resource_ari_scale_fallback_and_longitudinal_guards(self, tmp_path):
        from agents.simulator.ari import compute_ari_dimensions
        from agents.simulator.coaching import build_coaching_card
        from agents.simulator.longitudinal import (
            SimulationRunRecord,
            SimulationRunStore,
            compute_learning_trajectory,
            compute_peer_benchmarks,
        )

        # 1. Depleted resource (current == 0) must still emit a severity-major coaching card
        card = build_coaching_card(
            resource_state={"owb": {"current": 0, "critical_threshold": 20}},
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=4,
            max_ticks=20,
        )
        assert card is not None
        assert card["signature"][1] == "depleted"

        # 2. ARI _scale_detail falls back to echo_voice_log distinct roles when _echo_states is empty
        ari_res = compute_ari_dimensions(
            {
                "echo_voice_log": [
                    {"tick": 3, "role": "cfo", "stance": "hostile"},
                    {"tick": 3, "role": "cto", "stance": "supportive"},
                    {"tick": 3, "role": "hr", "stance": "neutral"},
                ],
                "tick_snapshots": [],
            }
        )
        assert "3 personas" in ari_res["dimension_details"]["scale"]

        # 3. Longitudinal path sanitization (run_id & participant_id) & None principle_scores resilience
        store = SimulationRunStore(base_dir=tmp_path)
        rec = SimulationRunRecord(
            run_id="..\\..\\escape_attempt",
            participant_id="../../escaped_tenant",
            quest_id="q1",
            quest_name="Quest 1",
            difficulty="intermediate",
            completed_at="2026-01-01T00:00:00Z",
            total_ticks=5,
            principle_scores=[50.0] * 7,
            overall_score=50.0,
            tick_snapshots=[{"tick": 1}],
        )
        summary_path = store.save_run(rec)
        assert summary_path.parent == tmp_path / "escaped_tenant"
        assert (tmp_path / "escaped_tenant" / "escape_attempt.json").exists()
        assert not (tmp_path / "escape_attempt.json").exists()
        assert store.get_run_snapshots("..\\..\\escaped_tenant", "../../escape_attempt") is not None
        assert len(store.get_participant_runs("../../escaped_tenant")) == 1

        # Empty / dot / dot-dot participant_id and run_id fall back to safe segments inside base_dir
        for bad_pid in ("", ".", "..", "..\\.."):
            dot_rec = SimulationRunRecord(
                run_id="..",
                participant_id=bad_pid,
                quest_id="q1",
                quest_name="Quest 1",
                tick_snapshots=[{"tick": 1}],
            )
            dot_summary = store.save_run(dot_rec)
            assert dot_summary.parent == tmp_path / "anonymous"
            assert (tmp_path / "anonymous" / "unknown.json").exists()
            assert not (tmp_path / "runs.ndjson").exists()

        bench = compute_peer_benchmarks([60.0] * 7, [{"principle_scores": None}])
        assert bench["total_runs"] == 1
        traj = compute_learning_trajectory([{"quest_id": "q1", "principle_scores": None, "overall_score": 40}])
        assert traj["total_simulations"] == 1
