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

"""Regression tests for leadership_runner and leadership_runner_autopilot bug fixes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.leadership_runner.agent import _stakeholder_before_agent_callback
from agents.leadership_runner.leadership_tick import process_leadership_tick
from agents.leadership_runner_autopilot.autopilot import handle_tick


class TestLeadershipRunnerFixes:
    """Verify tick-0 runner_count extraction, autopilot _tick_params, and finish fraction clamping."""

    @pytest.mark.asyncio
    async def test_before_agent_uses_runner_count_from_first_tick_broadcast(self):
        state: dict = {}
        ctx = MagicMock()
        ctx.state = state
        ctx.session = MagicMock()
        ctx.session.id = "stakeholder-1"
        part = MagicMock()
        part.text = json.dumps(
            {
                "event": "tick",
                "tick": 0,
                "minutes_per_tick": 0.0,
                "elapsed_minutes": 0.0,
                "race_distance_mi": 26.2,
                "runner_count": 12,
                "weather_modifier": 0.8,
                "simulation_id": "sim-lead-1",
            }
        )
        content = MagicMock()
        content.parts = [part]
        ctx.user_content = content

        with patch(
            "agents.leadership_runner.agent.initialize_leadership_stakeholder",
            wraps=__import__(
                "agents.leadership_runner.initialization",
                fromlist=["initialize_leadership_stakeholder"],
            ).initialize_leadership_stakeholder,
        ) as spy_init:
            await _stakeholder_before_agent_callback(ctx)

        spy_init.assert_called_once_with(state, "stakeholder-1", 12)
        assert state["_tick_params"]["weather_modifier"] == 0.8
        assert state["_tick_params"]["simulation_id"] == "sim-lead-1"

    def test_autopilot_populates_tick_params_and_reads_state_runner_count_fallback(self):
        state: dict = {"runner_count": 6}
        station_index = {"water_stations": [5.0, 10.0], "medical_tents": [8.0]}
        handle_tick(
            state,
            {
                "_session_id": "auto-stakeholder-1",
                "tick": 1,
                "minutes_per_tick": 12.0,
                "elapsed_minutes": 12.0,
                "race_distance_mi": 26.2,
                "station_index": station_index,
                "weather_modifier": 0.75,
                "route_friction_map": {"cfo": 7.5},
                "gatekeeper_route_map": {"cfo": [4.0, 8.0]},
                "simulation_id": "sim-auto-lead-1",
            },
        )
        assert state["velocity"] is not None
        tp = state["_tick_params"]
        assert tp["station_index"] == station_index
        assert tp["weather_modifier"] == 0.75
        assert tp["route_friction_map"] == {"cfo": 7.5}
        assert tp["simulation_id"] == "sim-auto-lead-1"

    @pytest.mark.asyncio
    async def test_finish_time_clamps_fraction_when_already_at_or_beyond_goal(self):
        state = {
            "velocity": 1.0,
            "distance": 27.0,  # Already >= 26.2 while finished=False -> overshoot > progress_this_tick
            "water": 80.0,
            "comfort": 90.0,
            "exhausted": False,
            "collapsed": False,
            "finished": False,
            "persona": "pragmatist",
            "_tick_params": {
                "tick": 5,
                "minutes_per_tick": 10.0,
                "elapsed_minutes": 50.0,
                "race_distance_mi": 26.2,
            },
        }
        ctx = MagicMock()
        ctx.state = state
        ctx.session = MagicMock()
        ctx.session.id = "clamp-test"

        res = await process_leadership_tick(
            minutes_per_tick=10.0,
            elapsed_minutes=50.0,
            race_distance_mi=26.2,
            tick=5,
            inner_thought="Crossing the finish line.",
            tool_context=ctx,
        )
        assert res["runner_status"] == "finished"
        # Clamping fraction to [0.0, 1.0] ensures finish_time stays within [40.0, 50.0]
        assert 40.0 <= state["finish_time_minutes"] <= 50.0

    @pytest.mark.asyncio
    async def test_process_leadership_tick_handles_scalar_dict_and_none_weather_modifier(self):
        """Scalar weather_modifier (0.75 / 1.0) and None must not crash with AttributeError."""
        base_state = {
            "velocity": 1.0,
            "distance": 2.0,
            "water": 90.0,
            "comfort": 90.0,
            "exhausted": False,
            "collapsed": False,
            "finished": False,
            "persona": "pragmatist",
        }
        # Neutral scalar (1.0) vs adverse scalar (0.5 -> higher capital drain)
        state_neutral = {
            **base_state,
            "_tick_params": {
                "tick": 1,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 15.0,
                "race_distance_mi": 26.2,
                "weather_modifier": 1.0,
            },
        }
        state_adverse = {
            **base_state,
            "_tick_params": {
                "tick": 1,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 15.0,
                "race_distance_mi": 26.2,
                "weather_modifier": 0.5,
            },
        }
        state_none = {
            **base_state,
            "_tick_params": {
                "tick": 1,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 15.0,
                "race_distance_mi": 26.2,
                "weather_modifier": None,
            },
        }
        for s in (state_neutral, state_adverse, state_none):
            ctx = MagicMock()
            ctx.state = s
            ctx.session = MagicMock()
            ctx.session.id = "weather-test"
            res = await process_leadership_tick(inner_thought="Advancing under weather.", tool_context=ctx)
            assert res["status"] == "success"

        # Adverse scalar weather drains strictly more political capital than neutral
        assert state_adverse["water"] < state_neutral["water"]
        assert state_none["water"] == pytest.approx(state_neutral["water"], abs=1e-4)

    @pytest.mark.asyncio
    async def test_process_leadership_tick_supports_list_segment_gatekeeper_route_map(self):
        """2-element list segments in gatekeeper_route_map must not raise TypeError."""
        state = {
            "velocity": 1.0,
            "distance": 5.0,
            "water": 90.0,
            "comfort": 90.0,
            "exhausted": False,
            "collapsed": False,
            "finished": False,
            "persona": "pragmatist",
            "_tick_params": {
                "tick": 2,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 30.0,
                "race_distance_mi": 26.2,
                "weather_modifier": 0.8,
                "route_friction_map": {"cfo": 8.0},
                "gatekeeper_route_map": {"cfo": [4.0, 8.0]},
            },
        }
        ctx = MagicMock()
        ctx.state = state
        ctx.session = MagicMock()
        ctx.session.id = "gk-list-test"

        res = await process_leadership_tick(inner_thought="Engaging CFO gatekeeper.", tool_context=ctx)
        assert res["status"] == "success"
        assert res["leadership_context"]["gatekeeper_engaged"] is True
        assert res["leadership_context"]["cliff_capital_cost"] > 0.0
        assert "cfo" in state["_cliff_charged_gatekeepers"]

    @pytest.mark.asyncio
    async def test_terminal_leadership_runner_pushes_to_collector_buffer(self):
        """Collapsed/aligned stakeholders must still direct-write RPUSH to collector_buffer_key."""
        from unittest.mock import AsyncMock

        state = {
            "velocity": 1.0,
            "distance": 12.0,
            "water": 0.0,
            "comfort": 50.0,
            "exhausted": True,
            "collapsed": True,
            "finished": False,
            "persona": "disruptor",
            "_tick_params": {
                "tick": 4,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 60.0,
                "race_distance_mi": 26.2,
                "collector_buffer_key": "collector:buf:lead-term",
            },
        }
        ctx = MagicMock()
        ctx.state = state
        ctx.session = MagicMock()
        ctx.session.id = "deadlocked-1"

        mock_redis = MagicMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.expire = AsyncMock()
        with patch("agents.leadership_runner.leadership_tick.get_shared_redis_client", return_value=mock_redis):
            res = await process_leadership_tick(inner_thought="Deadlocked.", tool_context=ctx)

        assert res["runner_status"] == "collapsed"
        mock_redis.rpush.assert_awaited_once()
        pushed_key, pushed_payload = mock_redis.rpush.await_args.args
        assert pushed_key == "collector:buf:lead-term"
        parsed = json.loads(pushed_payload)
        assert parsed["session_id"] == "deadlocked-1"
        assert parsed["payload"]["tool_name"] == "process_leadership_tick"
        assert parsed["payload"]["result"]["runner_status"] == "collapsed"

    @pytest.mark.asyncio
    async def test_terminal_result_includes_wave_number_progress_and_rounded_capital(self):
        """Terminal leadership_tick result must include wave_number, progress_this_tick, and rounded water/capital."""
        state = {
            "velocity": 1.0,
            "distance": 26.2188,
            "water": 42.3456,
            "comfort": 50.0,
            "exhausted": False,
            "collapsed": False,
            "finished": True,
            "wave_number": 3,
            "persona": "champion",
            "_tick_params": {
                "tick": 8,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 120.0,
                "race_distance_mi": 26.2188,
                "collector_buffer_key": "",
            },
        }
        ctx = MagicMock()
        ctx.state = state
        ctx.session = MagicMock()
        ctx.session.id = "aligned-schema-1"

        res = await process_leadership_tick(inner_thought="Aligned.", tool_context=ctx)
        assert res["wave_number"] == 3
        assert res["progress_this_tick"] == 0.0
        assert res["water"] == 42.3
        assert res["political_capital"] == 42.3

    @pytest.mark.asyncio
    async def test_venting_stop_on_finishing_tick_preserves_overshoot_interpolation(self):
        """Venting stop on the finishing tick must preserve raw_progress overshoot for finish_time_minutes."""
        state = {
            "velocity": 2.0,
            "distance": 25.0,
            "water": 95.0,
            "comfort": 20.0,  # < 40.0 triggers venting stop
            "exhausted": False,
            "collapsed": False,
            "finished": False,
            "persona": "champion",
            "will_hit_wall": False,
            "injury_risk": 0.0,
            "_tick_params": {
                "tick": 5,
                "minutes_per_tick": 30.0,
                "elapsed_minutes": 150.0,
                "race_distance_mi": 26.2188,
                "station_index": {"portable_toilets": [25.5]},
                "collector_buffer_key": "",
            },
        }
        ctx = MagicMock()
        ctx.state = state
        ctx.session = MagicMock()
        ctx.session.id = "vent-finish-1"

        res = await process_leadership_tick(inner_thought="Crossing finish.", tool_context=ctx)
        assert res["runner_status"] == "finished"
        assert res["finish_time_minutes"] is not None
        assert res["finish_time_minutes"] < 150.0


class TestLeadershipRunnerCapitalAndConfigHardening:
    """Regression tests for capital sanitization, model resolution, and port defaults."""

    @pytest.mark.asyncio
    async def test_executive_intervention_sanitizes_water_when_finished(self):
        from agents.leadership_runner.capital import executive_intervention

        ctx = MagicMock()
        ctx.state = {"finished": True, "water": float("nan")}
        res = await executive_intervention(tool_context=ctx)
        assert res["status"] == "already_finished"
        assert res["water"] == 100.0

    def test_leadership_runner_model_and_port_resolution(self, monkeypatch):
        from agents.leadership_runner.agent import DEFAULT_MODEL, _resolve_port
        from agents.leadership_runner_autopilot.agent import _resolve_port as _resolve_ap_port
        from agents.leadership_runner_autopilot.autopilot import HANDLERS
        from agents.utils.models import DEFAULT_FLASH_LITE_MODEL, get_model
        from agents.utils.runner_protocol import RunnerEventType

        assert DEFAULT_MODEL == DEFAULT_FLASH_LITE_MODEL
        monkeypatch.setenv("LEADERSHIP_RUNNER_MODEL", "   ")
        monkeypatch.setenv("RUNNER_MODEL", "")
        assert (
            get_model("LEADERSHIP_RUNNER_MODEL", DEFAULT_MODEL, fallback_env_vars=("RUNNER_MODEL",))
            == "gemini-3.5-flash-lite"
        )
        monkeypatch.setenv("PORT", "")
        monkeypatch.setenv("LEADERSHIP_RUNNER_PORT", "")
        monkeypatch.setenv("LEADERSHIP_RUNNER_AUTOPILOT_PORT", "")
        assert _resolve_port() == 9117
        assert _resolve_ap_port() == 9120
        assert RunnerEventType.HYDRATION_STATION in HANDLERS
