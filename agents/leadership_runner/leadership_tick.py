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

"""Leadership tick physics engine.

Mirrors agents/runner/running.py::process_tick() but replaces marathon
physics with organizational dynamics.  Returns identical state keys so
the frontend 3D viewport, HUD, and Redis telemetry pipeline work unchanged.
"""

import inspect
import json
import logging
import math

from google.adk.tools.tool_context import ToolContext

from agents.utils.redis_pool import get_shared_redis_client
from agents.utils.pulses import emit_narrative_pulse

from agents.leadership_runner.constants import (
    CHECKPOINT_INTERVAL,
    get_persona_profile,
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


def _progress_in_gatekeeper_zone(progress: float, data: object) -> bool:
    """Return True if *progress* falls within a gatekeeper's route zone.

    Supports dict segments (``{"start_mi": ..., "end_mi": ...}``), 2-element
    numeric ``[start_mi, end_mi]`` lists/tuples, discrete mile-marker lists,
    and scalar numeric mile markers.
    """
    if not isinstance(progress, (int, float)) or isinstance(progress, bool):
        return False
    try:
        progress_f = float(progress)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(progress_f):
        return False
    if isinstance(data, dict):
        try:
            start_mi = float(data.get("start_mi", 0.0))
            end_mi = float(data.get("end_mi", 0.0))
            return math.isfinite(start_mi) and math.isfinite(end_mi) and start_mi <= progress_f <= end_mi
        except (TypeError, ValueError, OverflowError):
            return False
    if isinstance(data, (list, tuple)):
        numeric_items: list[float] = []
        for x in data:
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                try:
                    xf = float(x)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(xf):
                    numeric_items.append(xf)
        if len(numeric_items) == 2 and numeric_items[0] <= numeric_items[1]:
            return numeric_items[0] <= progress_f <= numeric_items[1]
        return any(int(m) == int(progress_f) for m in numeric_items)
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        try:
            val = float(data)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(val) and int(val) == int(progress_f)
    if isinstance(data, str):
        try:
            val = float(data)
            return math.isfinite(val) and int(val) == int(progress_f)
        except (ValueError, OverflowError):
            return False
    return False


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
                    "tool_name": "process_leadership_tick",
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
            "process_leadership_tick: direct-write RPUSH failed for %s",
            collector_buffer_key,
            exc_info=True,
        )


async def _emit_leadership_narrative(
    *,
    session_id: str,
    simulation_id: str | None,
    persona: str,
    text: str,
    kind: str,
    metadata: dict | None = None,
) -> None:
    """Push a human-readable leadership event to the existing chat surface.

    Uses the project's standard narrative channel (`emit_narrative_pulse`,
    msg_type="json" + event="text") which the frontend Chat Log already
    renders as a TEXT entry — no UI changes required.

    Failures are swallowed: the simulation must never fail because the
    telemetry pipe is unavailable (e.g. unit tests, REDIS_ADDR unset).
    """
    payload_meta = {"kind": kind, "persona": persona}
    if metadata:
        payload_meta.update(metadata)
    try:
        await emit_narrative_pulse(
            session_id=session_id,
            text=text,
            author=f"{persona}_stakeholder",
            type="text",
            metadata=payload_meta,
            simulation_id=simulation_id,
        )
    except Exception:
        logger.debug(
            "leadership narrative emit failed (kind=%s, session=%s)",
            kind,
            session_id,
            exc_info=True,
        )


async def accelerate(intensity: float, tool_context: ToolContext) -> dict:
    """Increase the stakeholder's momentum based on the specified intensity.

    Momentum is reduced when political capital is depleted: at 0% capital,
    max momentum gain is halved. Acceleration and momentum cap scale with persona.

    Args:
        intensity: Acceleration intensity (0.0 to 1.0).
        tool_context: ADK tool context for state management.
    """
    raw_persona = tool_context.state.get("persona", "pragmatist")
    persona = raw_persona if isinstance(raw_persona, str) and raw_persona else "pragmatist"
    profile = get_persona_profile(persona)
    scale_factor = max(profile["momentum_scale"], 1e-6) / 4.0  # Normalize to pragmatist default

    safe_intensity = max(0.0, _finite_float(intensity, 0.0))
    momentum = max(0.0, _finite_float(tool_context.state.get("velocity", 0.0), 0.0))
    political_capital = max(0.0, min(100.0, _finite_float(tool_context.state.get("water", 100.0), 100.0)))
    crowd_responsiveness = max(0.0, _finite_float(tool_context.state.get("crowd_responsiveness", 0.5), 0.5))

    boost_multiplier = 0.1 * scale_factor
    max_momentum = 1.0 * scale_factor

    # Capital penalty: linearly scale from 1.0 (full capital) to 0.5 (no capital)
    capital_factor = 0.5 + 0.5 * (political_capital / 100.0)
    effective_responsiveness = max(0.2, crowd_responsiveness)  # Minimum 20% response
    new_momentum = min(max_momentum, momentum + (safe_intensity * boost_multiplier * capital_factor * effective_responsiveness))
    tool_context.state["velocity"] = new_momentum
    logger.info(
        f"Stakeholder ({persona}) accelerating: momentum={new_momentum:.2f} (capital: {political_capital:.0f}%)"
    )
    return {
        "status": "success",
        "message": f"Momentum increased to {new_momentum:.2f} (capital: {political_capital:.0f}%).",
        "velocity": new_momentum,
    }


async def brake(intensity: float, tool_context: ToolContext) -> dict:
    """Decrease the stakeholder's momentum.

    Braking aggression scales with the persona's profile.

    Args:
        intensity: Braking intensity (0.0 to 1.0).
        tool_context: ADK tool context for state management.
    """
    raw_persona = tool_context.state.get("persona", "pragmatist")
    persona = raw_persona if isinstance(raw_persona, str) and raw_persona else "pragmatist"
    profile = get_persona_profile(persona)
    scale_factor = max(profile["momentum_scale"], 1e-6) / 4.0  # Normalize to pragmatist default
    brake_multiplier = 1.5 * scale_factor

    safe_intensity = max(0.0, _finite_float(intensity, 0.0))
    momentum = max(0.0, _finite_float(tool_context.state.get("velocity", 0.0), 0.0))
    new_momentum = max(0.0, momentum - (safe_intensity * brake_multiplier))
    tool_context.state["velocity"] = new_momentum
    logger.info(f"Stakeholder ({persona}) braking: momentum={new_momentum:.2f}")
    return {
        "status": "success",
        "message": f"Momentum reduced to {new_momentum:.2f}.",
        "velocity": new_momentum,
    }


async def get_vitals(tool_context: ToolContext) -> dict:
    """Get current stakeholder vitals including momentum, progress, capital, and status.

    Args:
        tool_context: ADK tool context for state management.
    """
    vitals = {
        "status": "success",
        "message": "Vitals retrieved successfully.",
        # Isomorphic keys (pipeline compatibility)
        "velocity": tool_context.state.get("velocity", 0.0),
        "distance": tool_context.state.get("distance", 0.0),
        "water": tool_context.state.get("water", 100.0),
        "exhausted": tool_context.state.get("exhausted", False),
        "collapsed": tool_context.state.get("collapsed", False),
        # Leadership-semantic aliases (self-documenting)
        "momentum": tool_context.state.get("velocity", 0.0),
        "alignment_progress": tool_context.state.get("distance", 0.0),
        "political_capital": tool_context.state.get("water", 100.0),
        "stalled": tool_context.state.get("exhausted", False),
        "deadlocked": tool_context.state.get("collapsed", False),
    }
    logger.info("Stakeholder vitals: %s", vitals)
    return vitals


async def process_leadership_tick(
    tool_context: ToolContext,
    inner_thought: str,
    minutes_per_tick: float = -1.0,
    elapsed_minutes: float = -1.0,
    race_distance_mi: float = -1.0,
    tick: int = -1,
    route_friction_map: dict = None,
    gatekeeper_route_map: dict = None,
    collector_buffer_key: str = "",
) -> dict:
    """Advance the leadership simulation by one tick and record inner thought.

    Replaces marathon physics with organizational dynamics:
    - velocity → momentum (rate of alignment adoption)
    - water → political capital (spent on influence actions)
    - wall → resistance cliff (hidden organizational resistance)
    - fatigue → organizational entropy (natural alignment decay)

    Args:
        tool_context: ADK tool context for state management.
        inner_thought: Short internal monologue (5 words max) about what
            the stakeholder is thinking RIGHT NOW. Required.
        minutes_per_tick: (Auto-provided) Simulated minutes per tick.
        elapsed_minutes: (Auto-provided) Total elapsed simulated time.
        race_distance_mi: (Auto-provided) Total consensus distance.
        tick: (Auto-provided) Current tick number.
        route_friction_map: Map of stakeholder roles to their wall severity.
        gatekeeper_route_map: Map of stakeholder roles to their mile markers.
        collector_buffer_key: (Auto-provided) Redis key for telemetry.

    Returns:
        Dict with stakeholder vitals: distance, velocity, water, status.
    """
    state = tool_context.state

    # Resolve persona profile for physics parameters
    raw_persona = state.get("persona", "pragmatist")
    persona = raw_persona if isinstance(raw_persona, str) and raw_persona else "pragmatist"
    profile = get_persona_profile(persona)

    # Read tick params from state if the caller didn't provide them.
    raw_tick_params = state.get("_tick_params")
    tick_params = raw_tick_params if isinstance(raw_tick_params, dict) else {}
    raw_tick = tick_params.get("tick") if tick < 0 else tick
    raw_minutes_per_tick = tick_params.get("minutes_per_tick") if minutes_per_tick < 0 else minutes_per_tick
    raw_elapsed_minutes = tick_params.get("elapsed_minutes") if elapsed_minutes < 0 else elapsed_minutes
    raw_race_distance_mi = tick_params.get("race_distance_mi") if race_distance_mi < 0 else race_distance_mi
    if not collector_buffer_key:
        raw_cbk = tick_params.get("collector_buffer_key")
        collector_buffer_key = raw_cbk if isinstance(raw_cbk, str) else ""

    # H5 fix: fall back to _tick_params for friction maps when the LLM
    # doesn't pass them as function arguments.
    if route_friction_map is None:
        raw_rfm = tick_params.get("route_friction_map")
        route_friction_map = raw_rfm if isinstance(raw_rfm, dict) else {}
    elif not isinstance(route_friction_map, dict):
        route_friction_map = {}
    if gatekeeper_route_map is None:
        raw_grm = tick_params.get("gatekeeper_route_map")
        gatekeeper_route_map = raw_grm if isinstance(raw_grm, dict) else {}
    elif not isinstance(gatekeeper_route_map, dict):
        gatekeeper_route_map = {}

    # Weather modifier: capital/time/morale costs from environmental conditions.
    # May be a dict ({"capital_factor": ...}) or a numeric scalar (e.g. 1.0 default,
    # 0.8 adverse weather) set by before_agent_callback / autopilot.
    weather_modifier = tick_params.get("weather_modifier", {})
    if isinstance(weather_modifier, dict):
        weather_capital_factor = max(0.0, _finite_float(weather_modifier.get("capital_factor"), 1.0))
    elif isinstance(weather_modifier, (int, float)) and not isinstance(weather_modifier, bool):
        wm_val = _finite_float(weather_modifier, 1.0)
        weather_capital_factor = min(2.5, max(0.5, 1.0 / wm_val)) if wm_val > 0 else 1.0
    else:
        weather_capital_factor = 1.0

    # Validate we have all required params.
    if (
        raw_tick is None
        or raw_minutes_per_tick is None
        or raw_elapsed_minutes is None
        or raw_race_distance_mi is None
    ):
        logger.warning("process_leadership_tick called without tick params")
        return {
            "status": "error",
            "message": (
                "No tick event received yet. Wait for the simulation to "
                "send a tick event before calling process_leadership_tick."
            ),
        }

    tick = _finite_int(raw_tick, -1)
    minutes_per_tick = _finite_float(raw_minutes_per_tick, -1.0)
    elapsed_minutes = _finite_float(raw_elapsed_minutes, -1.0)
    race_distance_mi = _finite_float(raw_race_distance_mi, -1.0)
    if tick < 0 or minutes_per_tick < 0.0 or elapsed_minutes < 0.0 or race_distance_mi <= 0.0:
        logger.warning(
            "process_leadership_tick invalid numeric params: tick=%s mpt=%s elapsed=%s dist=%s",
            tick,
            minutes_per_tick,
            elapsed_minutes,
            race_distance_mi,
        )
        return {
            "status": "error",
            "message": "Invalid tick timing parameters.",
        }

    momentum = max(0.0, _finite_float(state.get("velocity", 0.0), 0.0))
    progress = max(0.0, _finite_float(state.get("distance", 0.0), 0.0))
    political_capital = max(0.0, min(100.0, _finite_float(state.get("water", 100.0), 100.0)))
    stalled = bool(state.get("exhausted", False))
    deadlocked = bool(state.get("collapsed", False))
    aligned = bool(state.get("finished", False))

    # Already aligned or deadlocked: no-op (still report to collector buffer)
    if aligned or deadlocked:
        term_injury = max(0.0, _finite_float(state.get("injury_risk", 0.0), 0.0))
        term_comfort = max(0.0, min(100.0, _finite_float(state.get("comfort", 100.0), 100.0)))
        term_bs = _finite_float(state.get("black_swan_multiplier", 1.0), 1.0)
        term_wall_sev = _finite_float(state.get("wall_severity", 0.0), 0.0)
        raw_term_pace = state.get("pace_min_per_mi")
        term_pace = _finite_float(raw_term_pace, 0.0) if raw_term_pace is not None else None
        raw_term_finish = state.get("finish_time_minutes")
        term_finish = _finite_float(raw_term_finish, 0.0) if raw_term_finish is not None else None
        term_wave = _finite_int(state.get("wave_number", 0), 0)
        terminal_result = {
            "status": "success",
            "tick": tick,
            "runner_status": "finished" if aligned else "collapsed",
            # Isomorphic keys (pipeline compatibility)
            "velocity": momentum,
            "effective_velocity": 0.0,
            "distance_mi": progress,
            "distance": round(progress, 4),
            "water": round(political_capital, 1),
            "pace_min_per_mi": term_pace,
            "elapsed_minutes": elapsed_minutes,
            "mi_this_tick": 0.0,
            "finish_time_minutes": term_finish,
            "exhausted": stalled,
            "collapsed": deadlocked,
            "inner_thought": inner_thought,
            "wave_number": term_wave,
            # Leadership-semantic aliases
            "momentum": momentum,
            "effective_momentum": 0.0,
            "alignment_progress": round(progress, 4),
            "political_capital": round(political_capital, 1),
            "alignment_rate": term_pace,
            "progress_this_tick": 0.0,
            "stalled": stalled,
            "deadlocked": deadlocked,
            "persona": persona,
            "notable_event": None,
            # Tier 1 enrichment: terminal-state snapshot.  Keeps schema stable
            # so the AI2UI surface always finds these keys regardless of phase.
            "leadership_context": {
                "persona": persona,
                "decision_phase": "finalized" if aligned else "collapsed",
                "capital_efficiency": 0.0,
                "resistance_factor": 0.0,
                "resistance_encountered": 1.0 if deadlocked else 0.0,
                "entropy_factor": 0.0,
                "entropy_rate": 1.0,
                "capital_factor": 0.0,
                "black_swan_factor": term_bs,
                "hygiene_factor": 1.0,
                "gatekeeper_engaged": False,
                "wall_severity": term_wall_sev,
                "burnout_capital_multiplier": 1.0,
                "cliff_capital_cost": 0.0,
            },
            "health": {
                "burnout_risk": round(term_injury, 3),
                "injury_risk": round(term_injury, 3),
                "recovery_mode": False,
                "recovery_ticks_left": 0,
                "medical_saves": _finite_int(state.get("medical_saves", 0), 0),
                "comfort": round(term_comfort, 1),
            },
            "roadblocks": [],
            "narratives": [],
        }
        await _push_to_collector_buffer(collector_buffer_key, tool_context, terminal_result)
        return terminal_result

    # --- Effective momentum with organizational degradation factors ---

    # 2. Resistance cliff: dynamic route friction
    resistance_factor = 1.0
    wall_severity = max(0.0, min(1.0, _finite_float(state.get("wall_severity", 0.30), 0.30)))
    gatekeeper_found = False
    active_gatekeeper: str | None = None
    cliff_capital_cost = 0.0

    # Dynamic check: which Gatekeeper controls the current mile?
    if route_friction_map and gatekeeper_route_map:
        # Find which role owns this mile/segment (if any)
        for role, data in gatekeeper_route_map.items():
            if _progress_in_gatekeeper_zone(progress, data):
                raw_severity = _finite_float(route_friction_map.get(role, 5.0), 5.0)
                wall_severity = max(0.0, min(1.0, raw_severity / 10.0))
                gatekeeper_found = True
                active_gatekeeper = role
                break

    # If progress is past a wall or we hit a dynamic wall
    if gatekeeper_found:
        # A wall_severity of > 0.5 increases resistance (lowers factor)
        # A wall_severity of < 0.5 reduces resistance (increases factor)
        # 0.5 severity = 1.0 factor (no change)
        # 1.0 severity = 0.0 factor (full stop)
        # 0.0 severity = 1.5 factor (coalition boost)
        resistance_factor = max(0.0, 1.0 - (wall_severity - 0.5) * 2.0)
        # One-time cliff capital penalty per gatekeeper zone.
        raw_charged = state.get("_cliff_charged_gatekeepers")
        charged_gatekeepers = list(raw_charged) if isinstance(raw_charged, list) else []
        if wall_severity > 0.5 and active_gatekeeper and active_gatekeeper not in charged_gatekeepers:
            cliff_capital_cost = (wall_severity - 0.5) * 20.0
            political_capital = max(0.0, political_capital - cliff_capital_cost)
            charged_gatekeepers.append(active_gatekeeper)
            state["_cliff_charged_gatekeepers"] = charged_gatekeepers
    elif state.get("will_hit_wall") and progress > _finite_float(state.get("wall_mi", 16.0), 16.0):
        # Traditional physical marathon physics
        wall_sev = max(0.0, min(1.0, _finite_float(state.get("wall_severity", 0.30), 0.30)))
        resistance_factor = 1.0 - wall_sev
        # One-time capital drain when first hitting the wall
        if not state.get("_wall_capital_charged"):
            wall_capital_cost = wall_sev * 15.0
            political_capital = max(0.0, political_capital - wall_capital_cost)
            state["_wall_capital_charged"] = True

    # 1. Political capital factor: 40–100% of base momentum (Uses post-cliff capital)
    capital_factor = 0.4 + 0.6 * (political_capital / 100.0)

    # Tier 0 debug: surface organizational friction dynamics per tick.
    if gatekeeper_found:
        logger.debug(
            "Resistance cliff [%s] tick=%s progress=%.2f wall_severity=%.2f resistance_factor=%.2f",
            persona,
            tick,
            progress,
            wall_severity,
            resistance_factor,
        )

    # 3. Organizational entropy: gradual slowdown ~0.3% per tick
    entropy_factor = max(profile["min_entropy_factor"], 1.0 - profile["entropy_rate"] * tick)
    logger.debug(
        "Entropy [%s] tick=%s entropy_factor=%.3f (alignment_decay=%.3f)",
        persona,
        tick,
        entropy_factor,
        1.0 - entropy_factor,
    )

    # 4. Black swan modifier (injected mid-simulation by scenario events)
    black_swan_factor = max(0.0, _finite_float(state.get("black_swan_multiplier", 1.0), 1.0))

    # 5. Decay the black swan multiplier back toward 1.0 over time
    if black_swan_factor != 1.0:
        recovery_rate = 0.05  # 5% recovery per tick
        if black_swan_factor < 1.0:
            state["black_swan_multiplier"] = min(1.0, black_swan_factor + recovery_rate)
        else:
            state["black_swan_multiplier"] = max(1.0, black_swan_factor - recovery_rate)

    # 6. Phase 3: Hygiene (Comfort) degradation
    hygiene = max(0.0, min(100.0, _finite_float(state.get("comfort", 100.0), 100.0)))
    hygiene_factor = 1.0
    if hygiene < 20.0:
        hygiene_factor = 0.85
    elif hygiene < 40.0:
        hygiene_factor = 0.95

    effective_momentum = (
        momentum * capital_factor * resistance_factor * entropy_factor * black_swan_factor * hygiene_factor
    )
    if not math.isfinite(effective_momentum):
        effective_momentum = 0.0
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
        # Heavy speed penalty while recovering from burnout
        effective_momentum *= 0.7
        if not math.isfinite(effective_momentum):
            effective_momentum = 0.0

    # --- Progress computation ---
    raw_effective_rate = effective_momentum * profile["momentum_scale"]
    effective_rate = raw_effective_rate if math.isfinite(raw_effective_rate) else 0.0
    raw_progress_this_tick = (effective_rate / 60.0) * minutes_per_tick
    progress_this_tick = raw_progress_this_tick if math.isfinite(raw_progress_this_tick) else 0.0
    raw_progress = progress + progress_this_tick
    if not math.isfinite(raw_progress):
        raw_progress = race_distance_mi
    new_progress = min(raw_progress, race_distance_mi)
    state["distance"] = new_progress

    # --- Phase 3: Hygiene (Comfort) Decay and Venting Spaces (Toilets) ---
    # H7 fix: Process venting BEFORE checkpoint refills so the progress
    # penalty from stopping at a venting station is applied before any
    # checkpoint crossing is evaluated. This prevents double-refills from
    # the same checkpoint when venting reduces new_progress backward.
    raw_station_index = tick_params.get("station_index")
    station_index = raw_station_index if isinstance(raw_station_index, dict) else {}
    hygiene = max(0.0, hygiene - 0.5 * minutes_per_tick)
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
        if progress < toilet_mi <= new_progress:
            if hygiene < 40.0:  # Only stop to vent if they need it
                hygiene = 100.0
                # Small speed penalty for stopping
                progress_this_tick *= 0.9
                if not math.isfinite(progress_this_tick):
                    progress_this_tick = 0.0
                raw_progress = progress + progress_this_tick
                if not math.isfinite(raw_progress):
                    raw_progress = race_distance_mi
                new_progress = min(raw_progress, race_distance_mi)
                state["distance"] = new_progress
                break  # At most one venting stop per tick

    # --- Political capital depletion ---
    efficiency = max(0.0, _finite_float(state.get("hydration_efficiency", 1.0), 1.0))
    local_resistance = max(0.0, _finite_float(state.get("local_resistance", 0.0), 0.0))
    resistance_modifier = 1.0 + local_resistance

    raw_base_depletion = profile["capital_depletion_rate"] * progress_this_tick * efficiency
    base_depletion = raw_base_depletion if math.isfinite(raw_base_depletion) else 0.0
    raw_friction = 1.0 + profile["friction_growth"] * new_progress
    friction = raw_friction if math.isfinite(raw_friction) else 1.0

    # Burnout ↔ capital coupling: high burnout accelerates capital drain.
    # 0.0-0.5 → no extra cost; 0.5-0.8 → up to 40% extra (linear ramp)
    burnout_risk_current = max(0.0, _finite_float(state.get("injury_risk", 0.0), 0.0))
    if burnout_risk_current > 0.5:
        burnout_capital_multiplier = 1.0 + min(0.4, (burnout_risk_current - 0.5) * 1.33)
    else:
        burnout_capital_multiplier = 1.0

    # Circuit breaker: if capital is already critically low, don't amplify further.
    if political_capital < 20.0:
        burnout_capital_multiplier = min(burnout_capital_multiplier, 1.1)

    raw_depletion = base_depletion * friction * resistance_modifier * burnout_capital_multiplier * weather_capital_factor
    if math.isnan(raw_depletion):
        depletion = 0.0
    elif not math.isfinite(raw_depletion):
        depletion = political_capital
    else:
        depletion = raw_depletion
    new_capital = max(0.0, political_capital - depletion)

    # --- Alignment checkpoint auto-refill (at planner-placed positions) ---
    # Uses new_progress (already adjusted for venting penalty above).
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

    # Check EVERY checkpoint crossed this tick.
    if not water_stations:
        interval = max(CHECKPOINT_INTERVAL, 1e-6)
        prev_marker = int(progress / interval)
        new_marker = int(new_progress / interval)
        if new_marker > prev_marker:
            for marker in range(prev_marker + 1, new_marker + 1):
                # Coalition bonus from DCNS zones (if available)
                coalition_bonus = max(0.0, _finite_float(state.get("coalition_bonus", 0.0), 0.0))
                refill = profile["checkpoint_refill"] * (1.0 + coalition_bonus)

                # Deterministic refill: always refill, amount scales with need.
                if new_capital <= 35.0:
                    refill_pct = 1.0
                elif new_capital <= 50.0:
                    refill_pct = 0.8
                elif new_capital <= 65.0:
                    refill_pct = 0.5
                else:
                    refill_pct = 0.25
                scaled_refill = refill * refill_pct
                new_capital = min(100.0, new_capital + scaled_refill)
                hygiene = max(0.0, hygiene - 15.0)  # Gaining capital uses up hygiene (like drinking water)
                if (new_capital >= 100.0 or scaled_refill == 0.0) and hygiene <= 0.0:
                    break
    else:
        for cp_mi in water_stations:
            if progress < cp_mi <= new_progress:
                # Coalition bonus from DCNS zones (if available)
                coalition_bonus = max(0.0, _finite_float(state.get("coalition_bonus", 0.0), 0.0))
                refill = profile["checkpoint_refill"] * (1.0 + coalition_bonus)

                # Deterministic refill: always refill, amount scales with need.
                if new_capital <= 35.0:
                    refill_pct = 1.0
                elif new_capital <= 50.0:
                    refill_pct = 0.8
                elif new_capital <= 65.0:
                    refill_pct = 0.5
                else:
                    refill_pct = 0.25
                scaled_refill = refill * refill_pct
                new_capital = min(100.0, new_capital + scaled_refill)
                hygiene = max(0.0, hygiene - 15.0)  # Gaining capital uses up hygiene (like drinking water)
                if (new_capital >= 100.0 or scaled_refill == 0.0) and hygiene <= 0.0:
                    break

    state["water"] = new_capital
    state["comfort"] = hygiene

    # --- Phase 2: Burnout Risk ---
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
    burnout_risk = max(0.0, _finite_float(state.get("injury_risk", 0.0), 0.0))

    if not recovering:
        # Natural recovery: small per-tick decay so burnout_risk is not
        # monotonically increasing (prevents guaranteed deadlock in long sims).
        burnout_risk = max(0.0, burnout_risk - 0.003)

        # Burnout risk accumulates faster when stalled/low capital
        base_risk_rate = 0.008
        fatigue_multiplier = 1.0 + (1.0 - new_capital / 100.0) * 1.5
        burnout_risk += base_risk_rate * fatigue_multiplier

        # "The Wall" zone: burnout risk doubles
        if state.get("will_hit_wall", False):
            wall_start = _finite_float(state.get("wall_mi", 16.0), 16.0)
            if wall_start <= new_progress <= wall_start + 6.0:
                burnout_risk += base_risk_rate * fatigue_multiplier

        # Medical tent proximity reduces burnout risk (safety net)
        for med_mi in medical_stations:
            if progress < med_mi <= new_progress:
                burnout_risk = max(0.0, burnout_risk - 0.15)
                state["medical_saves"] = _finite_int(state.get("medical_saves", 0), 0) + 1

        state["injury_risk"] = burnout_risk

        # Tier 0 debug: log burnout trajectory at threshold crossings.
        # Narrative (Tier 2) fires at 0.6 and 0.8; this also catches the
        # earlier 0.3 ramp so developers see fragility building up.
        _prev_burnout = _finite_int(state.get("_prev_burnout_bucket", 0), 0)
        for _threshold, _bucket, _level in (
            (0.3, 1, "info"),
            (0.6, 2, "info"),
            (0.8, 3, "warning"),
        ):
            if burnout_risk >= _threshold and _prev_burnout < _bucket:
                _log = logger.warning if _level == "warning" else logger.info
                _log(
                    "Burnout trajectory [%s] tick=%s crossed %.1f (risk=%.3f, capital=%.1f%%, progress=%.2f)",
                    persona,
                    tick,
                    _threshold,
                    burnout_risk,
                    new_capital,
                    new_progress,
                )
                state["_prev_burnout_bucket"] = _bucket
        # Reset bucket when burnout cools off so future spikes re-fire.
        if burnout_risk < 0.25:
            state["_prev_burnout_bucket"] = 0

    # --- Stall / Deadlock detection ---
    if new_capital < profile["stall_threshold"]:
        stalled = True
    else:
        stalled = False
    state["exhausted"] = stalled

    # Deadlock from pure political capital loss OR burnout risk
    if stalled and new_capital < profile["deadlock_threshold"]:
        deadlocked = True
    elif burnout_risk > 0.8 and not recovering:
        nearest_medical = min((abs(new_progress - m) for m in medical_stations), default=99)
        if nearest_medical <= 1.0:
            # Saved by medical - recover instead of collapse
            state["recovering"] = True
            state["recovery_ticks_left"] = 2
            state["injury_risk"] = 0.4
            state["medical_saves"] = _finite_int(state.get("medical_saves", 0), 0) + 1
        elif not medical_stations:
            # M11 fix: When no medical stations exist, auto-recover with
            # a heavier penalty instead of guaranteed deadlock. This gives
            # the planner feedback that medical infrastructure was needed.
            logger.warning(
                "Burnout auto-recovery [%s] tick=%s: no medical stations placed, "
                "forced recovery with heavy penalty (risk was %.3f)",
                persona,
                tick,
                burnout_risk,
            )
            state["recovering"] = True
            state["recovery_ticks_left"] = 4  # Longer recovery without medical
            state["injury_risk"] = 0.6  # Higher residual risk
        else:
            deadlocked = True

    state["collapsed"] = deadlocked

    # --- Alignment (finish) detection ---
    stakeholder_status = "running"
    raw_finish_state = state.get("finish_time_minutes", 0.0)
    finish_time = _finite_float(raw_finish_state, 0.0) if raw_finish_state is not None else None
    raw_pace_state = state.get("pace_min_per_mi", 0.0)
    pace = _finite_float(raw_pace_state, 0.0) if raw_pace_state is not None else None

    if new_progress >= race_distance_mi and not aligned:
        aligned = True
        state["finished"] = True
        stakeholder_status = "finished"
        overshoot = raw_progress - race_distance_mi
        fraction = 1.0 - (overshoot / progress_this_tick) if progress_this_tick > 0 else 1.0
        if not math.isfinite(fraction):
            fraction = 1.0
        fraction = max(0.0, min(1.0, fraction))
        raw_finish_time = elapsed_minutes - minutes_per_tick * (1.0 - fraction)
        finish_time = raw_finish_time if math.isfinite(raw_finish_time) else 0.0
        raw_pace = finish_time / race_distance_mi if race_distance_mi > 0 else 0.0
        pace = raw_pace if math.isfinite(raw_pace) else 0.0
        state["finish_time_minutes"] = round(finish_time, 2)
        state["pace_min_per_mi"] = round(pace, 2)
    elif deadlocked:
        stakeholder_status = "collapsed"
    elif stalled:
        stakeholder_status = "exhausted"

    state["runner_status"] = stakeholder_status

    # --- Notable event: report state transitions for simulator aggregation ---
    # The simulator's advance_tick reads result.get("notable_event") (singular)
    # and appends it to the tick's notable_events list.  We only fire on
    # transitions to avoid flooding the list with repeated events.
    notable_event = None

    if aligned and not state.get("_notified_aligned"):
        notable_event = f"{persona} stakeholder aligned at sprint {tick} (progress: {round(new_progress, 1)})"
        state["_notified_aligned"] = True
    elif deadlocked and not state.get("_notified_deadlocked"):
        notable_event = f"{persona} stakeholder deadlocked (capital: {round(new_capital, 1)}%)"
        state["_notified_deadlocked"] = True
    elif stalled and not state.get("_notified_stalled"):
        notable_event = f"{persona} stakeholder stalled (capital: {round(new_capital, 1)}%)"
        state["_notified_stalled"] = True
    # Reset stall notification if capital recovers
    if not stalled:
        state["_notified_stalled"] = False
    if not deadlocked:
        state["_notified_deadlocked"] = False

    # --- Tier 1: Leadership-context enrichment (AI2UI + debug surface) ---
    # Surfaces internally-computed organizational dynamics through the
    # existing tool-result channel.  No UI changes required: the frontend
    # Chat Log panel already renders arbitrary nested JSON under tool_end.
    raw_cap_eff = effective_momentum / momentum if momentum > 1e-6 else 0.0
    capital_efficiency = round(raw_cap_eff, 3) if math.isfinite(raw_cap_eff) else 0.0

    if recovering:
        decision_phase = "recover"
    elif momentum > 1e-6 and effective_momentum < momentum * 0.5:
        decision_phase = "brake"
    elif momentum > 1e-6 and effective_momentum >= momentum * 0.9:
        decision_phase = "accelerate"
    else:
        decision_phase = "hold"

    roadblocks = []
    if gatekeeper_found and wall_severity > 0.5:
        roadblocks.append(
            {
                "type": "gatekeeper_resistance",
                "severity": round(wall_severity, 2),
                "detail": f"resistance_factor={round(resistance_factor, 2)}",
            }
        )
    if new_capital < 35.0:
        roadblocks.append(
            {
                "type": "capital_depletion",
                "severity": round(1.0 - new_capital / 100.0, 2),
                "detail": f"political_capital={round(new_capital, 1)}%",
            }
        )
    if burnout_risk > 0.6:
        roadblocks.append(
            {
                "type": "burnout_risk_elevated",
                "severity": round(burnout_risk, 2),
                "detail": "stakeholder fragility nearing collapse threshold",
            }
        )
    if entropy_factor < 0.7:
        roadblocks.append(
            {
                "type": "alignment_decay",
                "severity": round(1.0 - entropy_factor, 2),
                "detail": f"entropy_factor={round(entropy_factor, 3)}",
            }
        )
    if black_swan_factor < 0.8:
        roadblocks.append(
            {
                "type": "external_shock",
                "severity": round(1.0 - black_swan_factor, 2),
                "detail": f"black_swan_multiplier={round(black_swan_factor, 2)}",
            }
        )
    if hygiene < 40.0:
        roadblocks.append(
            {
                "type": "team_fatigue",
                "severity": round(1.0 - hygiene / 100.0, 2),
                "detail": f"comfort={round(hygiene, 1)}",
            }
        )

    # --- Tier 2: Phase-transition + critical-event narratives (AI2UI) ---
    # Emits human-readable chat messages to the existing log surface using
    # the project's standard narrative pulse channel.  No UI changes
    # required: msg_type="json" + event="text" already renders as a TEXT
    # entry in the Chat Log panel.  All emits are best-effort.
    _narratives: list[tuple[str, str, dict]] = []

    _prev_status_raw = state.get("_prev_runner_status")
    # First tick: treat current status as prior to avoid spurious transitions.
    _prev_status = stakeholder_status if _prev_status_raw is None else _prev_status_raw

    if stakeholder_status != _prev_status:
        if stakeholder_status == "finished":
            _narratives.append(
                (
                    "phase_transition:aligned",
                    (
                        f"✅ {persona.title()} stakeholder reached full alignment "
                        f"at tick {tick} (capital {round(new_capital, 1)}%, "
                        f"{round(new_progress, 1)}/{round(race_distance_mi, 1)} mi)."
                    ),
                    {"from": _prev_status, "to": stakeholder_status, "trigger": "consensus_reached"},
                )
            )
        elif stakeholder_status == "collapsed":
            trigger = "capital_depletion" if new_capital < profile["deadlock_threshold"] else "burnout_overload"
            _narratives.append(
                (
                    "phase_transition:deadlocked",
                    (
                        f"💥 {persona.title()} stakeholder deadlocked "
                        f"({trigger.replace('_', ' ')}): capital "
                        f"{round(new_capital, 1)}%, burnout risk "
                        f"{round(burnout_risk, 2)}."
                    ),
                    {"from": _prev_status, "to": stakeholder_status, "trigger": trigger},
                )
            )
        elif stakeholder_status == "exhausted":
            _narratives.append(
                (
                    "phase_transition:stalled",
                    (
                        f"⚠️ {persona.title()} stakeholder stalled — political "
                        f"capital down to {round(new_capital, 1)}% (resistance "
                        f"{round(1.0 - resistance_factor, 2)})."
                    ),
                    {
                        "from": _prev_status,
                        "to": stakeholder_status,
                        "trigger": "capital_below_stall_threshold",
                        "stall_threshold": profile["stall_threshold"],
                    },
                )
            )
        elif stakeholder_status == "running" and _prev_status == "exhausted":
            _narratives.append(
                (
                    "phase_transition:recovered",
                    (f"🔄 {persona.title()} stakeholder regained momentum (capital back to {round(new_capital, 1)}%)."),
                    {"from": _prev_status, "to": stakeholder_status, "trigger": "capital_restored"},
                )
            )
        elif stakeholder_status == "running" and _prev_status == "collapsed":
            _narratives.append(
                (
                    "phase_transition:unlocked",
                    (f"{persona.title()} has been rescued by executive intervention! "
                     f"Political capital restored to {new_capital:.1f}%. Recovery in progress."),
                    {"from": _prev_status, "to": stakeholder_status, "trigger": "capital_restored"},
                )
            )

    state["_prev_runner_status"] = stakeholder_status

    # Critical events: one-shot per crossing.  Each alert has its own reset
    # threshold so it can re-fire if conditions deteriorate again later.
    if burnout_risk >= 0.8 and not state.get("_emitted_burnout_severe"):
        state["_emitted_burnout_severe"] = True
        _narratives.append(
            (
                "critical:burnout_severe",
                (
                    f"🚨 Severe burnout risk on {persona} stakeholder "
                    f"({round(burnout_risk, 2)}). Approaching collapse threshold."
                ),
                {"burnout_risk": round(burnout_risk, 3), "tier": "severe"},
            )
        )
    elif burnout_risk < 0.5:
        state["_emitted_burnout_severe"] = False

    if burnout_risk >= 0.6 and not state.get("_emitted_burnout_warn"):
        state["_emitted_burnout_warn"] = True
        _narratives.append(
            (
                "critical:burnout_warning",
                (
                    f"⚠️ Elevated burnout risk on {persona} stakeholder "
                    f"({round(burnout_risk, 2)}). Consider coaching support."
                ),
                {"burnout_risk": round(burnout_risk, 3), "tier": "warning"},
            )
        )
    elif burnout_risk < 0.4:
        state["_emitted_burnout_warn"] = False

    if black_swan_factor < 0.5 and not state.get("_emitted_black_swan"):
        state["_emitted_black_swan"] = True
        _narratives.append(
            (
                "critical:external_shock",
                (
                    f"🌪️ External shock event hit {persona} stakeholder "
                    f"(multiplier {round(black_swan_factor, 2)}). Momentum disrupted."
                ),
                {"black_swan_factor": round(black_swan_factor, 3)},
            )
        )
    elif black_swan_factor > 0.8:
        state["_emitted_black_swan"] = False

    if new_capital < 25.0 and not state.get("_emitted_capital_critical"):
        state["_emitted_capital_critical"] = True
        _narratives.append(
            (
                "critical:capital_critical",
                (
                    f"💧 Political capital critical on {persona} stakeholder "
                    f"({round(new_capital, 1)}%). Influence near depleted."
                ),
                {"political_capital": round(new_capital, 1)},
            )
        )
    elif new_capital > 45.0:
        state["_emitted_capital_critical"] = False

    if recovering and not state.get("_emitted_medical_save"):
        state["_emitted_medical_save"] = True
        _narratives.append(
            (
                "critical:medical_save",
                (
                    f"🏥 {persona.title()} stakeholder pulled into recovery "
                    f"(medical save #{state.get('medical_saves', 0)})."
                ),
                {"recovery_ticks_left": state.get("recovery_ticks_left", 0)},
            )
        )
    elif not recovering:
        state["_emitted_medical_save"] = False

    if gatekeeper_found and wall_severity >= 0.7 and not state.get("_emitted_hard_wall"):
        state["_emitted_hard_wall"] = True
        _narratives.append(
            (
                "critical:hard_resistance",
                (
                    f"🚧 {persona.title()} stakeholder hit hard gatekeeper "
                    f"resistance (severity {round(wall_severity, 2)})."
                ),
                {"wall_severity": round(wall_severity, 3), "resistance_factor": round(resistance_factor, 3)},
            )
        )
    elif not gatekeeper_found:
        state["_emitted_hard_wall"] = False

    if _narratives:
        _session_id = getattr(getattr(tool_context, "session", None), "id", None) or "default"
        _simulation_id = state.get("simulation_id") or tick_params.get("simulation_id")
        for _kind, _text, _meta in _narratives:
            # Tier 0 debug: mirror every narrative as a structured log line.
            # phase_transition:* → INFO, critical:* → WARNING.
            if _kind.startswith("critical:"):
                logger.warning(
                    "LEADERSHIP_EVENT [%s] tick=%s kind=%s meta=%s | %s",
                    persona,
                    tick,
                    _kind,
                    _meta,
                    _text,
                )
            else:
                logger.info(
                    "LEADERSHIP_EVENT [%s] tick=%s kind=%s meta=%s | %s",
                    persona,
                    tick,
                    _kind,
                    _meta,
                    _text,
                )
            await _emit_leadership_narrative(
                session_id=_session_id,
                simulation_id=_simulation_id,
                persona=persona,
                text=_text,
                kind=_kind,
                metadata=_meta,
            )

    wave = _finite_int(state.get("wave_number", 0), 0)
    medical_saves_val = _finite_int(state.get("medical_saves", 0), 0)
    recovery_ticks_val = _finite_int(state.get("recovery_ticks_left", 0), 0)
    result = {
        "status": "success",
        "tick": tick,
        "runner_status": stakeholder_status,
        # Isomorphic keys (pipeline compatibility)
        "velocity": momentum,
        "effective_velocity": round(effective_momentum, 4),
        "distance_mi": min(round(new_progress, 3), race_distance_mi),
        "distance": min(round(new_progress, 4), race_distance_mi),
        "water": round(new_capital, 1),
        "pace_min_per_mi": pace,
        "elapsed_minutes": elapsed_minutes,
        "mi_this_tick": round(progress_this_tick, 3),
        "finish_time_minutes": finish_time,
        "exhausted": stalled,
        "collapsed": deadlocked,
        "inner_thought": inner_thought,
        "wave_number": wave,
        # Leadership-semantic aliases
        "momentum": momentum,
        "effective_momentum": round(effective_momentum, 4),
        "alignment_progress": min(round(new_progress, 4), race_distance_mi),
        "political_capital": round(new_capital, 1),
        "alignment_rate": pace,
        "progress_this_tick": round(progress_this_tick, 3),
        "stalled": stalled,
        "deadlocked": deadlocked,
        "persona": persona,
        "notable_event": notable_event,
        # Tier 1 enrichment: organizational dynamics surfaced for AI2UI/debug.
        "leadership_context": {
            "persona": persona,
            "decision_phase": decision_phase,
            "capital_efficiency": capital_efficiency,
            "resistance_factor": round(resistance_factor, 3),
            "resistance_encountered": round(1.0 - resistance_factor, 3),
            "entropy_factor": round(entropy_factor, 3),
            "entropy_rate": round(1.0 - entropy_factor, 3),
            "capital_factor": round(capital_factor, 3),
            "black_swan_factor": round(black_swan_factor, 3),
            "hygiene_factor": round(hygiene_factor, 3),
            "gatekeeper_engaged": gatekeeper_found,
            "wall_severity": round(wall_severity, 3),
            "burnout_capital_multiplier": round(burnout_capital_multiplier, 3),
            "cliff_capital_cost": round(cliff_capital_cost, 2) if 'cliff_capital_cost' in locals() else 0.0,
        },
        "health": {
            "burnout_risk": round(burnout_risk, 3),
            "injury_risk": round(burnout_risk, 3),
            "recovery_mode": recovering,
            "recovery_ticks_left": recovery_ticks_val,
            "medical_saves": medical_saves_val,
            "comfort": round(hygiene, 1),
        },
        "roadblocks": roadblocks,
        "narratives": [{"kind": k, "text": t, "metadata": m} for k, t, m in _narratives],
    }

    # Stagger first tick emission by wave
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
