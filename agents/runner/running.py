# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

import inspect
import json
import logging
import math
import random

from google.adk.tools.tool_context import ToolContext

from agents.utils.redis_pool import get_shared_redis_client

from agents.runner.constants import (
    BASE_DEPLETION_RATE,
    COLLAPSE_THRESHOLD,
    EXHAUSTION_THRESHOLD,
    FATIGUE_DEPLETION_GROWTH,
    HYDRATION_STATION_INTERVAL_MI,
    HYDRATION_STATION_REFILL,
    MAX_VELOCITY,
    MIN_FATIGUE_FACTOR,
    NATURAL_FATIGUE_RATE,
    SPEED_SCALE,
    runner_seed,
)

logger = logging.getLogger(__name__)


def _finite_float(value: object, default: float = 0.0) -> float:
    """Coerce numeric value to finite float, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: object, default: int = 0) -> int:
    """Coerce int/float value to int, rejecting bool/None/NaN/Inf."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        try:
            num = float(value)
        except OverflowError:
            return default
        return value if math.isfinite(num) else default
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(num):
        return default
    return int(num)


def _strip_nonfinite(obj: object) -> object:
    """Recursively replace non-finite floats (NaN/Inf) with None for RFC 8259 JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_nonfinite(v) for v in obj]
    return obj


async def accelerate(intensity: float, tool_context: ToolContext) -> dict:
    """Increase the runner's speed based on the specified intensity.

    Speed is reduced when dehydrated: at 0% water, max speed is halved.

    Args:
        intensity: Acceleration intensity (0.0 to 1.0).
        tool_context: ADK tool context for state management.
    """
    safe_intensity = max(0.0, _finite_float(intensity, 0.0))
    velocity = max(0.0, _finite_float(tool_context.state.get("velocity", 0.0), 0.0))
    water = max(0.0, min(100.0, _finite_float(tool_context.state.get("water", 100.0), 100.0)))
    crowd_responsiveness = max(0.0, _finite_float(tool_context.state.get("crowd_responsiveness", 0.5), 0.5))
    boost_multiplier = 0.1
    # Dehydration penalty: linearly scale from 1.0 (full water) to 0.5 (no water)
    hydration_factor = 0.5 + 0.5 * (water / 100.0)
    new_velocity = velocity + (safe_intensity * boost_multiplier * hydration_factor * crowd_responsiveness)
    new_velocity = max(0.0, min(MAX_VELOCITY, new_velocity))  # Hard cap
    tool_context.state["velocity"] = new_velocity
    logger.info(f"Runner accelerating: velocity={new_velocity:.2f} (hydration: {water:.0f}%)")
    return {
        "status": "success",
        "message": f"Accelerated to velocity={new_velocity:.2f} (hydration: {water:.0f}%).",
        "velocity": new_velocity,
    }


async def brake(intensity: float, tool_context: ToolContext) -> dict:
    """Decrease the runner's speed.

    Args:
        intensity: Braking intensity (0.0 to 1.0).
        tool_context: ADK tool context for state management.
    """
    safe_intensity = max(0.0, _finite_float(intensity, 0.0))
    velocity = max(0.0, _finite_float(tool_context.state.get("velocity", 0.0), 0.0))
    new_velocity = max(0.0, velocity - (safe_intensity * 1.5))
    tool_context.state["velocity"] = new_velocity
    logger.info(f"Runner braking: velocity={new_velocity:.2f}")
    return {
        "status": "success",
        "message": f"Braked to velocity={new_velocity:.2f}.",
        "velocity": new_velocity,
    }


async def get_vitals(tool_context: ToolContext) -> dict:
    """Get current runner vitals including speed, distance, hydration, and status.

    Args:
        tool_context: ADK tool context for state management.
    """
    vitals = {
        "status": "success",
        "message": "Vitals retrieved successfully.",
        "velocity": tool_context.state.get("velocity", 0.0),
        "distance": tool_context.state.get("distance", 0.0),
        "water": tool_context.state.get("water", 100.0),
        "exhausted": tool_context.state.get("exhausted", False),
        "collapsed": tool_context.state.get("collapsed", False),
    }
    logger.info(f"Runner vitals: {vitals}")
    return vitals


async def process_tick(
    tool_context: ToolContext,
    inner_thought: str,
    minutes_per_tick: float = -1.0,
    elapsed_minutes: float = -1.0,
    race_distance_mi: float = -1.0,
    tick: int = -1,
    collector_buffer_key: str = "",
) -> dict:
    """Advance the simulation by one tick and record your inner thought.

    Call this once per tick after setting your speed with accelerate or brake.
    Timing parameters (tick number, elapsed time, race distance) are provided
    automatically by the simulation -- you only need to supply inner_thought.

    ``inner_thought`` has no default value: ADK's auto-generated tool schema
    marks it as required, which materially improves small-model (e.g.
    gemma4:e2b) tool-call reliability. Optional fields are routinely
    dropped by 2-3B models.

    Args:
        tool_context: ADK tool context for state management.
        inner_thought: Short internal monologue (5 words max) about what
            the runner is thinking RIGHT NOW. Required (no default) so
            ADK marks it required in the tool schema -- small models
            reliably honor required fields and routinely drop optional
            ones. The autopilot path passes an empty string.
        minutes_per_tick: (Auto-provided) Simulated minutes per tick.
        elapsed_minutes: (Auto-provided) Total elapsed simulated time.
        race_distance_mi: (Auto-provided) Race distance in miles.
        tick: (Auto-provided) Current tick number.
        collector_buffer_key: (Auto-provided) Redis key for telemetry.

    Returns:
        Dict with runner vitals: distance, velocity, water, status.
    """
    state = tool_context.state

    # Read tick params from state if the caller didn't provide them.
    # Sentinel detection: ``-1`` / ``-1.0`` means "not provided." The
    # sentinel-default approach (vs. ``T | None = None``) is required to
    # work around an ADK schema-builder bug where union-with-None
    # parameter types cause ``required`` to be cleared on the entire tool
    # schema -- which lets small models (gemma4:e2b) drop the
    # ``inner_thought`` arg too. See Task I in
    # docs/plans/2026-04-19-llm-runner-cap-task-i-required-inner-thought.md.
    raw_tick_params = state.get("_tick_params")
    tick_params = raw_tick_params if isinstance(raw_tick_params, dict) else {}
    raw_tick = tick_params.get("tick") if tick < 0 else tick
    raw_minutes_per_tick = tick_params.get("minutes_per_tick") if minutes_per_tick < 0 else minutes_per_tick
    raw_elapsed_minutes = tick_params.get("elapsed_minutes") if elapsed_minutes < 0 else elapsed_minutes
    raw_race_distance_mi = tick_params.get("race_distance_mi") if race_distance_mi < 0 else race_distance_mi
    if not collector_buffer_key:
        raw_cbk = tick_params.get("collector_buffer_key")
        collector_buffer_key = raw_cbk if isinstance(raw_cbk, str) else ""

    # Validate we have all required params from either source.
    # Return an error dict (not raise) so the LLM can recover if it calls
    # process_tick before any tick event has been received (e.g. during spawn).
    if (
        raw_tick is None
        or raw_minutes_per_tick is None
        or raw_elapsed_minutes is None
        or raw_race_distance_mi is None
    ):
        logger.warning("process_tick called without tick params -- no tick event received yet")
        return {
            "status": "error",
            "message": (
                "No tick event received yet. Wait for the simulation to send a tick event before calling process_tick."
            ),
        }

    tick = _finite_int(raw_tick, -1)
    minutes_per_tick = _finite_float(raw_minutes_per_tick, -1.0)
    elapsed_minutes = _finite_float(raw_elapsed_minutes, -1.0)
    race_distance_mi = _finite_float(raw_race_distance_mi, -1.0)
    if tick < 0 or minutes_per_tick < 0.0 or elapsed_minutes < 0.0 or race_distance_mi <= 0.0:
        logger.warning(
            "process_tick called with invalid numeric params: tick=%s mpt=%s elapsed=%s dist=%s",
            tick,
            minutes_per_tick,
            elapsed_minutes,
            race_distance_mi,
        )
        return {
            "status": "error",
            "message": "Invalid tick timing parameters.",
        }

    velocity = max(0.0, _finite_float(state.get("velocity", 0.0), 0.0))
    distance = max(0.0, _finite_float(state.get("distance", 0.0), 0.0))
    water = max(0.0, min(100.0, _finite_float(state.get("water", 100.0), 100.0)))
    exhausted = bool(state.get("exhausted", False))
    collapsed = bool(state.get("collapsed", False))
    finished = bool(state.get("finished", False))

    # Already finished or collapsed: no-op (still report to collector buffer)
    if finished or collapsed:
        raw_term_pace = state.get("pace_min_per_mi")
        term_pace = _finite_float(raw_term_pace, 0.0) if raw_term_pace is not None else None
        raw_term_finish = state.get("finish_time_minutes")
        term_finish = _finite_float(raw_term_finish, 0.0) if raw_term_finish is not None else None
        term_wave = _finite_int(state.get("wave_number", 0), 0)
        terminal_result = {
            "status": "success",
            "tick": tick,
            "runner_status": "finished" if finished else "collapsed",
            "velocity": velocity,
            "effective_velocity": 0.0,
            "distance_mi": distance,
            "distance": round(distance, 4),
            "water": water,
            "pace_min_per_mi": term_pace,
            "elapsed_minutes": elapsed_minutes,
            "mi_this_tick": 0.0,
            "finish_time_minutes": term_finish,
            "exhausted": exhausted,
            "collapsed": collapsed,
            "inner_thought": inner_thought,
            "wave_number": term_wave,
        }
        await _push_to_collector_buffer(collector_buffer_key, tool_context, terminal_result)
        return terminal_result

    # --- Effective velocity with degradation factors ---
    # 1. Hydration: 50-100% of base speed
    hydration_factor = 0.5 + 0.5 * (water / 100.0)

    # 2. Wall: sharp pace degradation if past wall_mi
    wall_factor = 1.0
    if state.get("will_hit_wall") and distance > _finite_float(state.get("wall_mi", 18.6411), 18.6411):
        wall_sev = max(0.0, min(1.0, _finite_float(state.get("wall_severity", 0.25), 0.25)))
        wall_factor = 1.0 - wall_sev

    # 3. Natural fatigue: gradual slowdown ~0.2% per tick
    fatigue_factor = max(MIN_FATIGUE_FACTOR, 1.0 - NATURAL_FATIGUE_RATE * tick)

    # 4. Phase 3: Comfort degradation
    # L6: `comfort` is never explicitly initialized in runner state — it
    # defaults to 100.0 (perfect comfort) on first read.  This is intentional:
    # runners start comfortable and degrade via natural decay (-0.5/tick),
    # portable toilet stops (resets to 100.0), and water station side-effects
    # (-15.0 per drink).
    comfort = max(0.0, min(100.0, _finite_float(state.get("comfort", 100.0), 100.0)))
    comfort_factor = 1.0
    if comfort < 20.0:
        comfort_factor = 0.85
    elif comfort < 40.0:
        comfort_factor = 0.95

    # 5. Weather modifier (1.0 default or dict with speed_factor/velocity_factor)
    weather_mod = tick_params.get("weather_modifier", 1.0)
    if isinstance(weather_mod, dict):
        raw_wf = weather_mod.get("speed_factor", weather_mod.get("velocity_factor", 1.0))
        weather_factor = _finite_float(raw_wf, 1.0)
    elif isinstance(weather_mod, (int, float)) and not isinstance(weather_mod, bool):
        weather_factor = _finite_float(weather_mod, 1.0)
    else:
        weather_factor = 1.0
    weather_factor = max(0.5, min(1.5, weather_factor))

    effective_velocity = velocity * hydration_factor * wall_factor * fatigue_factor * comfort_factor * weather_factor
    if not math.isfinite(effective_velocity):
        effective_velocity = 0.0

    # --- Phase 2: Recovery Logic ---
    recovering = bool(state.get("recovering", False))
    recovery_ticks_left = _finite_int(state.get("recovery_ticks_left", 0), 0)

    if recovering:
        recovery_ticks_left -= 1
        if recovery_ticks_left <= 0:
            recovering = False
            recovery_ticks_left = 0
        state["recovery_ticks_left"] = recovery_ticks_left
        state["recovering"] = recovering
        # Heavy speed penalty while recovering
        effective_velocity *= 0.7
        if not math.isfinite(effective_velocity):
            effective_velocity = 0.0

    # --- Distance computation ---
    raw_effective_mph = effective_velocity * SPEED_SCALE
    effective_mph = raw_effective_mph if math.isfinite(raw_effective_mph) else 0.0
    raw_mi_this_tick = (effective_mph / 60.0) * minutes_per_tick
    mi_this_tick = raw_mi_this_tick if math.isfinite(raw_mi_this_tick) else 0.0
    raw_distance = distance + mi_this_tick
    if not math.isfinite(raw_distance):
        raw_distance = race_distance_mi
    new_distance = min(raw_distance, race_distance_mi)
    state["distance"] = new_distance

    # --- Phase 3: Comfort Decay and Toilets ---
    # Process toilets BEFORE hydration depletion and hydration stations
    # because a toilet stop reduces mi_this_tick (and therefore new_distance).
    raw_station_index = tick_params.get("station_index")
    station_index = raw_station_index if isinstance(raw_station_index, dict) else {}
    comfort = max(0.0, comfort - 0.5 * minutes_per_tick)
    raw_toilets = station_index.get("portable_toilets")
    toilet_stations = (
        [
            num
            for x in raw_toilets
            if math.isfinite(num := _finite_float(x, float("nan")))
        ]
        if isinstance(raw_toilets, list)
        else []
    )

    for toilet_mi in toilet_stations:
        if distance < toilet_mi <= new_distance:
            if comfort < 40.0:  # Only stop if they need it
                comfort = 100.0
                # Small speed penalty for stopping
                mi_this_tick *= 0.9
                if not math.isfinite(mi_this_tick):
                    mi_this_tick = 0.0
                raw_distance = distance + mi_this_tick
                if not math.isfinite(raw_distance):
                    raw_distance = race_distance_mi
                new_distance = min(raw_distance, race_distance_mi)
                state["distance"] = new_distance
                break  # At most one toilet stop per tick

    state["comfort"] = comfort

    # --- Hydration depletion ---
    efficiency = max(0.0, _finite_float(state.get("hydration_efficiency", 1.0), 1.0))
    raw_base_depletion = BASE_DEPLETION_RATE * mi_this_tick * efficiency
    base_depletion = raw_base_depletion if math.isfinite(raw_base_depletion) else 0.0
    raw_fatigue_growth = 1.0 + FATIGUE_DEPLETION_GROWTH * new_distance
    fatigue_growth = raw_fatigue_growth if math.isfinite(raw_fatigue_growth) else 1.0
    raw_depletion = base_depletion * fatigue_growth
    if math.isnan(raw_depletion):
        depletion = 0.0
    elif not math.isfinite(raw_depletion):
        depletion = water
    else:
        depletion = raw_depletion
    new_water = max(0.0, water - depletion)

    # --- Auto hydration station check (at planner-placed positions) ---
    raw_water_stations = station_index.get("water_stations")
    water_stations = (
        [
            num
            for x in raw_water_stations
            if math.isfinite(num := _finite_float(x, float("nan")))
        ]
        if isinstance(raw_water_stations, list)
        else []
    )
    raw_medical_stations = station_index.get("medical_tents")
    medical_stations = (
        [
            num
            for x in raw_medical_stations
            if math.isfinite(num := _finite_float(x, float("nan")))
        ]
        if isinstance(raw_medical_stations, list)
        else []
    )

    # Check EVERY station crossed this tick (fast runners may cross 2-3).
    if not water_stations:
        # Fallback to hardcoded interval if planner didn't provide stations or old data
        interval = max(HYDRATION_STATION_INTERVAL_MI, 1e-6)
        prev_marker = int(distance / interval)
        new_marker = int(new_distance / interval)
        if new_marker > prev_marker:
            session_id = getattr(getattr(tool_context, "session", None), "id", None) or "default"
            for marker in range(prev_marker + 1, new_marker + 1):
                rng = random.Random(runner_seed(session_id, marker))
                should_drink = (
                    new_water <= 40.0
                    or exhausted
                    or (new_water <= 60.0 and rng.random() < 0.5)
                    or (new_water > 60.0 and rng.random() < 0.3)
                )
                if should_drink:
                    new_water = min(100.0, new_water + HYDRATION_STATION_REFILL)
                    comfort = max(0.0, comfort - 15.0)  # Drinking water reduces comfort
                if new_water >= 100.0 and comfort <= 0.0:
                    break
    else:
        for station_mi in water_stations:
            if distance < station_mi <= new_distance:
                session_id = getattr(getattr(tool_context, "session", None), "id", None) or "default"
                # Use station mile marker (multiplied) for deterministic seed
                try:
                    scaled_station = station_mi * 1000
                except OverflowError:
                    scaled_station = float("nan")
                if math.isfinite(scaled_station):
                    station_salt = int(scaled_station)
                elif math.isfinite(station_mi):
                    station_salt = int(station_mi)
                else:
                    station_salt = 0
                rng = random.Random(runner_seed(session_id, station_salt))
                should_drink = (
                    new_water <= 40.0
                    or exhausted
                    or (new_water <= 60.0 and rng.random() < 0.5)
                    or (new_water > 60.0 and rng.random() < 0.3)
                )
                if should_drink:
                    new_water = min(100.0, new_water + HYDRATION_STATION_REFILL)
                    comfort = max(0.0, comfort - 15.0)  # Drinking water reduces comfort
                if new_water >= 100.0 and comfort <= 0.0:
                    break

    state["water"] = new_water
    state["comfort"] = comfort

    # --- Phase 2: Injury Risk ---
    injury_risk = max(0.0, _finite_float(state.get("injury_risk", 0.0), 0.0))
    if not recovering:
        # Natural recovery: small per-tick decay so injury_risk is not
        # monotonically increasing (prevents guaranteed collapse in long sims).
        injury_risk = max(0.0, injury_risk - 0.003)

        # Injury risk accumulates faster when fatigued/dehydrated
        base_risk_rate = 0.008
        fatigue_multiplier = 1.0 + (1.0 - new_water / 100.0) * 1.5
        injury_risk += base_risk_rate * fatigue_multiplier

        # "The Wall" zone (miles 18-22): injury risk doubles
        if 18.0 <= new_distance <= 22.0:
            injury_risk += base_risk_rate * fatigue_multiplier

        # Medical tent proximity reduces injury risk
        for med_mi in medical_stations:
            if distance < med_mi <= new_distance:
                injury_risk = max(0.0, injury_risk - 0.15)
                state["medical_saves"] = _finite_int(state.get("medical_saves", 0), 0) + 1

        state["injury_risk"] = injury_risk

    # --- Exhaustion / collapse ---
    if new_water < EXHAUSTION_THRESHOLD:
        exhausted = True
    else:
        exhausted = False
    state["exhausted"] = exhausted

    # Collapse from dehydration OR injury risk
    if exhausted and new_water < COLLAPSE_THRESHOLD:
        collapsed = True
    elif injury_risk > 0.8 and not recovering:
        nearest_medical = min((abs(new_distance - m) for m in medical_stations), default=99)
        if nearest_medical <= 1.0:
            # Saved by medical - recover instead of collapse
            state["recovering"] = True
            state["recovery_ticks_left"] = 2
            state["injury_risk"] = 0.4
            state["medical_saves"] = _finite_int(state.get("medical_saves", 0), 0) + 1
        else:
            collapsed = True

    state["collapsed"] = collapsed

    # --- Finish detection ---
    runner_status = "running"
    raw_finish_state = state.get("finish_time_minutes")
    finish_time = _finite_float(raw_finish_state, 0.0) if raw_finish_state is not None else None
    raw_pace_state = state.get("pace_min_per_mi")
    pace = _finite_float(raw_pace_state, 0.0) if raw_pace_state is not None else None

    if new_distance >= race_distance_mi and not finished:
        finished = True
        state["finished"] = True
        runner_status = "finished"
        # Interpolate exact finish time (use raw_distance, before clamping)
        overshoot = raw_distance - race_distance_mi
        fraction = 1.0 - (overshoot / mi_this_tick) if mi_this_tick > 0 else 1.0
        if not math.isfinite(fraction):
            fraction = 1.0
        fraction = max(0.0, min(1.0, fraction))  # L8: clamp to prevent edge-case anomalies
        raw_finish_time = elapsed_minutes - minutes_per_tick * (1.0 - fraction)
        finish_time = raw_finish_time if math.isfinite(raw_finish_time) else 0.0
        raw_pace = finish_time / race_distance_mi if race_distance_mi > 0 else 0.0
        pace = raw_pace if math.isfinite(raw_pace) else 0.0
        state["finish_time_minutes"] = round(finish_time, 2)
        state["pace_min_per_mi"] = round(pace, 2)
    elif collapsed:
        runner_status = "collapsed"
    elif exhausted:
        runner_status = "exhausted"

    state["runner_status"] = runner_status

    wave = _finite_int(state.get("wave_number", 0), 0)
    result = {
        "status": "success",
        "tick": tick,
        "runner_status": runner_status,
        "velocity": velocity,
        "effective_velocity": round(effective_velocity, 4),
        "distance_mi": min(round(new_distance, 3), race_distance_mi),
        "distance": min(round(new_distance, 4), race_distance_mi),
        "water": round(new_water, 1),
        "pace_min_per_mi": pace,
        "elapsed_minutes": elapsed_minutes,
        "mi_this_tick": round(mi_this_tick, 3),
        "finish_time_minutes": finish_time,
        "exhausted": exhausted,
        "collapsed": collapsed,
        "inner_thought": inner_thought,
        "wave_number": wave,
    }

    # Stagger the first movement tick's gateway emission by wave so the
    # frontend sees runners start in waves (~2s between each wave).
    WAVE_STAGGER_SECONDS = 2.0
    MAX_GATEWAY_DELAY_SECONDS = 30.0
    if tick == 1 and wave > 0:
        try:
            gateway_delay = wave * WAVE_STAGGER_SECONDS
        except OverflowError:
            gateway_delay = float("nan")
        if math.isfinite(gateway_delay):
            result["gateway_delay_seconds"] = min(gateway_delay, MAX_GATEWAY_DELAY_SECONDS)

    # --- Direct-write to collector buffer (bypass PubSub bottleneck) ---
    await _push_to_collector_buffer(collector_buffer_key, tool_context, result)

    return result


async def _push_to_collector_buffer(
    collector_buffer_key: str,
    tool_context: ToolContext,
    result: dict,
) -> None:
    """Direct-write tick result to RaceCollector Redis buffer (sync/async safe)."""
    if not collector_buffer_key:
        return
    try:
        r = get_shared_redis_client()
        if r is not None:
            session_id = getattr(getattr(tool_context, "session", None), "id", "") or ""
            payload_obj = {
                "session_id": session_id,
                "payload": {
                    "tool_name": "process_tick",
                    "result": result,
                },
            }
            try:
                direct_msg = json.dumps(payload_obj, allow_nan=False, default=str)
            except ValueError:
                direct_msg = json.dumps(_strip_nonfinite(payload_obj), allow_nan=False, default=str)
            maybe_push = r.rpush(collector_buffer_key, direct_msg)  # type: ignore[misc]
            if inspect.isawaitable(maybe_push):
                await maybe_push
            maybe_exp = r.expire(collector_buffer_key, 7200)
            if inspect.isawaitable(maybe_exp):
                await maybe_exp
    except Exception:
        logger.warning(
            "process_tick: direct-write RPUSH failed for %s",
            collector_buffer_key,
            exc_info=True,
        )
