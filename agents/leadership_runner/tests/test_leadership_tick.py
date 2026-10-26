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

"""Tests for process_leadership_tick physics engine."""

import pytest
from unittest.mock import MagicMock

from agents.leadership_runner.leadership_tick import process_leadership_tick, get_vitals


def _make_tool_context(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    ctx.session = MagicMock()
    ctx.session.id = "test-stakeholder-001"
    return ctx


def _base_state(**overrides) -> dict:
    """Valid starting state for a leadership stakeholder."""
    state = {
        "velocity": 1.0,
        "distance": 0.0,
        "water": 95.0,
        "exhausted": False,
        "collapsed": False,
        "finished": False,
        "runner_status": "running",
        "will_hit_wall": False,
        "wall_mi": 16.0,
        "wall_severity": 0.3,
        "hydration_efficiency": 0.8,
        "target_finish_minutes": 300.0,
        "crowd_responsiveness": 0.0,
        "wave_number": 0,
        "start_delay_minutes": 0.0,
        "_tick_params": {
            "tick": 1,
            "minutes_per_tick": 5.0,
            "elapsed_minutes": 5.0,
            "race_distance_mi": 26.2188,
            "collector_buffer_key": "",
        },
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_basic_tick_advances_distance():
    """A single tick should advance distance from 0."""
    state = _base_state()
    ctx = _make_tool_context(state)
    result = await process_leadership_tick(inner_thought="Let's begin.", tool_context=ctx)
    assert result["status"] == "success"
    assert result["tick"] == 1
    assert result["distance_mi"] > 0
    assert result["inner_thought"] == "Let's begin."


@pytest.mark.asyncio
async def test_political_capital_depletes():
    """Political capital should decrease after a tick."""
    state = _base_state(water=95.0)
    ctx = _make_tool_context(state)
    await process_leadership_tick(inner_thought="Spending capital.", tool_context=ctx)
    assert ctx.state["water"] < 95.0


@pytest.mark.asyncio
async def test_resistance_cliff_reduces_momentum():
    """When past the resistance cliff, effective velocity should drop."""
    state_no_cliff = _base_state(distance=18.0, will_hit_wall=False)
    ctx1 = _make_tool_context(state_no_cliff)
    r1 = await process_leadership_tick(inner_thought="No cliff.", tool_context=ctx1)

    state_with_cliff = _base_state(distance=18.0, will_hit_wall=True, wall_mi=16.0, wall_severity=0.4)
    ctx2 = _make_tool_context(state_with_cliff)
    r2 = await process_leadership_tick(inner_thought="Cliff hit.", tool_context=ctx2)

    assert r2["effective_velocity"] < r1["effective_velocity"]


@pytest.mark.asyncio
async def test_stall_at_low_capital():
    """Stakeholder should be stalled when capital drops below threshold."""
    state = _base_state(water=30.0)
    ctx = _make_tool_context(state)
    result = await process_leadership_tick(inner_thought="Running low.", tool_context=ctx)
    assert result["exhausted"] is True
    assert result["runner_status"] == "exhausted"


@pytest.mark.asyncio
async def test_deadlock_at_critical_capital():
    """Stakeholder should be deadlocked when capital is critically low."""
    state = _base_state(water=10.0, exhausted=True)
    ctx = _make_tool_context(state)
    result = await process_leadership_tick(inner_thought="No capital left.", tool_context=ctx)
    assert result["collapsed"] is True
    assert result["runner_status"] == "collapsed"


@pytest.mark.asyncio
async def test_alignment_completion():
    """When distance reaches consensus distance, stakeholder should be aligned."""
    state = _base_state(distance=26.0, velocity=2.0)
    ctx = _make_tool_context(state)
    result = await process_leadership_tick(inner_thought="Almost there.", tool_context=ctx)
    assert result["runner_status"] == "finished"
    assert ctx.state["finished"] is True


@pytest.mark.asyncio
async def test_already_aligned_noop():
    """Already aligned stakeholders should return immediately."""
    state = _base_state(finished=True, distance=26.2188)
    ctx = _make_tool_context(state)
    result = await process_leadership_tick(inner_thought="Done.", tool_context=ctx)
    assert result["runner_status"] == "finished"
    assert result["mi_this_tick"] == 0.0


@pytest.mark.asyncio
async def test_already_deadlocked_noop():
    """Already deadlocked stakeholders should return immediately."""
    state = _base_state(collapsed=True, water=5.0)
    ctx = _make_tool_context(state)
    result = await process_leadership_tick(inner_thought="Failed.", tool_context=ctx)
    assert result["runner_status"] == "collapsed"
    assert result["mi_this_tick"] == 0.0


@pytest.mark.asyncio
async def test_no_tick_params_returns_error():
    """Missing tick params should return an error."""
    state = _base_state()
    del state["_tick_params"]
    ctx = _make_tool_context(state)
    result = await process_leadership_tick(inner_thought="", tool_context=ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_black_swan_reduces_momentum():
    """Black swan multiplier should reduce effective velocity."""
    state_normal = _base_state(distance=10.0)
    ctx1 = _make_tool_context(state_normal)
    r1 = await process_leadership_tick(inner_thought="Normal.", tool_context=ctx1)

    state_swan = _base_state(distance=10.0, black_swan_multiplier=0.5)
    ctx2 = _make_tool_context(state_swan)
    r2 = await process_leadership_tick(inner_thought="Crisis.", tool_context=ctx2)

    assert r2["effective_velocity"] < r1["effective_velocity"]


@pytest.mark.asyncio
async def test_black_swan_decays():
    """Black swan multiplier should decay back toward 1.0."""
    state = _base_state(black_swan_multiplier=0.6)
    ctx = _make_tool_context(state)
    await process_leadership_tick(inner_thought="Recovering.", tool_context=ctx)
    assert ctx.state["black_swan_multiplier"] > 0.6


@pytest.mark.asyncio
async def test_get_vitals():
    """get_vitals should return current state."""
    state = _base_state()
    ctx = _make_tool_context(state)
    result = await get_vitals(ctx)
    assert result["status"] == "success"
    assert result["velocity"] == 1.0
    assert result["water"] == 95.0
