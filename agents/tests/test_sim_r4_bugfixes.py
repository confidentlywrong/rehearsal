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

"""Round-4 regression tests covering bug fixes across simulator, runners, echoes, and quest configs."""

import asyncio
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.sessions.state import State
from google.genai import types
from pydantic import ValidationError


def _load_module_from_path(name: str, rel_path: str):
    """Import a hyphenated skill tools module directly by repo-relative path."""
    repo_root = Path(__file__).resolve().parents[2]
    file_path = repo_root / rel_path
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_llm_request(payload: str, *, role: str = "user") -> LlmRequest:
    """Create a minimal LlmRequest with one text part."""
    return LlmRequest(
        model="test-model",
        contents=[types.Content(role=role, parts=[types.Part.from_text(text=payload)])],
    )


def _make_tool_context(state: dict | State | None = None, session_id: str | None = "sess-1"):
    """Build a lightweight ToolContext stand-in."""
    return SimpleNamespace(
        state={} if state is None else state,
        session=SimpleNamespace(id=session_id),
    )


# ---------------------------------------------------------------------------
# 1. Runner & Leadership Runner fixes
# ---------------------------------------------------------------------------


class TestRunnerR4:
    """Verify runner toilet overshoot sync and leadership capital sanitization."""

    @pytest.mark.asyncio
    async def test_process_tick_toilet_slowdown_syncs_raw_distance_for_finish_time(self):
        """A toilet break on the finishing tick uses slowed raw_distance in finish_time_minutes."""
        from agents.runner.running import process_tick

        state = {
            "velocity": 9.0,  # 1.5 mi over 10 min before toilet; 1.35 mi after 0.9x toilet penalty
            "distance": 25.0,
            "water": 100.0,
            "comfort": 30.0,  # >= 20 (comfort_factor=1.0) and after -5 decay -> 25.0 < 40.0
            "_tick_params": {
                "tick": 10,
                "minutes_per_tick": 10.0,
                "elapsed_minutes": 100.0,
                "race_distance_mi": 26.0,
                "station_index": {"portable_toilets": [25.5], "water_stations": []},
            },
        }
        res = await process_tick(_make_tool_context(state, "runner-toilet"), "Almost there")

        assert res["runner_status"] == "finished"
        assert state["finished"] is True
        assert state["comfort"] == 100.0  # Confirms toilet stop fired
        assert res["distance_mi"] == 26.0
        # finish_time_minutes uses post-toilet raw_distance = 25.0 + mi_this_tick
        mi = res["mi_this_tick"]
        expected_finish = round(100.0 - 10.0 * ((25.0 + mi - 26.0) / mi), 2)
        assert state["finish_time_minutes"] == pytest.approx(expected_finish, abs=0.02)

    @pytest.mark.asyncio
    async def test_capital_tools_sanitize_corrupted_water_on_early_returns(self):
        """deplete_capital and restore_capital return finite water on corrupted-state early returns."""
        from agents.leadership_runner.capital import deplete_capital, restore_capital

        ctx_fin = _make_tool_context({"finished": True, "water": None})
        res_dep_fin = await deplete_capital(10.0, ctx_fin)
        assert res_dep_fin["status"] == "already_finished"
        assert res_dep_fin["water"] == 100.0

        ctx_cool = _make_tool_context(
            {
                "water": float("nan"),
                "_current_tick": 6,
                "_capital_restored_window": [[5, 30.0]],
            }
        )
        res_rest_cool = await restore_capital(10.0, ctx_cool)
        assert res_rest_cool["status"] == "cooldown"
        assert res_rest_cool["water"] == 100.0

        ctx_bad_amt = _make_tool_context({"water": 50.0})
        res_bad_amt = await deplete_capital(float("inf"), ctx_bad_amt)
        assert res_bad_amt["status"] == "success"
        assert res_bad_amt["water"] == 50.0
        assert math.isfinite(ctx_bad_amt.state["water"])

    def test_leadership_autopilot_handle_tick_falls_back_when_runner_count_missing(self):
        """Missing or None runner_count in leadership autopilot falls back to 1 cleanly."""
        from agents.leadership_runner_autopilot.autopilot import handle_tick

        state: dict = {
            "runner_count": None,
            "runner_id": "lr-1",
            "distance": 0.0,
            "water": 100.0,
        }
        resp = handle_tick(
            state,
            {
                "tick": 0,
                "minutes_per_tick": 0.0,
                "elapsed_minutes": 0.0,
                "race_distance_mi": 26.2,
                "runner_id": "lr-1",
            },
        )
        assert resp.content is not None
        assert resp.content.parts
        assert resp.content.parts[0].function_call is not None
        assert resp.content.parts[0].function_call.name == "process_leadership_tick"
        assert state["velocity"] > 0.0


# ---------------------------------------------------------------------------
# 2. Reactive Echo callback fixes
# ---------------------------------------------------------------------------


class TestReactiveEchoR4:
    """Verify reactive Echo terminal short-circuit and JSON null state guards."""

    @pytest.mark.asyncio
    async def test_reactive_echo_short_circuits_terminal_echoes_even_when_relevant(self):
        """Aligned or defected reactive Echoes do not hit the LLM on domain-relevant ticks."""
        from agents.ai_echo_reactive.callback import reactive_echo_callback

        req = _make_llm_request(
            json.dumps(
                {
                    "event": "tick",
                    "tick": 5,
                    "decision_text": "Cut R&D and finance headcount immediately",
                    "decision_tags": {"principles_applied": [], "principles_neglected": ["Financial Discipline"]},
                    "resource_state": {"owb": {"current": 40.0, "critical_threshold": 20.0}},
                    "influence_deltas": {"cfo": -0.2},
                    "collector_buffer_key": "sim:buf:1",
                }
            )
        )
        for flag in ("aligned", "defected"):
            state = {
                flag: True,
                "role": "cfo",
                "role_display": "CFO",
                "sentiment": 0.4,
                "stance": flag,
                "resistance": 0.2,
                "domain_principles": ["Financial Discipline"],
            }
            ctx = _make_tool_context(state=state, session_id=f"echo-{flag}")
            resp = await reactive_echo_callback(ctx, req)  # type: ignore[arg-type]
            assert resp is not None
            assert resp.content is not None
            assert resp.content.parts
            fc = resp.content.parts[0].function_call
            assert fc is not None and fc.name in ("process_decision_round", "process_decision_round_tool")

    @pytest.mark.asyncio
    async def test_reactive_echo_guards_json_null_payload_fields_on_llm_fallthrough(self):
        """Explicit JSON null resource_state / influence_deltas / collector_buffer_key become safe types."""
        from agents.ai_echo_reactive.callback import reactive_echo_callback

        req = _make_llm_request(
            json.dumps(
                {
                    "event": "tick",
                    "tick": 3,
                    "decision_text": "Financial Discipline review",
                    "decision_tags": {"principles_applied": ["Financial Discipline"]},
                    "resource_state": None,
                    "influence_deltas": None,
                    "collector_buffer_key": None,
                }
            )
        )
        state = {
            "role": "cfo",
            "role_display": "CFO",
            "sentiment": 0.1,
            "stance": "neutral",
            "resistance": 0.3,
            "domain_principles": ["Financial Discipline"],
        }
        ctx = _make_tool_context(state=state, session_id="echo-cfo")
        resp = await reactive_echo_callback(ctx, req)  # type: ignore[arg-type]
        assert resp is None  # Domain-relevant non-terminal Echo delegates to LLM
        assert state["_current_resources"] == {}
        assert state["_last_influence_deltas"] == {}
        assert state["_last_collector_buffer_key"] == ""


# ---------------------------------------------------------------------------
# 3. Core Simulator Engine edge cases (ECSI, ARI, Truth, Classifier, Coaching, UI, Callback)
# ---------------------------------------------------------------------------


class TestSimulatorEngineR4:
    """Verify NaN/Inf/bool-is-int/None guards across core simulator modules."""

    def test_ecsi_detect_contention_handles_nan_and_inf_ticks(self):
        """detect_contention safely handles NaN/Inf snapshot ticks without crashing."""
        from agents.simulator.ecsi import detect_contention

        for bad_tick in (float("nan"), float("inf"), float("-inf"), True):
            alerts = detect_contention(
                {"tick": bad_tick, "echo_sentiments": {}, "notable_events": []},
                {"resources": {"owb": {"critical_threshold": 20.0}}},
                {"owb": 50.0},
                state={"tick_snapshots": []},
            )
            assert isinstance(alerts, list)

    def test_ari_handles_nan_inf_bool_ticks_in_echo_voice_log(self):
        """compute_ari_dimensions skips non-finite and bool ticks in echo_voice_log._compute_scale."""
        from agents.simulator.ari import compute_ari_dimensions

        state = {
            "tick_snapshots": [{"tick": 1, "avg_distance": 5.0}],
            "decision_log": [{"tick": 1, "decision_tags": {"principles_applied": ["P1"], "principles_neglected": []}}],
            "principle_scores": [70.0],
            "echo_voice_log": [
                {"tick": float("nan"), "sentiment": 0.2, "autonomous_action": "warn", "resistance": 0.4},
                {"tick": float("inf"), "sentiment": -0.3, "notable_event": "pushback", "resistance": 0.6},
                {"tick": True, "sentiment": 0.1, "autonomous_action": "note", "resistance": 0.2},
                {"tick": 1, "sentiment": 0.3, "autonomous_action": "align", "resistance": 0.2},
            ],
        }
        result = compute_ari_dimensions(state)
        for key in ("o", "j", "d", "s", "ari"):
            assert math.isfinite(result[key])

    def test_truth_engine_handles_non_finite_ticks_and_inf_affinities(self):
        """extract_commitments, detect_contradictions, and find_best_responder reject NaN/Inf cleanly."""
        from agents.simulator.truth_engine import (
            Contradiction,
            detect_contradictions,
            extract_commitments,
            find_best_responder,
        )

        principles = ["Ethical Leadership & Governance", "Customer Obsession"]
        commitments = extract_commitments("I commit to Ethical Leadership.", float("nan"), principles)  # type: ignore[arg-type]
        assert len(commitments) == 1
        assert commitments[0].tick_declared == 0

        contradictions = detect_contradictions(
            [
                {
                    "principle_name": "Ethical Leadership & Governance",
                    "principle_index": 0,
                    "text": "we commit to integrity",
                    "tick_declared": float("inf"),
                    "active": True,
                }
            ],
            {"principles_applied": [], "principles_neglected": ["Ethical Leadership & Governance"], "severity": "major"},
            float("nan"),  # type: ignore[arg-type]
            principles,
        )
        assert len(contradictions) == 1
        assert contradictions[0].tick_detected == 0
        assert contradictions[0].tick_declared == 0

        # Non-finite Inf affinity must NOT beat finite high-affinity Echo.
        responder = find_best_responder(
            [
                Contradiction(
                    commitment_text="commit",
                    violated_principle="Ethical Leadership & Governance",
                    violated_principle_index=0,
                    evidence="ignored governance",
                    tick_detected=2,
                    tick_declared=1,
                    severity="critical",
                )
            ],
            [
                {"role": "broken_inf", "affinity": [float("inf")]},
                {"role": "chief_ethics", "affinity": [0.9]},
            ],
        )
        assert responder == "chief_ethics"

    def test_black_swan_trigger_rejects_bool_and_nan_ticks(self):
        """apply_black_swan_events rejects bool True==1 and NaN ticks cleanly."""
        from agents.simulator.decision_classifier import apply_black_swan_events

        resources = {"owb": {"current": 50.0}}
        events = [
            {
                "event": "Bool tick trap",
                "trigger": {"type": "tick", "tick": True},
                "resource_impact": {"owb": -10.0},
            },
            {
                "event": "NaN tick trap",
                "trigger": {"type": "tick", "tick": float("nan")},
                "resource_impact": {"owb": -10.0},
            },
            {
                "event": "Real tick 1 event",
                "trigger": {"type": "tick", "tick": 1},
                "resource_impact": {"owb": -10.0},
            },
        ]
        fired = apply_black_swan_events(1, resources, events, [])
        assert [e["event"] for e in fired] == ["Real tick 1 event"]
        assert resources["owb"]["current"] == 40.0

    def test_coaching_handles_non_list_alerts_and_negative_crisis_current(self):
        """generate_micro_feedback ignores non-sequence alerts; build_coaching_card alerts when current < 0."""
        from agents.simulator.coaching import build_coaching_card, generate_micro_feedback

        feedback = generate_micro_feedback(
            decision_tags={"principles_applied": [], "principles_neglected": []},
            resource_state={"owb": {"current": 60.0, "critical_threshold": 20.0}},
            alerts=123,  # type: ignore[arg-type]
            tick=4,
        )
        assert isinstance(feedback, str)

        card = build_coaching_card(
            resource_state={"owb": {"current": -5.0, "critical_threshold": 20.0, "label": "Workforce Wellbeing"}},
            decision_tags={"principles_applied": [], "principles_neglected": []},
            alerts=[],
            tick=4,
            max_ticks=20,
        )
        assert card is not None
        assert "surfaceUpdate" in card

    def test_live_dashboard_defaults_null_avg_comfort_to_100(self):
        """Explicit None avg_comfort defaults Psych. Safety bar to 100.0 instead of 0.0."""
        from agents.simulator.live_dashboard import build_live_dashboard

        dash = build_live_dashboard(
            tick=2,
            max_ticks=20,
            completion_pct=10.0,
            resource_state={"owb": {"current": 70.0, "critical_threshold": 20.0, "label": "Wellbeing"}},
            resource_deltas={"owb": 0.0},
            status_counts={"running": 5},
            persona_status_counts={},
            infrastructure={"avg_comfort": None},
            active_alerts_count=0,
            avg_velocity=6.5,
            avg_water=90.0,
        )
        bars = [
            c["component"]["ScoreBar"]
            for c in dash["surfaceUpdate"]["components"]
            if c.get("id") == "dash-infra-psych"
        ]
        assert len(bars) == 1
        assert bars[0]["value"] == {"literalNumber": 100.0}

    @pytest.mark.asyncio
    async def test_pre_race_callback_tracks_and_cleans_error_broadcast_tasks(self):
        """pre_race_callback stores async error broadcast tasks in _background_tasks and discards on finish."""
        from agents.simulator import pre_race_callback as prc

        completed = asyncio.Event()

        async def _fake_emit(**_kwargs) -> None:
            completed.set()

        req = LlmRequest(
            model="test-model",
            contents=[
                types.Content(role="user", parts=[types.Part.from_text(text='{"action":"execute"}')]),
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name="prepare_simulation",
                                response={"status": "error", "message": "boom"},
                            )
                        )
                    ],
                ),
            ],
        )
        ctx = SimpleNamespace(state={}, session=SimpleNamespace(id="sim-prc-r4"))

        prc._background_tasks.clear()
        with patch.object(prc, "emit_gateway_message", side_effect=_fake_emit):
            resp = prc.pre_race_callback(ctx, req)  # type: ignore[arg-type]
            assert resp is not None
            assert len(prc._background_tasks) == 1
            await asyncio.wait_for(completed.wait(), timeout=2.0)
            await asyncio.sleep(0)
            assert len(prc._background_tasks) == 0


# ---------------------------------------------------------------------------
# 4. Quest Config YAML files & QuestConfig schema parity
# ---------------------------------------------------------------------------


class TestQuestConfigsR4:
    """Validate all YAML quest configs and enforce finite/valid numeric ranges in QuestConfig."""

    @pytest.mark.parametrize(
        ("quest_id", "expected_echoes"),
        [
            ("ai_powered_disruptor", 11),
            ("budget_cuts", 11),
            ("hypergrowth", 11),
        ],
    )
    def test_all_quest_yaml_files_parse_and_validate_cleanly(self, quest_id: str, expected_echoes: int):
        """All shipped configs/quests/*.yaml validate cleanly against QuestConfig."""
        from agents.simulator.quest_loader import load_quest_from_yaml

        cfg = load_quest_from_yaml(quest_id)
        assert len(cfg.echoes) == expected_echoes
        assert len(cfg.black_swan_events) == 5

        roles = [echo.role for echo in cfg.echoes]
        assert len(roles) == len(set(roles))

        principle_set = set(cfg.principles)
        for echo in cfg.echoes:
            assert set(echo.domain_principles).issubset(principle_set)
            assert math.isfinite(echo.initial_sentiment)
            assert -1.0 <= echo.initial_sentiment <= 1.0
            assert math.isfinite(echo.initial_resistance)
            assert 0.0 <= echo.initial_resistance <= 1.0
            assert len(echo.affinity) == 7
            assert all(math.isfinite(w) and 0.0 <= w <= 1.0 for w in echo.affinity)

        for res in cfg.resources.values():
            assert math.isfinite(res.initial)
            assert math.isfinite(res.regen_per_tick)
            assert math.isfinite(res.critical_threshold)

        for key, seg in cfg.gatekeeper_route_map.items():
            assert isinstance(key, str) and key
            if isinstance(seg, (list, tuple)) and len(seg) == 2:
                assert 0.0 <= seg[0] <= seg[1]
                assert math.isfinite(seg[0]) and math.isfinite(seg[1])
            elif isinstance(seg, dict) and "start_mi" in seg and "end_mi" in seg:
                assert 0.0 <= seg["start_mi"] <= seg["end_mi"]
                assert math.isfinite(seg["start_mi"]) and math.isfinite(seg["end_mi"])

    def test_quest_config_rejects_nan_inf_bool_and_invalid_gatekeeper_ranges(self):
        """QuestConfig rejects non-finite floats, bools, and invalid gatekeeper route ranges."""
        from agents.simulator.quest_loader import (
            BlackSwanEvent,
            BlackSwanTrigger,
            EchoConfig,
            InfluenceEdgeConfig,
            QuestConfig,
            ResourceConfig,
        )

        with pytest.raises(ValidationError):
            ResourceConfig(initial=float("inf"), regen_per_tick=0.0, critical_threshold=10.0)
        with pytest.raises(ValidationError):
            ResourceConfig(initial=50.0, regen_per_tick=float("nan"), critical_threshold=10.0)
        with pytest.raises(ValidationError):
            ResourceConfig(initial=True, regen_per_tick=0.0, critical_threshold=10.0)  # type: ignore[arg-type]

        with pytest.raises(ValidationError):
            BlackSwanTrigger(type="tick", tick=True)  # type: ignore[arg-type]

        with pytest.raises(ValidationError):
            BlackSwanEvent(
                trigger=BlackSwanTrigger(type="tick", tick=5, when=None),
                event="Bad",
                resource_impact={"owb": float("nan")},
            )

        with pytest.raises(ValidationError):
            EchoConfig(
                role="cfo",
                persona="CFO persona",
                affinity=[0.5, 0.5, 0.5, 0.5, 0.5, 0.5, float("inf")],
            )
        with pytest.raises(ValidationError):
            EchoConfig(
                role="cfo",
                persona="CFO persona",
                affinity=[True, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],  # type: ignore[list-item]
            )

        with pytest.raises(ValidationError):
            InfluenceEdgeConfig(source="cfo", target="cto", weight=float("nan"))

        base_resources = {"owb": ResourceConfig(initial=80.0)}
        base_echoes = [EchoConfig(role="cfo", persona="CFO persona", affinity=[0.5] * 7)]

        with pytest.raises(ValidationError):
            QuestConfig(
                id="bad_gk",
                name="Bad GK",
                resources=base_resources,
                echoes=base_echoes,
                gatekeeper_route_map={"cfo": [15.0, 5.0]},
            )
        with pytest.raises(ValidationError):
            QuestConfig(
                id="bad_gk_neg",
                name="Bad GK Neg",
                resources=base_resources,
                echoes=base_echoes,
                gatekeeper_route_map={"cfo": [-1.0, 5.0]},
            )


# ---------------------------------------------------------------------------
# 5. Simulator skill tools (prepare/spawn, advance_tick, compile_results)
# ---------------------------------------------------------------------------


class TestSimulatorSkillToolsR4:
    """Verify ADK State _delta hygiene and defensive aggregation in simulator skills."""

    @pytest.mark.asyncio
    async def test_spawn_runners_marks_simulation_config_in_state_delta_and_sanitizes_echoes(self):
        """spawn_runners copies simulation_config into ADK State._delta and skips invalid echo entries."""
        prep_tools = _load_module_from_path(
            "sim_prep_tools_r4",
            "agents/simulator/skills/preparing-the-race/tools.py",
        )
        adk_state = State(
            {
                "simulation_ready": True,
                "runner_count": 5,
                "runner_type": "runner_autopilot",
                "simulation_config": {"runner_count": 5, "total_race_hours": 4.0},
                "quest_config": {
                    "echoes": [
                        "invalid-entry",
                        {"role": "cfo", "tier": True},  # bool tier falls back to 2
                    ]
                },
            },
            {},
        )
        ctx = _make_tool_context(adk_state, "sim-spawn-r4")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "sessions": [
                    {"sessionId": "", "agentType": "runner_autopilot"},
                    {"sessionId": "r-1", "agentType": "runner_autopilot"},
                    {"sessionId": "e-1", "agentType": "ai_echo"},
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

        with (
            patch("aiohttp.ClientSession", return_value=mock_client_cm),
            patch.object(prep_tools.oidc_auth, "get_id_token", return_value=None),
        ):
            res = await prep_tools.spawn_runners(count=5, tool_context=ctx)

        assert res["status"] == "success"
        assert adk_state["runner_count"] == 1
        assert adk_state["runner_session_ids"] == ["r-1"]
        assert adk_state["echo_session_ids"] == ["e-1"]
        assert "simulation_config" in adk_state._delta
        assert adk_state._delta["simulation_config"]["runner_count"] == 1

    @pytest.mark.asyncio
    async def test_advance_tick_filters_bool_ticks_handles_none_sets_and_updates_traffic_delta(self):
        """advance_tick guards bool/unhashable inputs, keeps 0.0 weather modifier, and records traffic_model delta."""
        tick_tools = _load_module_from_path(
            "sim_tick_tools_r4",
            "agents/simulator/skills/advancing-race-ticks/tools.py",
        )
        adk_state = State(
            {
                "simulation_ready": True,
                "current_tick": 1,
                "max_ticks": 10,
                "runner_session_ids": ["r1"],
                "echo_session_ids": ["e1", "e2"],
                "finished_runner_ids": None,
                "collapsed_runner_ids": None,
                "influence_network": None,
                "echo_voice_log": [],
                "route_plan": {
                    "elevation_profile": [0.0, 0.0, 0.0],
                    "distance_index": {"total_distance_mi": 26.2, "segments": []},
                },
                "traffic_model": {
                    "segment_index": [
                        {
                            "road_name": "Main St",
                            "start_mi": 2.0,
                            "end_mi": 5.0,
                            "coordinates": [[-115.17, 36.11], [-115.18, 36.12]],
                        }
                    ],
                    "ticks_closed": {},
                },
                "simulation_config": {
                    "total_race_hours": 2.0,
                    "tick_interval_seconds": 0.0,
                },
                "weather_modifier": 0.0,
                "quest_config": {"id": "budget_cuts", "resources": {}, "principles": ["P1"], "echoes": []},
            },
            {},
        )
        ctx = _make_tool_context(adk_state, "sim-adv-r4")

        mock_collector = MagicMock()
        mock_collector.drain = AsyncMock(
            return_value=[
                # Bool tick=True must NOT match current_tick=1!
                {
                    "session_id": {"unhashable": 1},
                    "payload": {"tool_name": "process_tick", "result": {"tick": True, "runner_status": "running"}},
                },
                # Unhashable dict session_id on echo msg + non-str truthy raw_role + two echoes same tick
                {
                    "session_id": {"echo": 1},
                    "payload": {
                        "tool_name": "process_decision_round",
                        "result": {"tick": 1, "role": 123, "stance": "neutral", "sentiment": 0.1, "aligned": True},
                    },
                },
                {
                    "session_id": "e2",
                    "payload": {
                        "tool_name": "process_decision_round",
                        "result": {"tick": 1, "role": "cto", "stance": "supportive", "sentiment": 0.5, "aligned": True},
                    },
                },
            ]
        )
        published_events: list[dict] = []

        async def _capture_publish(payload_str: str, **_kwargs) -> None:
            published_events.append(json.loads(payload_str))

        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock()
        mock_redis.delete = AsyncMock()

        with (
            patch("agents.simulator.collector.RaceCollector.get", return_value=mock_collector),
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(tick_tools, "publish_to_runners", side_effect=_capture_publish),
            patch("agents.utils.redis_pool.get_shared_redis_client", return_value=mock_redis),
            patch.object(tick_tools, "emit_gateway_message", new=AsyncMock()),
        ):
            res = await tick_tools.advance_tick(tool_context=ctx)

        assert res["status"] == "success"
        # Bool tick=True runner message must be rejected as stale (not matching tick=1).
        assert res["runners_reporting"] == 0
        # Scalar 0.0 weather_modifier must be preserved as 0.0, not treated as falsy.
        assert published_events[0]["weather_modifier"] == 0.0
        # Hoisted echo_voice_log preserves both valid echoes on tick 1.
        assert len(adk_state["echo_voice_log"]) == 2
        # ADK State._delta includes updated traffic_model.
        assert "traffic_model" in adk_state._delta

    @pytest.mark.asyncio
    async def test_compile_results_handles_none_state_fields_and_nan_persona_progress(self, tmp_path: Path):
        """compile_results tolerates explicit None collections and sanitizes NaN in persona_avg_progress."""
        from agents.simulator.longitudinal import SimulationRunStore

        comp_tools = _load_module_from_path(
            "sim_comp_tools_r4",
            "agents/simulator/skills/completing-the-race/tools.py",
        )
        state = {
            "current_tick": 1,
            "max_ticks": 1,
            "simulation_config": {"total_race_hours": 2.0, "tick_interval_seconds": 0.0},
            "runner_session_ids": ["r1"],
            "vitals_trend_full": None,
            "echo_voice_log": None,
            "resources": None,
            "decision_log": None,
            "aligned_echo_ids": None,
            "defected_echo_ids": None,
            "contradiction_log": None,
            "alert_history": None,
            "triggered_black_swans": None,
            "tick_snapshots": [
                {
                    "tick": 1,
                    "runners_reporting": 1,
                    "avg_velocity": 6.0,
                    "avg_water": 90.0,
                    "avg_distance": 1.0,
                    "status_counts": {"running": 1},
                    "persona_avg_progress": {"elite": float("nan")},
                    "notable_events": [],
                }
            ],
        }
        with (
            patch.object(comp_tools, "store_simulation_data", new=AsyncMock()),
            patch.object(comp_tools, "SimulationRunStore", side_effect=lambda: SimulationRunStore(base_dir=tmp_path)),
        ):
            res = await comp_tools.compile_results(_make_tool_context(state, "sim-comp-r4"))

        assert res["status"] == "success"
        assert res["persona_avg_progress"]["elite"] == 0.0
        assert res["decision_log"] == []
        assert res["resources"] == {}

        # Longitudinal tracking OSError must return explicit status == "error"
        with (
            patch.object(comp_tools, "store_simulation_data", new=AsyncMock()),
            patch.object(comp_tools, "SimulationRunStore", side_effect=OSError("no disk")),
        ):
            err_res = await comp_tools.compile_results(_make_tool_context(dict(state), "sim-comp-r4"))
        assert err_res["status"] == "error"
        assert "Longitudinal tracking failed" in err_res["message"]
