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

"""Round-3 regression tests covering bug fixes across simulator, runners, and echoes."""

import asyncio
import gc
import importlib
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
# 1. Collector fixes (env import safety, per-item drain, rollback & retention)
# ---------------------------------------------------------------------------


class TestCollectorR3:
    """Verify RaceCollector and _DrainProxy resilience."""

    def test_invalid_buffer_max_len_env_does_not_crash_import(self, monkeypatch):
        """Empty or non-numeric COLLECTOR_BUFFER_MAX_LEN falls back safely."""
        import agents.simulator.collector as collector_mod

        try:
            monkeypatch.setenv("COLLECTOR_BUFFER_MAX_LEN", "")
            reloaded = importlib.reload(collector_mod)
            assert reloaded._BUFFER_MAX_LEN >= 100

            monkeypatch.setenv("COLLECTOR_BUFFER_MAX_LEN", "not-an-int")
            reloaded = importlib.reload(collector_mod)
            assert reloaded._BUFFER_MAX_LEN >= 100

            monkeypatch.setenv("COLLECTOR_BUFFER_MAX_LEN", "10")
            reloaded = importlib.reload(collector_mod)
            assert reloaded._BUFFER_MAX_LEN == 100
        finally:
            monkeypatch.delenv("COLLECTOR_BUFFER_MAX_LEN", raising=False)
            importlib.reload(collector_mod)

    @pytest.mark.asyncio
    async def test_drain_skips_malformed_and_non_dict_items_without_dropping_batch(self):
        """One corrupt entry in Redis list must not drop valid telemetry entries."""
        from agents.simulator.collector import RaceCollector, _DrainProxy

        good = json.dumps({"session_id": "r1", "payload": {"tool_name": "process_tick", "result": {"tick": 1}}})
        raw_batch = [good.encode(), b"{broken json", json.dumps([1, 2, 3]).encode(), good.encode()]

        pipe = MagicMock()
        pipe.lrange = MagicMock()
        pipe.delete = MagicMock()
        pipe.execute = AsyncMock(return_value=[raw_batch, 1])
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=False)

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=pipe)

        with patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis):
            proxy_items = await _DrainProxy("sim-drain", mock_redis).drain()
            assert len(proxy_items) == 2

            collector = RaceCollector(session_id="sim-drain", runner_session_ids={"r1"})
            direct_items = await collector.drain()
            assert len(direct_items) == 2

    @pytest.mark.asyncio
    async def test_start_rollback_on_redis_set_failure_cleans_up_task_and_pubsub(self):
        """If active-key write raises during start(), task and pubsub are closed."""
        from agents.simulator import collector as collector_mod
        from agents.simulator.collector import RaceCollector, _DrainProxy

        mock_pubsub = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)
        mock_redis.set = AsyncMock(side_effect=RuntimeError("redis set boom"))
        mock_redis.delete = AsyncMock()

        async def _slow_get_message(**kwargs):
            await asyncio.sleep(10)
            return None

        mock_pubsub.get_message = _slow_get_message

        with (
            patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis),
            pytest.raises(RuntimeError, match="redis set boom"),
        ):
            await RaceCollector.start(session_id="sim-rollback", runner_session_ids={"r1"})

        mock_pubsub.close.assert_awaited()
        assert "sim-rollback" not in collector_mod._instances
        with patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis):
            assert isinstance(RaceCollector.get("sim-rollback"), _DrainProxy)

    @pytest.mark.asyncio
    async def test_start_retains_strong_reference_with_skip_pubsub_and_replaces_prior(self):
        """skip_pubsub collector stays registered after caller drops ref & GC runs."""
        from agents.simulator import collector as collector_mod
        from agents.simulator.collector import RaceCollector

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock()
        mock_redis.delete = AsyncMock()

        with patch("agents.simulator.collector.get_shared_redis_client", return_value=mock_redis):
            await RaceCollector.start(session_id="sim-strong", runner_session_ids={"r1"}, skip_pubsub=True)
            gc.collect()
            first = RaceCollector.get("sim-strong")
            assert isinstance(first, RaceCollector)

            # Re-starting same session cleanly replaces and stops first.
            second = await RaceCollector.start(session_id="sim-strong", runner_session_ids={"r2"}, skip_pubsub=True)
            assert first is not second
            assert RaceCollector.get("sim-strong") is second
            await second.stop()
            assert "sim-strong" not in collector_mod._instances


# ---------------------------------------------------------------------------
# 2. Live dashboard & simulator utility edge-case fixes
# ---------------------------------------------------------------------------


class TestSimulatorUtilitiesR3:
    """Verify NaN/Inf/None resilience across simulator helpers."""

    def test_live_dashboard_nan_inf_and_none_safety(self):
        """_bar, _score_bar, and build_live_dashboard survive NaN/Inf/None values."""
        from agents.simulator.live_dashboard import _bar, _score_bar, build_live_dashboard

        for bad in (float("nan"), float("inf"), float("-inf"), None, True):
            assert isinstance(_bar(bad, 100.0, width=10), str)  # type: ignore[arg-type]
            assert len(_bar(bad, 0.0, width=-3)) == 0  # type: ignore[arg-type]
            sb = _score_bar("sb-test", "Metric", bad, max_value=bad)  # type: ignore[arg-type]
            sb_json = json.dumps(sb, allow_nan=False)
            assert "sb-test" in sb_json

        payload = build_live_dashboard(
            tick=1,
            max_ticks=10,
            completion_pct=float("nan"),
            resource_state={
                "lau": {"current": None, "critical_threshold": None, "initial": None},
                "bt": "corrupt",  # type: ignore[dict-item]
                "tbu": {"current": float("nan"), "critical_threshold": float("inf")},
                "owb": {"current": 50.0, "critical_threshold": 20.0},
            },
            resource_deltas={"lau": {"delta": None}, "tbu": {"delta": float("nan")}},
            status_counts={"running": None, "finished": True},  # type: ignore[dict-item]
            persona_status_counts={},
            infrastructure={
                "active_gatekeepers": None,
                "avg_comfort": float("nan"),
                "friction_level": None,
            },
            active_alerts_count=None,  # type: ignore[arg-type]
            avg_velocity=float("nan"),
            avg_water=None,  # type: ignore[arg-type]
            voices=[None, "cfo: need clarity"],  # type: ignore[list-item]
        )
        serialized = json.dumps(payload, allow_nan=False)
        assert "NaN" not in serialized
        assert "Infinity" not in serialized

    def test_influence_network_guards_nan_and_negative_decay(self):
        """InfluenceNetwork/Edge stay finite under NaN/negative decay/corrupt inputs."""
        from agents.simulator.influence_network import InfluenceEdge, InfluenceNetwork

        net = InfluenceNetwork(
            edges=[
                InfluenceEdge(source="cfo", target="cto", weight=0.6),
            ]
        )
        net.edges[0].decay(-5.0)
        assert 0.0 <= net.edges[0].weight <= 1.0
        net.edges[0].decay(float("nan"))
        assert math.isfinite(net.edges[0].weight)

        deltas = net.propagate({"cfo": float("nan"), "cto": 0.4}, propagation_rate=float("inf"))
        assert all(math.isfinite(v) for v in deltas.values())

        bad_net = InfluenceNetwork.from_dict_list(
            [{"source": "cfo", "target": "cto", "weight": float("nan"), "original_weight": float("inf")}]
        )
        assert len(bad_net.edges) == 1
        assert math.isfinite(bad_net.edges[0].weight)

    @pytest.mark.asyncio
    async def test_broadcast_handles_non_utf8_bytes(self):
        """wait_for_runners_ready decodes invalid UTF-8 payloads safely."""
        from agents.simulator.broadcast import wait_for_runners_ready

        mock_redis = MagicMock()
        mock_redis.mget = AsyncMock(return_value=[b"\xff\xfe", b"bad"])
        with patch("agents.simulator.broadcast.get_shared_redis_client", return_value=mock_redis):
            result = await wait_for_runners_ready(["r1", "r2"], "sim-nonutf8", timeout_seconds=0.05)
        assert mock_redis.mget.await_count >= 1
        assert result == 0

        mock_redis.mget = AsyncMock(return_value=[b"\xff\xfe", b"sim-nonutf8"])
        with patch("agents.simulator.broadcast.get_shared_redis_client", return_value=mock_redis):
            partial = await wait_for_runners_ready(["r1", "r2"], "sim-nonutf8", timeout_seconds=0.05)
        assert mock_redis.mget.await_count >= 1
        assert partial == 1

    def test_tick_and_pre_race_callbacks_handle_invalid_values(self):
        """tick_callback and pre_race_callback survive corrupted state types."""
        from agents.simulator.pre_race_callback import pre_race_callback
        from agents.simulator.tick_callback import tick_callback

        ctx = SimpleNamespace(
            state={"current_tick": "not-an-int", "max_ticks": True},
        )
        req = LlmRequest(model="test-model", contents=[])
        res = tick_callback(ctx, req)  # type: ignore[arg-type]
        assert res is not None

        pre_ctx = SimpleNamespace(
            state={"_pre_race_phase": "spawn", "runner_count": None, "runner_type": None},
        )
        pre_req = LlmRequest(model="test-model", contents=[types.Content(role="user", parts=[types.Part.from_text(text="{}")])])
        pre_res = pre_race_callback(pre_ctx, pre_req)  # type: ignore[arg-type]
        assert pre_res is not None

    def test_quest_loader_rejects_path_traversal_and_resets_influence_network(self):
        """load_quest_from_yaml blocks path traversal and inject resets influence_network."""
        from agents.simulator.quest_loader import inject_quest_into_state, load_quest_from_yaml

        with pytest.raises(ValueError):
            load_quest_from_yaml("../secrets")

        cfg = load_quest_from_yaml("budget_cuts")
        state = State({"influence_network": [{"source": "stale", "target": "cfo"}]}, {})
        inject_quest_into_state(state, cfg)
        assert state["influence_network"] is None
        assert state.has_delta()


# ---------------------------------------------------------------------------
# 3. Truth engine, ECSI, decision classifier, ARI, coaching, longitudinal
# ---------------------------------------------------------------------------


class TestAnalyticsAndStateDeltaR3:
    """Verify ADK State delta updates and mathematical/edge guards."""

    def test_truth_engine_adk_state_delta_and_critical_responder_priority(self):
        """update_state_with_* must assign to ADK State so _delta captures mutations."""
        from agents.simulator.truth_engine import (
            Contradiction,
            extract_commitments,
            find_best_responder,
            update_state_with_commitments,
            update_state_with_contradictions,
        )

        principles = [
            "Strategic Foresight & Vision Cascading",
            "Adaptive Decision-Making & Agility",
            "Stakeholder Engagement & Inclusive Communication",
            "Ethical Leadership & Governance",
        ]
        extracted = extract_commitments("I commit to protecting ethical governance.", 1, principles)
        assert len(extracted) >= 1

        adk_state = State({"declared_commitments": [], "contradiction_log": []}, {})
        update_state_with_commitments(adk_state, extracted)
        assert "declared_commitments" in adk_state._delta
        assert len(adk_state._delta["declared_commitments"]) == len(extracted)

        update_state_with_contradictions(
            adk_state,
            [
                Contradiction(
                    commitment_text="c",
                    violated_principle="Ethical Leadership & Governance",
                    violated_principle_index=3,
                    evidence="d",
                    tick_detected=2,
                    tick_declared=1,
                    severity="critical",
                )
            ],
        )
        assert len(adk_state._delta["contradiction_log"]) == 1

        # Critical severity selects highest-affinity responder and skips None affinities safely.
        echoes = [
            {"role": "a", "affinity": None},
            {"role": "executive_coach", "affinity": [0.1, 0.2, 0.3, 0.9]},
        ]
        responder = find_best_responder(
            [
                Contradiction(
                    commitment_text="c",
                    violated_principle="Ethical Leadership & Governance",
                    violated_principle_index=3,
                    evidence="d",
                    tick_detected=2,
                    tick_declared=1,
                    severity="critical",
                )
            ],
            echoes,
        )
        assert responder == "executive_coach"
        assert extract_commitments(None, 1, []) == []  # type: ignore[arg-type]

    def test_ecsi_and_decision_classifier_unknown_resources_and_nan(self):
        """ECSI and Decision Classifier handle unknown/None/NaN resources safely."""
        from agents.simulator.decision_classifier import (
            _eval_condition,
            apply_resource_impacts,
        )
        from agents.simulator.ecsi import detect_contention

        assert _eval_condition("NONEXISTENT_RES < 20", {"lau": {"current": 50}}) is False

        resources: dict = {"lau": {"current": None, "critical_threshold": 15}, "owb": "corrupt"}
        deltas = apply_resource_impacts(resources, {"lau": float("nan"), "unknown": -10})
        assert math.isfinite(resources["lau"]["current"])
        assert isinstance(deltas, dict)

        alerts = detect_contention(
            tick_snapshot={
                "tick": 5,
                "echo_sentiments": {"cfo": {"sentiment": float("nan"), "stance": None}},
                "echo_actions": [{"role": "cfo", "action": None, "resource_claims": ["lau", "lau"]}],
            },
            quest_config={"resources": {"lau": {"initial": 80}}, "principles": []},
            resource_state={"lau": {"current": None, "critical_threshold": None}},
            state={"tick_snapshots": None, "echo_configs": None, "revealed_agendas": None},
        )
        assert isinstance(alerts, list)

    def test_ari_coaching_and_longitudinal_safety(self):
        """ARI clamps to [0,1], coaching dedups list vs tuple signatures, longitudinal survives NaN."""
        from agents.simulator.ari import build_ari_a2ui_card, compute_ari_dimensions
        from agents.simulator.coaching import build_coaching_card, generate_micro_feedback
        from agents.simulator.longitudinal import (
            SimulationRunRecord,
            compute_learning_trajectory,
            compute_peer_benchmarks,
            replay_from_tick,
        )

        dims = compute_ari_dimensions(
            {
                "principle_scores": [float("nan"), None, 20.0],
                "contradiction_log": [None, "bad"],
                "decision_log": None,
                "_echo_states": None,
                "tick_snapshots": None,
                "resources": {"lau": {"current": float("inf"), "initial": 0}},
            }
        )
        for key in ("o", "j", "d", "s", "ari"):
            assert 0.0 <= dims[key] <= 1.0
        assert compute_ari_dimensions(None)["zone"] == "Dead Zone"  # type: ignore[arg-type]

        card = build_ari_a2ui_card(dims)
        assert isinstance(card, dict) and "surfaceUpdate" in card

        fb_line = generate_micro_feedback(
            decision_tags={"principles_neglected": ["Ethical Leadership & Governance"]},
            resource_state={"lau": {"current": 10, "critical_threshold": 20}},
            alerts=[],
            tick=1,
        )
        assert isinstance(fb_line, str) and fb_line

        # Signature dedup must match JSON-deserialized list against tuple signature.
        c_card1 = build_coaching_card(
            resource_state={"lau": {"current": 10, "critical_threshold": 20}},
            decision_tags={"principles_neglected": ["Ethical Leadership & Governance"]},
            alerts=[],
            tick=1,
            max_ticks=10,
        )
        assert c_card1 is not None and "signature" in c_card1
        c_card2 = build_coaching_card(
            resource_state={"lau": {"current": 10, "critical_threshold": 20}},
            decision_tags={"principles_neglected": ["Ethical Leadership & Governance"]},
            alerts=[],
            tick=2,
            max_ticks=10,
            last_coaching_signature=list(c_card1["signature"]),  # type: ignore[arg-type]
        )
        assert c_card2 is None

        rec = SimulationRunRecord.from_session_state(
            {
                "quest_id": "q1",
                "principle_scores": [float("nan"), 10.0, None],
                "resources": {"lau": {"current": None}},
                "tick_snapshots": None,
                "vitals_trend_full": None,
            }
        )
        assert math.isfinite(rec.overall_score)
        assert all(math.isfinite(x) for x in rec.principle_scores)
        replayed = replay_from_tick(
            [{"tick": 0, "organizational_state": {"resources": {"lau": {"current": 50, "initial": 100, "regen_per_tick": 0}}}}],
            fork_tick=0,
            modified_decision_tags={"resource_impact_suggestion": {"lau": -10}},
        )
        assert "forked_trajectory" in replayed
        assert compute_peer_benchmarks(rec.principle_scores, [rec.to_dict()])["total_runs"] == 1
        assert compute_learning_trajectory([rec.to_dict()])["total_simulations"] == 1


# ---------------------------------------------------------------------------
# 4. Simulator skills & simulator_with_failure
# ---------------------------------------------------------------------------


class TestSimulatorSkillsAndFailureR3:
    """Verify ADK State updates in skills and reset on failure."""

    @pytest.mark.asyncio
    async def test_simulator_with_failure_resets_flags_before_raising(self):
        """prepare_simulation (failure variant) clears state flags before raise."""
        fail_tools = _load_module_from_path(
            "sim_fail_tools_r3",
            "agents/simulator_with_failure/skills/simulating-pre-race-failure/tools.py",
        )
        ctx = _make_tool_context({"simulation_ready": True, "simulation_in_progress": True})
        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(RuntimeError):
            await fail_tools.prepare_simulation(plan_json="{}", tool_context=ctx)
        assert ctx.state["simulation_ready"] is False
        assert ctx.state["simulation_in_progress"] is False

    @pytest.mark.asyncio
    async def test_preparing_the_race_resets_multi_round_state_and_rejects_nan(self):
        """prepare_simulation validates non-finite timing and resets _prev_medical_saves / influence_network."""
        prep_tools = _load_module_from_path(
            "sim_prep_tools_r3",
            "agents/simulator/skills/preparing-the-race/tools.py",
        )
        bad_plan = json.dumps(
            {
                "quest_id": "budget_cuts",
                "simulation_config": {"total_race_hours": float("nan")},
            }
        )
        # json.dumps produces NaN unless disallow_nan; pass string directly to simulate external NaN.
        bad_plan = '{"quest_id": "budget_cuts", "simulation_config": {"total_race_hours": 0}}'
        ctx_bad = _make_tool_context({})
        bad_res = await prep_tools.prepare_simulation(bad_plan, ctx_bad)
        assert bad_res["status"] == "error"

        adk_state = State(
            {
                "_prev_medical_saves": 99,
                "influence_network": [{"source": "stale", "target": "cfo"}],
            },
            {},
        )
        ctx_ok = _make_tool_context(adk_state)
        ok_res = await prep_tools.prepare_simulation(
            json.dumps({"quest_id": "budget_cuts", "simulation_config": None}),
            ctx_ok,
        )
        assert ok_res["status"] == "success"
        assert adk_state["_prev_medical_saves"] == {}
        assert adk_state["influence_network"] is None

    @pytest.mark.asyncio
    async def test_advancing_and_completing_race_adk_state_and_timeout_safety(self, tmp_path: Path):
        """advance_tick records ADK State deltas and completing handles NaN / Redis timeout."""
        adv_tools = _load_module_from_path(
            "sim_adv_tools_r3",
            "agents/simulator/skills/advancing-race-ticks/tools.py",
        )
        comp_tools = _load_module_from_path(
            "sim_comp_tools_r3",
            "agents/simulator/skills/completing-the-race/tools.py",
        )

        adk_state = State(
            {
                "current_tick": 0,
                "max_ticks": 5,
                "simulation_config": {"total_race_hours": 2.0, "tick_interval_seconds": 0.0},
                "runner_session_ids": ["r1"],
                "echo_session_ids": ["e1"],
                "simulation_id": "sim-adk",
                "runner_type": "leadership_runner",
                "resources": {"lau": {"current": 80, "initial": 80, "critical_threshold": 15}},
                "echo_configs": [{"role": "cfo", "domain_principles": []}],
                "influence_edges": [{"source": "cfo", "target": "cfo", "weight": 0.5}],
                "principles": ["Ethical Leadership & Governance"],
                "influence_network": None,
            },
            {},
        )
        drain_msgs = [
            {
                "session_id": "r1",
                "payload": {
                    "tool_name": "process_leadership_tick",
                    "result": {
                        "tick": 0,
                        "distance_mi": float("nan"),
                        "velocity": 1.0,
                        "water": 80.0,
                        "runner_status": "running",
                    },
                },
            },
            {
                "session_id": "e1",
                "payload": {
                    "tool_name": "process_decision_round",
                    "result": {
                        "tick": 0,
                        "role": "cfo",
                        "stance": "neutral",
                        "sentiment": 0.2,
                        "resistance": 0.3,
                        "aligned": True,
                        "wall_severity": float("nan"),
                    },
                },
            },
        ]

        mock_collector = MagicMock()
        mock_collector.drain = AsyncMock(return_value=drain_msgs)

        with (
            patch.object(adv_tools, "publish_to_runners", new=AsyncMock()),
            patch.object(adv_tools, "emit_gateway_message", new=AsyncMock()),
            patch.object(adv_tools.RaceCollector, "get", return_value=mock_collector),
        ):
            res = await adv_tools.advance_tick(_make_tool_context(adk_state, "sim-adk"))

        assert res["status"] == "success"
        assert "tick_snapshots" in adk_state._delta
        assert "vitals_trend_full" in adk_state._delta
        assert "echo_voice_log" in adk_state._delta
        assert "influence_network" in adk_state._delta
        assert adk_state["current_tick"] == 1

        # compile_results with NaN/None fields + stop_race_collector TimeoutError
        comp_state = {
            "simulation_id": "sim-timeout",
            "runner_count": 1,
            "simulation_config": {"race_distance_mi": None},
            "global_runner_states": {"r1": {"distance": float("nan"), "water": None, "status": None, "persona": None}},
            "principle_scores": [float("nan"), 1.0],
            "tick_snapshots": [
                {
                    "tick": 0,
                    "avg_velocity": float("nan"),
                    "avg_water": None,
                    "avg_distance": float("inf"),
                    "status_counts": {"running": 1},
                    "runners_reporting": 1,
                }
            ],
            "vitals_trend_full": None,
        }
        from agents.simulator.longitudinal import SimulationRunStore

        with (
            patch.object(comp_tools, "store_simulation_data", new=AsyncMock()),
            patch.object(comp_tools, "SimulationRunStore", side_effect=lambda: SimulationRunStore(base_dir=tmp_path)),
        ):
            comp_res = await comp_tools.compile_results(_make_tool_context(comp_state, "sim-timeout"))
        assert comp_res["status"] == "success"
        assert math.isfinite(comp_res["vitals_trend"][0]["avg_distance"])

        # Longitudinal tracking OSError must return explicit status == "error"
        with (
            patch.object(comp_tools, "store_simulation_data", new=AsyncMock()),
            patch.object(comp_tools, "SimulationRunStore", side_effect=OSError("read-only disk")),
        ):
            err_res = await comp_tools.compile_results(_make_tool_context(dict(comp_state), "sim-timeout"))
        assert err_res["status"] == "error"
        assert "Longitudinal tracking failed" in err_res["message"]

        mock_timeout_collector = MagicMock()
        mock_timeout_collector.stop = AsyncMock(side_effect=TimeoutError("timed out"))
        with (
            patch.object(comp_tools.RaceCollector, "get", return_value=mock_timeout_collector),
            patch("agents.simulator.broadcast.publish_end_simulation", new=AsyncMock()),
        ):
            stop_res = await comp_tools.stop_race_collector(_make_tool_context(comp_state, "sim-timeout"))
        assert stop_res["status"] == "success"
        assert comp_state["simulation_ready"] is False
        assert comp_state["simulation_in_progress"] is False


# ---------------------------------------------------------------------------
# 5. Leadership runner, Runner, and AI Echo edge-case fixes
# ---------------------------------------------------------------------------


class TestRunnersAndEchoesR3:
    """Verify edge-case guards across leadership_runner*, runner*, and ai_echo*."""

    def test_leadership_runner_constants_initialization_and_capital_guards(self):
        """Persona helper, init, and capital tools handle None/NaN/bool/<=0 cleanly."""
        from agents.leadership_runner.capital import deplete_capital, restore_capital
        from agents.leadership_runner.constants import PERSONA_PROFILES, get_persona_profile
        from agents.leadership_runner.initialization import initialize_leadership_stakeholder

        p = get_persona_profile(None)
        assert p == PERSONA_PROFILES["pragmatist"]
        p["momentum_scale"] = 999.0
        assert PERSONA_PROFILES["pragmatist"]["momentum_scale"] != 999.0

        state: dict = {"persona": "  UNKNOWN_PERSONA  "}
        initialize_leadership_stakeholder(state, "sess-lead", runner_count=0)
        assert state["persona"] in PERSONA_PROFILES
        assert math.isfinite(state["velocity"])

        ctx = _make_tool_context({"water": None, "_tick_params": None, "_capital_restored_window": ["bad"]})
        d_res = asyncio.run(deplete_capital(float("nan"), ctx))
        assert math.isfinite(d_res["water"])
        r_res = asyncio.run(restore_capital(float("inf"), ctx))
        assert math.isfinite(r_res["water"])

    def test_leadership_tick_and_autopilot_guards(self):
        """leadership_tick and leadership_runner_autopilot handle NaN, None, and missing fields."""
        from agents.leadership_runner.leadership_tick import (
            _progress_in_gatekeeper_zone,
            accelerate,
            brake,
            process_leadership_tick,
        )
        from agents.leadership_runner_autopilot.autopilot import (
            _pick_inner_thought,
            autopilot_leadership_callback,
            build_summary,
            extract_last_user_text,
        )

        assert _progress_in_gatekeeper_zone(float("nan"), [0.0, 10.0]) is False
        assert _progress_in_gatekeeper_zone(5.0, float("nan")) is False

        ctx = _make_tool_context({"velocity": None, "water": None, "_tick_params": None})
        assert math.isfinite(asyncio.run(accelerate(float("nan"), ctx))["velocity"])
        assert math.isfinite(asyncio.run(brake(float("inf"), ctx))["velocity"])

        bad_tick = asyncio.run(
            process_leadership_tick(ctx, "thinking", minutes_per_tick=5.0, elapsed_minutes=5.0, race_distance_mi=0.0, tick=1)
        )
        assert bad_tick["status"] == "error"

        assert extract_last_user_text(LlmRequest(model="m", contents=[])) == ""
        assert isinstance(_pick_inner_thought({"persona": None}, None), str)  # type: ignore[arg-type]
        assert "Status:" in build_summary({"velocity": None, "water": None, "distance": None}).content.parts[0].text  # type: ignore[union-attr]

        cb_ctx = SimpleNamespace(
            state={"runner_count": 4},
            session=SimpleNamespace(id=None),
        )
        tick_req = _make_llm_request(
            json.dumps({"event": "tick", "tick": 0, "runner_count": None, "minutes_per_tick": 0.0, "elapsed_minutes": 0.0, "race_distance_mi": 26.2})
        )
        resp = autopilot_leadership_callback(cb_ctx, tick_req)  # type: ignore[arg-type]
        assert resp.content is not None and resp.content.parts[0].function_call.name == "process_leadership_tick"  # type: ignore[union-attr]

    def test_runner_waves_hydration_init_running_and_autopilot_guards(self):
        """Runner physics, waves, hydration, and autopilot survive bad inputs cleanly."""
        from agents.runner.hydration import deplete_water, rehydrate
        from agents.runner.initialization import initialize_runner
        from agents.runner.running import accelerate, brake, process_tick
        from agents.runner.waves import compute_runner_wave, compute_wave
        from agents.runner_autopilot.autopilot import (
            autopilot_callback,
            build_summary,
            handle_crowd_boost,
        )

        assert compute_wave(-5, 0) == (0, 0.0)
        assert compute_wave(0, True) == (0, 0.0)  # type: ignore[arg-type]
        assert compute_runner_wave("s1", -3) == (0, 0.0)

        r_state: dict = {}
        initialize_runner(r_state, "r-init", runner_count=0)
        assert math.isfinite(r_state["velocity"])

        h_ctx = _make_tool_context({"water": None})
        assert asyncio.run(deplete_water(-20.0, h_ctx))["water"] == 100.0
        assert math.isfinite(asyncio.run(deplete_water(float("nan"), h_ctx))["water"])
        assert asyncio.run(rehydrate(float("inf"), h_ctx))["water"] == 100.0

        run_ctx = _make_tool_context({"velocity": None, "water": None, "crowd_responsiveness": None, "_tick_params": None})
        assert math.isfinite(asyncio.run(accelerate(float("nan"), run_ctx))["velocity"])
        assert math.isfinite(asyncio.run(brake(float("inf"), run_ctx))["velocity"])
        bad_run = asyncio.run(
            process_tick(run_ctx, "go", minutes_per_tick=5.0, elapsed_minutes=5.0, race_distance_mi=0.0, tick=1)
        )
        assert bad_run["status"] == "error"

        assert handle_crowd_boost({}, {"intensity": float("nan")}).content.parts[0].function_call.args["intensity"] == 0.5  # type: ignore[union-attr]
        assert "Status:" in build_summary({"velocity": None, "water": None, "distance": None}).content.parts[0].text  # type: ignore[union-attr]

        ap_ctx = SimpleNamespace(state={}, session=SimpleNamespace(id=None))
        ap_req = _make_llm_request(json.dumps({"event": "tick", "tick": 0, "runner_count": None}))
        ap_resp = autopilot_callback(ap_ctx, ap_req)  # type: ignore[arg-type]
        assert ap_resp.content is not None and ap_resp.content.parts[0].function_call.name == "process_tick"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_ai_echo_suite_handles_corrupt_json_none_and_nan(self):
        """AI Echo init, process_decision_round, prompts, autopilot, and reactive callbacks survive bad payloads."""
        from agents.ai_echo.coaching_prompts import (
            get_coaching_prompt,
            get_contradiction_prompt,
            get_resource_crisis_prompt,
        )
        from agents.ai_echo.initialization import initialize_echo
        from agents.ai_echo.process_decision_round import process_decision_round
        from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback
        from agents.ai_echo_reactive.callback import reactive_echo_callback

        state: dict = {}
        initialize_echo(
            state,
            "echo-1",
            {
                "role": None,
                "tier": None,
                "affinity": [float("nan"), None],
                "domain_principles": None,
                "initial_sentiment": float("nan"),
            },
        )
        assert state["role"] == "unknown"
        assert isinstance(state["tier"], int)
        assert len(state["principle_affinity"]) == 7

        res = process_decision_round(
            state=state,
            tick=None,  # type: ignore[arg-type]
            decision_tags={"principles_applied": None, "principles_neglected": None},
            resource_state={"owb": None},
            wall_severity=float("nan"),
            influence_deltas={"unknown": float("nan")},
        )
        assert math.isfinite(res["sentiment"])
        assert math.isfinite(res["wall_severity"])

        assert isinstance(get_coaching_prompt(None, None), str)  # type: ignore[arg-type]
        assert isinstance(get_contradiction_prompt(None, None), str)  # type: ignore[arg-type]
        assert isinstance(get_resource_crisis_prompt(None, None), str)  # type: ignore[arg-type]

        # Non-dict JSON list must not raise AttributeError in autopilot or reactive callbacks.
        bad_json_req = _make_llm_request("[1, 2, 3]")
        echo_ctx = SimpleNamespace(state={"sentiment": None, "stance": None}, session=SimpleNamespace(id=None))
        auto_bad_resp = await autopilot_echo_callback(echo_ctx, bad_json_req)  # type: ignore[arg-type]
        react_bad_resp = await reactive_echo_callback(echo_ctx, bad_json_req)  # type: ignore[arg-type]
        assert auto_bad_resp is not None and auto_bad_resp.content is not None and auto_bad_resp.content.parts
        assert react_bad_resp is not None and react_bad_resp.content is not None and react_bad_resp.content.parts

        # None decision_tags / principles_applied fallback to deterministic tool call cleanly.
        tick_req = _make_llm_request(
            json.dumps(
                {
                    "event": "decision_round",
                    "tick": 1,
                    "decision_tags": {"principles_applied": None, "principles_neglected": None},
                    "resource_state": {"owb": None},
                }
            )
        )
        det_resp = await reactive_echo_callback(
            SimpleNamespace(state={"sentiment": 0.1, "stance": "neutral", "domain_principles": None}, session=None),  # type: ignore[arg-type]
            tick_req,
        )
        assert det_resp is not None and det_resp.content is not None and det_resp.content.parts
