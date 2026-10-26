# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
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

"""Pre-race setup tools for the Simulator agent.

These tools handle simulation initialization: parsing the plan, spawning
runner agents via the gateway API, starting the Redis telemetry collector,
and delegating inter-agent communication.
"""

import json
import logging
import os

import aiohttp
from google.adk.tools.tool_context import ToolContext

from agents.simulator.broadcast import publish_to_runners, wait_for_runners_ready
from agents.utils import auth as oidc_auth
from agents.utils.sim_defaults import (
    DEFAULT_DURATION_SECONDS,
    DEFAULT_MAX_TICKS,
    DEFAULT_RACE_DISTANCE_MI,
    DEFAULT_TICK_INTERVAL_SECONDS,
)
from agents.utils.runner_protocol import (
    RunnerEvent,
    RunnerEventType,
    serialize_runner_event,
)
from agents.utils.runner_types import DEFAULT_RUNNER_TYPE, cap_for_runner_type

logger = logging.getLogger(__name__)


async def prepare_simulation(
    plan_json: str,
    tool_context: ToolContext,
) -> dict:
    """Parse the plan JSON and configure simulation parameters.

    The plan JSON should contain: {action, narrative, route, simulation_config?}
    simulation_config is optional: {duration_seconds?, tick_interval_seconds?, runner_count?, total_race_hours?}
    """
    # Re-entrancy guard: prevent duplicate simulation runs from A2A retries
    if tool_context.state.get("simulation_in_progress"):
        logger.warning("prepare_simulation: simulation already in progress, rejecting duplicate")
        return {
            "status": "error",
            "message": "A simulation is already in progress for this session. Ignoring duplicate request.",
        }

    try:
        plan = json.loads(plan_json)
    except (json.JSONDecodeError, TypeError) as e:
        return {"status": "error", "message": f"Invalid plan JSON: {e}"}

    from agents.simulator.quest_loader import load_quest_from_yaml, inject_quest_into_state, QuestConfig

    # Quest loading: try Redis side-channel first (leadership planner stores
    # quest_config there to keep the A2A payload small), then fall back to
    # the payload body. This follows the same pattern as route_geojson and
    # traffic_assessment: Redis → payload → session state.
    from agents.utils.simdata import load_simulation_data as _load_quest_simdata

    sim_id_for_quest = tool_context.state.get("simulation_id", "")
    quest_simdata = await _load_quest_simdata(sim_id_for_quest)
    quest_config_dict = quest_simdata.get("quest_config") or plan.get("quest_config")

    if quest_config_dict:
        try:
            quest = QuestConfig.model_validate(quest_config_dict)
            inject_quest_into_state(tool_context.state, quest)
            logger.info("prepare_simulation: injected dynamic quest_config '%s'", quest.id)
        except Exception as e:
            logger.error("prepare_simulation: invalid dynamic quest_config: %s", e)
            return {"status": "error", "message": f"Invalid quest_config: {e}"}
    else:
        quest_id = plan.get("quest_id", "ai_powered_disruptor")
        try:
            quest = load_quest_from_yaml(quest_id)
            inject_quest_into_state(tool_context.state, quest)
        except Exception as e:
            logger.error("prepare_simulation: failed to load quest %s: %s", quest_id, e)
            return {"status": "error", "message": f"Failed to load quest {quest_id}: {e}"}

    # Extract simulation_config with defaults
    sim_config = plan.get("simulation_config", {})
    duration_seconds = sim_config.get("duration_seconds", DEFAULT_DURATION_SECONDS)
    tick_interval_seconds = sim_config.get("tick_interval_seconds", DEFAULT_TICK_INTERVAL_SECONDS)
    total_race_hours = sim_config.get("total_race_hours", 6.0)
    runner_type = sim_config.get("runner_type", DEFAULT_RUNNER_TYPE)
    max_runners = cap_for_runner_type(runner_type)
    # Default runner_count to the cap for the chosen runner_type. This matches
    # the OSS GCP intent: runner_autopilot defaults to MAX_RUNNERS_AUTOPILOT
    # (100), runner_cloudrun / runner_gke / runner default to MAX_RUNNERS_LLM
    # (10). Local dev can dial either down via env vars to keep simulations
    # fast. The planner can always specify an explicit runner_count to
    # override.
    runner_count = sim_config.get("runner_count", max_runners)
    original_runner_count = runner_count
    if runner_count > max_runners:
        logger.warning(
            "prepare_simulation: runner_count %d exceeds cap %d for runner_type=%s, capping",
            runner_count,
            max_runners,
            runner_type,
        )
        runner_count = max_runners
    max_ticks = duration_seconds // tick_interval_seconds

    # Store ALL config in state
    tool_context.state["simulation_config"] = {
        "duration_seconds": duration_seconds,
        "tick_interval_seconds": tick_interval_seconds,
        "total_race_hours": total_race_hours,
        "runner_count": runner_count,
        "runner_type": runner_type,
        "persona": sim_config.get("persona", "pragmatist"),
        "race_distance_mi": sim_config.get("race_distance_mi", DEFAULT_RACE_DISTANCE_MI),
    }
    # Route GeoJSON & traffic assessment: try Redis side-channel first
    # (planner stores data there to avoid LLM passthrough corruption),
    # then fall back to plan payload and session state for backward compat.
    from agents.utils.simdata import load_simulation_data

    sim_id = tool_context.state.get("simulation_id", "")
    simdata = await load_simulation_data(sim_id)
    route_data = simdata.get("route_geojson") or plan.get("route") or tool_context.state.get("route_geojson") or {}

    # Boundary validator: reject shape-corrupt route_geojson at the wire
    # (Redis side-channel or plan payload).  The state-driven persistence
    # refactor closes the LLM-passthrough corruption path in
    # planner_with_memory; this guard catches any future regression or
    # any direct caller that bypasses the planner.  See
    # docs/plans/2026-04-19-state-driven-memory-persistence-design.md.
    if route_data:
        from agents.utils.traffic import validate_route_geojson

        ok, msg = validate_route_geojson(route_data)
        if not ok:
            logger.error("prepare_simulation: rejected corrupt route_geojson: %s", msg)
            return {"status": "error", "message": f"Invalid route_geojson: {msg}"}

        # Extract infrastructure stations for runner consumption
        try:
            from agents.utils.station_index import extract_station_index

            tool_context.state["station_index"] = extract_station_index(route_data)
        except Exception as e:
            logger.error("prepare_simulation: failed to extract station index: %s", e)

    tool_context.state["route_geojson"] = route_data

    traffic_assessment = (
        simdata.get("traffic_assessment")
        or plan.get("traffic_assessment")
        or tool_context.state.get("traffic_assessment")
    )
    if traffic_assessment:
        tool_context.state["traffic_assessment"] = traffic_assessment

    # Build traffic model from route for per-tick traffic computation
    from agents.utils.traffic import build_segment_distance_index

    if route_data and route_data.get("features"):
        segment_index = build_segment_distance_index(route_data)
        tool_context.state["traffic_model"] = {
            "segment_index": segment_index,
            "ticks_closed": {},
        }
        logger.info("prepare_simulation: built traffic model with %d segments", len(segment_index))
    else:
        logger.warning("prepare_simulation: no route data available, traffic model not built")

    tool_context.state["plan_narrative"] = plan.get("narrative", "")
    tool_context.state["plan_action"] = plan.get("action", "execute")
    tool_context.state["max_ticks"] = max_ticks
    tool_context.state["current_tick"] = 0
    tool_context.state["tick_snapshots"] = []
    tool_context.state["runner_count"] = runner_count
    tool_context.state["runner_type"] = runner_type
    tool_context.state["simulation_ready"] = True  # Guard flag
    tool_context.state["simulation_in_progress"] = True  # Re-entrancy guard

    result = {
        "status": "success",
        "message": (
            f"Decision cycle configured: {max_ticks} cycles at {tick_interval_seconds}s intervals, {runner_count} stakeholders. "
            f"Scenario: {quest.name}"
        ),
        "max_ticks": max_ticks,
        "runner_count": runner_count,
        "duration_seconds": duration_seconds,
        "tick_interval_seconds": tick_interval_seconds,
        "simulation_id": tool_context.state.get("simulation_id"),
        "quest_id": quest.id,
    }
    if original_runner_count > max_runners:
        result["capped_from"] = original_runner_count
        result["message"] = (
            f"Decision cycle configured: {max_ticks} cycles at {tick_interval_seconds}s intervals, "
            f"{runner_count} stakeholders (capped from {original_runner_count}; "
            f"max for stakeholder_type={runner_type} is {max_runners})."
        )
    return result


async def spawn_runners(count: int, tool_context: ToolContext) -> dict:
    """Spawn runner agent sessions via the gateway spawn API.

    Args:
        count: Number of runner agents to spawn.
        tool_context: ADK tool context for session state access.

    Returns:
        dict with status and list of spawned session_ids.
    """
    # Guard: prepare_simulation must have run first
    if not tool_context.state.get("simulation_ready"):
        return {
            "status": "error",
            "message": "prepare_simulation must be called before spawn_runners. "
            "It sets required state (runner_count, simulation_id).",
        }

    # Always use GATEWAY_INTERNAL_URL. All internal systems (Cloud Run,
    # Agent Engine, local) communicate via the internal URL. The gateway's
    # Cloud Run ingress must be set to "all" so that AE agents (which run
    # in Google's tenant project) can reach it. IAP protects the public
    # GATEWAY_URL for browser access; service-to-service calls bypass IAP
    # via the internal URL.
    gateway_url = os.environ.get("GATEWAY_INTERNAL_URL") or os.environ.get("GATEWAY_URL", "http://127.0.0.1:8101")
    spawn_url = f"{gateway_url}/api/v1/spawn"
    runner_type = tool_context.state.get("runner_type", DEFAULT_RUNNER_TYPE)
    simulation_id = tool_context.state.get("simulation_id", "")

    # Belt-and-suspenders: enforce the state's capped runner_count.
    # If state doesn't have runner_count (state propagation failure),
    # fall back to the environment cap for the runner type.
    capped_count = tool_context.state.get("runner_count")
    if capped_count is None:
        # State propagation failed — use environment cap
        capped_count = cap_for_runner_type(runner_type)
        logger.warning(
            "spawn_runners: runner_count NOT in state! Falling back to cap_for_runner_type(%s)=%d",
            runner_type,
            capped_count,
        )
    allow_override = os.environ.get("ALLOW_RUNNER_OVERRIDE", "false").lower() == "true"
    if count > capped_count:
        if allow_override:
            logger.warning(
                "spawn_runners: LLM requested %d runners, state caps at %d. ALLOW_RUNNER_OVERRIDE=true, allowing override.",
                count,
                capped_count,
            )
        else:
            logger.warning(
                "spawn_runners: LLM requested %d runners, but state caps at %d. Enforcing cap.",
                count,
                capped_count,
            )
            count = capped_count


    # Map echoes to tiers
    echo_configs = tool_context.state.get("echo_configs", [])
    echo_config_by_type = {"ai_echo": [], "ai_echo_reactive": [], "ai_echo_autopilot": []}
    for ec in echo_configs:
        tier = ec.get("tier", 2)
        if tier == 1:
            t = "ai_echo"
        elif tier == 3:
            t = "ai_echo_autopilot"
        else:
            t = "ai_echo_reactive"
        echo_config_by_type[t].append(ec)

    agents_payload = [{"agentType": runner_type, "count": count}]
    for t, configs in echo_config_by_type.items():
        if configs:
            agents_payload.append({"agentType": t, "count": len(configs)})

    payload = {
        "agents": agents_payload,
        "simulation_id": simulation_id,
    }


    logger.info("spawn_runners: POST %s with count=%d type=%s", spawn_url, count, runner_type)

    # OIDC for OSS Cloud Run IAM mode (gateway has roles/run.invoker enforced).
    # get_id_token=None (local dev / no ADC) -> no header, gateway accepts unauth.
    headers: dict[str, str] = {}
    audience = oidc_auth.resolve_audience(gateway_url)
    token = oidc_auth.get_id_token(audience)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(spawn_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(
                        "spawn_runners: gateway returned %d: %s",
                        resp.status,
                        error_text,
                    )
                    return {
                        "status": "error",
                        "message": f"Gateway spawn failed ({resp.status}): {error_text}",
                    }


                data = await resp.json()
                raw_sessions = data.get("sessions", [])

                # Match session IDs back to configurations
                runner_session_ids = []
                import json

                from agents.utils.redis_pool import get_shared_redis_client
                redis = get_shared_redis_client()

                for s in raw_sessions:
                    if isinstance(s, dict):
                        sid = s.get("sessionId")
                        atype = s.get("agentType")
                        if atype == runner_type or atype not in echo_config_by_type:
                            runner_session_ids.append(sid)
                        elif atype in echo_config_by_type and echo_config_by_type[atype]:
                            # Pop one config for this type and store it in Redis
                            ec = echo_config_by_type[atype].pop(0)
                            if redis:
                                redis.set(f"echo_config:{sid}", json.dumps(ec), ex=86400)
                    else:
                        runner_session_ids.append(s)

        # Store session IDs in state for downstream tools
        tool_context.state["runner_session_ids"] = runner_session_ids
        session_ids = runner_session_ids


        # Reconcile runner_count with actual spawned count.
        # The gateway may have its own MAX_RUNNERS cap that is lower than
        # the simulator's per-type cap (cap_for_runner_type), so the actual
        # spawned count can be less than requested.  Downstream tools
        # (compile_results) use state["runner_count"] for DNF calculation,
        # so it MUST reflect reality.
        actual_count = len(session_ids)
        if actual_count != count:
            logger.warning(
                "spawn_runners: requested %d but gateway spawned %d, reconciling state",
                count,
                actual_count,
            )
            tool_context.state["runner_count"] = actual_count
            sim_config = tool_context.state.get("simulation_config", {})
            if sim_config:
                sim_config["runner_count"] = actual_count
                tool_context.state["simulation_config"] = sim_config

        logger.info("spawn_runners: spawned %d runners: %s", actual_count, session_ids)

        return {
            "status": "success",
            "session_ids": session_ids,
            "count": actual_count,
            "message": f"Instantiated {actual_count} stakeholder personas",
            "simulation_id": tool_context.state.get("simulation_id"),
        }
    except Exception as e:
        logger.error("spawn_runners: failed to contact gateway: %s", e)
        return {"status": "error", "message": f"Failed to instantiate stakeholders: {e}"}


async def start_race_collector(tool_context: ToolContext) -> dict:
    """Start a RaceCollector subscribed to gateway:broadcast for runner telemetry.

    Requires runner_session_ids to be present in tool_context.state
    (set by spawn_runners).

    Args:
        tool_context: ADK tool context for session state access.

    Returns:
        dict with status and collector session info.
    """
    # Guard: prepare_simulation must have run first
    if not tool_context.state.get("simulation_ready"):
        return {
            "status": "error",
            "message": "prepare_simulation must be called before start_race_collector. "
            "It sets required state (simulation_id).",
        }

    runner_session_ids = tool_context.state.get("runner_session_ids")
    if not runner_session_ids:
        return {
            "status": "error",
            "message": "No runner_session_ids in state. Run spawn_runners first.",
        }

    session_id = tool_context.session.id

    from agents.simulator.collector import RaceCollector

    runner_type = tool_context.state.get("simulation_config", {}).get("runner_type", "")
    skip_pubsub = "autopilot" in runner_type

    try:
        await RaceCollector.start(
            session_id=session_id,
            runner_session_ids=set(runner_session_ids),
            skip_pubsub=skip_pubsub,
        )

        logger.info(
            "start_race_collector: started for session %s tracking %d runners",
            session_id,
            len(runner_session_ids),
        )

        return {
            "status": "success",
            "session_id": session_id,
            "runner_count": len(runner_session_ids),
            "message": f"Stakeholder telemetry started for {len(runner_session_ids)} stakeholders",
            "simulation_id": tool_context.state.get("simulation_id"),
        }
    except Exception as e:
        logger.error("start_race_collector: failed to start: %s", e)
        return {"status": "error", "message": f"Failed to start telemetry stream: {e}"}


async def fire_start_gun(tool_context: ToolContext) -> dict:
    """Broadcast a START_GUN signal to all runners.

    The START_GUN is a pure "go" signal carrying only race metadata
    (``max_ticks``, ``race_distance_mi``).  Runner initialization
    (velocity, distance=0) is deferred to tick 0 inside the race engine
    loop, so that the first tick reports initial state without advancing.

    Should be called after spawn_runners and start_race_collector, just
    before the race engine loop begins.

    Args:
        tool_context: ADK tool context for session state access.

    Returns:
        dict with status and event details.
    """
    runner_session_ids = tool_context.state.get("runner_session_ids")
    if not runner_session_ids:
        return {
            "status": "error",
            "message": "No runner_session_ids in state. Run spawn_runners first.",
        }

    session_id = tool_context.session.id
    simulation_id = tool_context.state.get("simulation_id")

    config = tool_context.state.get("simulation_config", {})
    max_ticks = tool_context.state.get("max_ticks", DEFAULT_MAX_TICKS)
    race_distance_mi = config.get("race_distance_mi", DEFAULT_RACE_DISTANCE_MI)

    # START_GUN is a pure signal: "the race has begun."  Runner
    # initialization (velocity, distance=0) is handled by tick 0 inside
    # the race engine loop, so the event carries only race metadata.
    start_event = RunnerEvent(
        event=RunnerEventType.START_GUN,
        data={
            "max_ticks": max_ticks,
            "race_distance_mi": race_distance_mi,
        },
    )

    # Ensure the RaceCollector is running BEFORE broadcasting so it
    # captures runner telemetry from this tick onward.  The LLM pre-race
    # agent is supposed to call start_race_collector, but this is fragile.
    # Starting here guarantees the collector is ready.
    from agents.simulator.collector import RaceCollector

    if not RaceCollector.is_running(session_id):
        try:
            await RaceCollector.start(
                session_id=session_id,
                runner_session_ids=set(runner_session_ids),
            )
            logger.info(
                "fire_start_gun: auto-started RaceCollector for session %s (%d runners)",
                session_id,
                len(runner_session_ids),
            )
        except Exception as e:
            logger.warning("fire_start_gun: failed to start RaceCollector: %s", e)

    # --- Spawn readiness gate ---
    # Wait for all runners to be registered in the simulation registry
    # before broadcasting.  Dispatchers process spawn events asynchronously
    # via BLPOP; without this gate the START_GUN fires before all runners
    # have subscribed to the broadcast channel, causing them to be lost.
    await wait_for_runners_ready(runner_session_ids, simulation_id=simulation_id, timeout_seconds=60)

    try:
        await publish_to_runners(serialize_runner_event(start_event), simulation_id=simulation_id)
    except Exception as e:
        logger.error("fire_start_gun: broadcast failed: %s", e)
        return {"status": "error", "message": f"Failed to broadcast start gun: {e}"}

    # L4: Redundant with prepare_simulation line 188, kept as safety net
    # in case fire_start_gun is invoked without prepare_simulation completing.
    tool_context.state["current_tick"] = 0

    logger.info(
        "fire_start_gun: START_GUN broadcast for session %s (%d runners)",
        session_id,
        len(runner_session_ids),
    )
    return {
        "status": "success",
        "event": "start_gun",
        "session_id": session_id,
        "runner_count": len(runner_session_ids),
        "message": f"Change initiative broadcast to {len(runner_session_ids)} stakeholders",
        "simulation_id": tool_context.state.get("simulation_id"),
    }


async def call_agent(agent_name: str, message: str, tool_context: ToolContext) -> dict:
    """Delegate inter-agent communication via the shared A2A utility.

    Args:
        agent_name: Name of the target agent (e.g., 'runner_autopilot', 'planner').
        message: The instruction or query to send.
        tool_context: ADK tool context for A2A client access.

    Returns:
        dict with status and response from the target agent.
    """
    from agents.utils.communication import call_agent as _call_agent

    return await _call_agent(agent_name=agent_name, message=message, tool_context=tool_context)
