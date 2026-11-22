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

"""Regression tests for runner and runner_autopilot bug fixes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.runner.agent import _runner_before_agent_callback
from agents.runner.running import process_tick
from agents.runner_autopilot.autopilot import handle_tick


def _make_tool_context(state: dict, session_id: str = "runner-audit-1") -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    ctx.session = MagicMock()
    ctx.session.id = session_id
    return ctx


class TestRunnerComfortAndTickParams:
    """Verify comfort persistence after drinking and _tick_params population."""

    @pytest.mark.asyncio
    async def test_comfort_reduction_from_drinking_is_persisted_in_state(self):
        state = {
            "velocity": 1.0,
            "distance": 2.9,
            "water": 30.0,  # <= 40 guarantees drinking at water station
            "comfort": 90.0,
            "exhausted": False,
            "collapsed": False,
            "finished": False,
            "hydration_efficiency": 1.0,
            "_tick_params": {
                "tick": 1,
                "minutes_per_tick": 10.0,
                "elapsed_minutes": 10.0,
                "race_distance_mi": 26.2,
                "station_index": {"water_stations": [3.0]},
                "weather_modifier": 1.0,
            },
        }
        ctx = _make_tool_context(state)
        await process_tick(
            minutes_per_tick=10.0,
            elapsed_minutes=10.0,
            inner_thought="Grabbing water at mile 3.",
            tool_context=ctx,
        )
        # Comfort decays naturally over mi_this_tick AND drops an extra 15.0 from drinking
        assert state["comfort"] < 75.0

    @pytest.mark.asyncio
    async def test_runner_autopilot_populates_tick_params_for_stations_and_weather(self):
        state: dict = {}
        station_index = {"water_stations": [2.5, 5.0], "medical_tents": [4.0]}
        handle_tick(
            state,
            {
                "_session_id": "auto-runner-1",
                "tick": 1,
                "minutes_per_tick": 10.0,
                "elapsed_minutes": 10.0,
                "race_distance_mi": 26.2,
                "runner_count": 8,
                "station_index": station_index,
                "weather_modifier": 0.85,
                "simulation_id": "sim-weather-1",
                "collector_buffer_key": "buf:1",
            },
        )
        assert "_tick_params" in state
        assert state["_tick_params"]["station_index"] == station_index
        assert state["_tick_params"]["weather_modifier"] == 0.85
        assert state["_tick_params"]["simulation_id"] == "sim-weather-1"

    @pytest.mark.asyncio
    async def test_runner_before_agent_uses_tick_runner_count_and_extracts_weather(self):
        state: dict = {}
        ctx = MagicMock()
        ctx.state = state
        ctx.session = MagicMock()
        ctx.session.id = "llm-runner-1"
        part = MagicMock()
        part.text = json.dumps(
            {
                "event": "tick",
                "tick": 0,
                "minutes_per_tick": 0.0,
                "elapsed_minutes": 0.0,
                "race_distance_mi": 26.2,
                "runner_count": 25,
                "weather_modifier": 0.9,
                "simulation_id": "sim-llm-1",
            }
        )
        content = MagicMock()
        content.parts = [part]
        ctx.user_content = content

        await _runner_before_agent_callback(ctx)
        assert state["velocity"] is not None
        assert state["_tick_params"]["weather_modifier"] == 0.9
        assert state["_tick_params"]["simulation_id"] == "sim-llm-1"

    @pytest.mark.asyncio
    async def test_weather_modifier_modulates_effective_velocity(self):
        """Scalar or dict weather_modifier in _tick_params scales effective_velocity."""
        base = {
            "velocity": 1.0,
            "distance": 1.0,
            "water": 100.0,
            "comfort": 100.0,
            "exhausted": False,
            "collapsed": False,
            "finished": False,
            "hydration_efficiency": 1.0,
        }
        state_clear = {
            **base,
            "_tick_params": {
                "tick": 1,
                "minutes_per_tick": 10.0,
                "elapsed_minutes": 10.0,
                "race_distance_mi": 26.2,
                "weather_modifier": 1.0,
            },
        }
        state_headwind = {
            **base,
            "_tick_params": {
                "tick": 1,
                "minutes_per_tick": 10.0,
                "elapsed_minutes": 10.0,
                "race_distance_mi": 26.2,
                "weather_modifier": 0.8,
            },
        }
        res_clear = await process_tick(
            inner_thought="Clear skies.", tool_context=_make_tool_context(state_clear, "r-clear")
        )
        res_headwind = await process_tick(
            inner_thought="Headwind.", tool_context=_make_tool_context(state_headwind, "r-wind")
        )
        assert res_headwind["effective_velocity"] == pytest.approx(res_clear["effective_velocity"] * 0.8, abs=1e-3)

    @pytest.mark.asyncio
    async def test_terminal_runner_pushes_to_collector_buffer(self):
        """Collapsed/finished runners must still direct-write RPUSH to collector_buffer_key."""
        from unittest.mock import AsyncMock, patch

        state = {
            "velocity": 1.0,
            "distance": 14.0,
            "water": 0.0,
            "comfort": 50.0,
            "exhausted": True,
            "collapsed": True,
            "finished": False,
            "_tick_params": {
                "tick": 6,
                "minutes_per_tick": 10.0,
                "elapsed_minutes": 60.0,
                "race_distance_mi": 26.2,
                "collector_buffer_key": "collector:buf:runner-term",
            },
        }
        mock_redis = MagicMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.expire = AsyncMock()
        with patch("agents.runner.running.get_shared_redis_client", return_value=mock_redis):
            res = await process_tick(
                inner_thought="Out of race.", tool_context=_make_tool_context(state, "collapsed-runner-1")
            )

        assert res["runner_status"] == "collapsed"
        mock_redis.rpush.assert_awaited_once()
        pushed_key, pushed_payload = mock_redis.rpush.await_args.args
        assert pushed_key == "collector:buf:runner-term"
        parsed = json.loads(pushed_payload)
        assert parsed["session_id"] == "collapsed-runner-1"
        assert parsed["payload"]["tool_name"] == "process_tick"
        assert parsed["payload"]["result"]["runner_status"] == "collapsed"

    @pytest.mark.asyncio
    async def test_toilet_stop_reduces_hydration_depletion_and_terminal_includes_wave_number(self):
        """Toilet stop must adjust mi_this_tick before hydration depletion, and terminal result keeps wave_number."""
        base_state = {
            "velocity": 1.0,
            "distance": 5.0,
            "water": 90.0,
            "comfort": 20.0,  # < 40.0 triggers toilet stop when passing station
            "exhausted": False,
            "collapsed": False,
            "finished": False,
            "hydration_efficiency": 1.0,
            "will_hit_wall": False,
            "injury_risk": 0.0,
        }
        state_toilet = {
            **base_state,
            "_tick_params": {
                "tick": 2,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 30.0,
                "race_distance_mi": 26.2188,
                "station_index": {"portable_toilets": [5.5], "water_stations": [99.0]},
            },
        }
        state_no_toilet = {
            **base_state,
            "_tick_params": {
                "tick": 2,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 30.0,
                "race_distance_mi": 26.2188,
                "station_index": {"portable_toilets": [], "water_stations": [99.0]},
            },
        }

        res_toilet = await process_tick(
            inner_thought="Quick stop.", tool_context=_make_tool_context(state_toilet, "r-toilet")
        )
        res_no_toilet = await process_tick(
            inner_thought="No stop.", tool_context=_make_tool_context(state_no_toilet, "r-notoilet")
        )
        # Toilet stop reduces mi_this_tick by 10%, so less water is depleted
        assert res_toilet["mi_this_tick"] < res_no_toilet["mi_this_tick"]
        assert res_toilet["water"] > res_no_toilet["water"]

        term_state = {
            "velocity": 1.0,
            "distance": 26.2188,
            "water": 75.0,
            "exhausted": False,
            "collapsed": False,
            "finished": True,
            "wave_number": 2,
            "_tick_params": {
                "tick": 10,
                "minutes_per_tick": 15.0,
                "elapsed_minutes": 150.0,
                "race_distance_mi": 26.2188,
            },
        }
        term_res = await process_tick(
            inner_thought="Done.", tool_context=_make_tool_context(term_state, "r-term-wave")
        )
        assert term_res["wave_number"] == 2


class TestRunnerHydrationAndConfigHardening:
    """Regression tests for hydration guards and centralized model/port resolution."""

    @pytest.mark.asyncio
    async def test_hydration_tools_respect_finished_and_injury_risk_guards(self):
        from agents.runner.hydration import deplete_water, rehydrate

        finished_ctx = _make_tool_context({"finished": True, "water": 80.0}, "r-fin")
        assert (await deplete_water(25.0, finished_ctx))["status"] == "already_finished"
        assert finished_ctx.state["water"] == 80.0
        assert (await rehydrate(15.0, finished_ctx))["status"] == "already_finished"
        assert finished_ctx.state["water"] == 80.0

        injured_ctx = _make_tool_context(
            {"finished": False, "water": 20.0, "exhausted": True, "collapsed": True, "injury_risk": 0.9},
            "r-inj",
        )
        await rehydrate(50.0, injured_ctx)
        assert injured_ctx.state["water"] == 70.0
        assert injured_ctx.state["exhausted"] is False
        assert injured_ctx.state["collapsed"] is True

    def test_runner_model_and_port_resolution(self, monkeypatch):
        from agents.runner.agent import DEFAULT_MODEL, _resolve_port
        from agents.runner.constants import DEFAULT_MODEL as CONST_DEFAULT_MODEL
        from agents.runner_autopilot.agent import _resolve_port as _resolve_ap_port
        from agents.utils.models import DEFAULT_FLASH_LITE_MODEL, get_model

        assert DEFAULT_MODEL == DEFAULT_FLASH_LITE_MODEL
        assert CONST_DEFAULT_MODEL == DEFAULT_FLASH_LITE_MODEL
        monkeypatch.setenv("RUNNER_MODEL", "   ")
        assert get_model("RUNNER_MODEL", DEFAULT_MODEL) == "gemini-3.5-flash-lite"
        monkeypatch.setenv("PORT", "")
        monkeypatch.setenv("RUNNER_PORT", "")
        monkeypatch.setenv("RUNNER_AUTOPILOT_PORT", "")
        assert _resolve_port() == 9108
        assert _resolve_ap_port() == 9110
