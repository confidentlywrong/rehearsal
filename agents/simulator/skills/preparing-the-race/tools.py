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

"""Pre-race setup tools for the Simulator agent.

These tools handle simulation initialization: parsing the plan, spawning
runner agents via the gateway API, starting the Redis telemetry collector,
and delegating inter-agent communication.
"""

import json
import logging
import math
import os

import aiohttp
from google.adk.tools.tool_context import ToolContext

from agents.simulator.broadcast import publish_to_runners, wait_for_runners_ready
from agents.utils import auth as oidc_auth
from agents.utils import simulation_registry
from agents.utils import tenant_keys
from agents.utils import user_auth_context
from agents.utils.communication import resolve_tool_context_user_id, strict_json_dumps
from agents.utils.gateway import get_gateway_url
from agents.utils.simdata import SimDataError
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


def _is_positive_finite_number(value: object) -> bool:
    """Return True when value is a non-bool int/float that converts to a finite positive float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        fv = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(fv) and fv > 0


def _should_skip_pubsub(tool_context: ToolContext) -> bool:
    """Return True when the configured runner uses direct-write RPUSH instead of PubSub."""
    runner_cfg = tool_context.state.get("simulation_config") or {}
    runner_type = str(runner_cfg.get("runner_type") or "") if isinstance(runner_cfg, dict) else ""
    return "autopilot" in runner_type


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
    if not isinstance(plan, dict):
        return {"status": "error", "message": "Invalid plan JSON: top-level payload must be a JSON object."}

    from agents.simulator.quest_loader import load_quest_from_yaml, inject_quest_into_state, QuestConfig

    # Side-channel loading: load once from Redis (leadership planner stores
    # quest_config, route_geojson, and traffic_assessment there to keep the
    # A2A payload small), then fall back to payload body / session state.
    from agents.utils.simdata import load_simulation_data

    sim_id = tool_context.state.get("simulation_id", "")
    try:
        sim_user_id = resolve_tool_context_user_id(tool_context, require_when_saas=False)
        simdata = await (
            load_simulation_data(sim_id, user_id=sim_user_id)
            if sim_user_id is not None
            else load_simulation_data(sim_id)
        )
    except (SimDataError, ValueError) as e:
        logger.error("prepare_simulation: failed to load simulation data for %s: %s", sim_id, e, exc_info=True)
        return {"status": "error", "message": f"Failed to load simulation data: {e}"}

    quest_config_dict = simdata.get("quest_config") or plan.get("quest_config")

    if quest_config_dict:
        try:
            quest = QuestConfig.model_validate(quest_config_dict)
            inject_quest_into_state(tool_context.state, quest)
            logger.info("prepare_simulation: injected dynamic quest_config '%s'", quest.id)
        except Exception as e:
            logger.error("prepare_simulation: invalid dynamic quest_config: %s", e)
            return {"status": "error", "message": f"Invalid quest_config: {e}"}
    else:
        quest_id = plan.get("quest_id") or "ai_powered_disruptor"
        try:
            quest = load_quest_from_yaml(quest_id)
            inject_quest_into_state(tool_context.state, quest)
        except Exception as e:
            logger.error("prepare_simulation: failed to load quest %s: %s", quest_id, e)
            return {"status": "error", "message": f"Failed to load quest {quest_id}: {e}"}

    # Extract simulation_config with defaults (guard explicit null/non-dict)
    raw_sim_config = plan.get("simulation_config")
    sim_config = raw_sim_config if isinstance(raw_sim_config, dict) else {}
    duration_seconds = sim_config.get("duration_seconds", DEFAULT_DURATION_SECONDS)
    tick_interval_seconds = sim_config.get("tick_interval_seconds", DEFAULT_TICK_INTERVAL_SECONDS)
    if not _is_positive_finite_number(tick_interval_seconds):
        return {
            "status": "error",
            "message": f"Invalid tick_interval_seconds: {tick_interval_seconds!r} (must be > 0).",
        }
    if not _is_positive_finite_number(duration_seconds):
        return {
            "status": "error",
            "message": f"Invalid duration_seconds: {duration_seconds!r} (must be > 0).",
        }
    total_race_hours = sim_config.get("total_race_hours", 6.0)
    if not _is_positive_finite_number(total_race_hours):
        return {
            "status": "error",
            "message": f"Invalid total_race_hours: {total_race_hours!r} (must be > 0).",
        }
    race_distance_mi = sim_config.get("race_distance_mi", DEFAULT_RACE_DISTANCE_MI)
    if not _is_positive_finite_number(race_distance_mi):
        return {
            "status": "error",
            "message": f"Invalid race_distance_mi: {race_distance_mi!r} (must be > 0).",
        }
    runner_type = sim_config.get("runner_type") or DEFAULT_RUNNER_TYPE
    max_runners = cap_for_runner_type(str(runner_type))
    # Default runner_count to the cap for the chosen runner_type. This matches
    # the OSS GCP intent: runner_autopilot defaults to MAX_RUNNERS_AUTOPILOT
    # (100), runner_cloudrun / runner_gke / runner default to MAX_RUNNERS_LLM
    # (10). Local dev can dial either down via env vars to keep simulations
    # fast. The planner can always specify an explicit runner_count to
    # override.
    runner_count = sim_config.get("runner_count", max_runners)
    if not isinstance(runner_count, int) or isinstance(runner_count, bool) or runner_count <= 0:
        return {
            "status": "error",
            "message": f"Invalid runner_count: {runner_count!r} (must be a positive integer).",
        }
    original_runner_count = runner_count
    if runner_count > max_runners:
        logger.warning(
            "prepare_simulation: runner_count %d exceeds cap %d for runner_type=%s, capping",
            runner_count,
            max_runners,
            runner_type,
        )
        runner_count = max_runners
    try:
        ticks_float = float(duration_seconds // tick_interval_seconds)
    except (TypeError, ValueError, OverflowError):
        return {
            "status": "error",
            "message": "Invalid simulation duration_seconds / tick_interval_seconds ratio (overflow).",
        }
    if not math.isfinite(ticks_float):
        return {
            "status": "error",
            "message": "Invalid simulation duration_seconds / tick_interval_seconds ratio (non-finite).",
        }
    max_ticks = max(1, int(ticks_float))

    # Store ALL config in state
    tool_context.state["simulation_config"] = {
        "duration_seconds": duration_seconds,
        "tick_interval_seconds": tick_interval_seconds,
        "total_race_hours": total_race_hours,
        "runner_count": runner_count,
        "runner_type": runner_type,
        "persona": sim_config.get("persona", "pragmatist"),
        "race_distance_mi": race_distance_mi,
    }
    # Route GeoJSON & traffic assessment: reuse Redis side-channel payload,
    # then fall back to plan payload and session state for backward compat.
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
            logger.error("prepare_simulation: failed to extract station index: %s", e, exc_info=True)
            return {"status": "error", "message": f"Failed to extract station index: {e}"}

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
        if "traffic_model" in tool_context.state:
            tool_context.state["traffic_model"] = None
        if not route_data and "station_index" in tool_context.state:
            tool_context.state["station_index"] = {}
        logger.warning("prepare_simulation: no route data available, traffic model not built")

    # Reset per-run accumulators so multi-round simulations on the same
    # session never inherit stale runner/echo/coaching state from a prior run.
    for _stale_list_key in (
        "vitals_trend_full",
        "finished_runner_ids",
        "collapsed_runner_ids",
        "echo_voice_log",
        "aligned_echo_ids",
        "defected_echo_ids",
        "decision_log",
        "runner_session_ids",
        "echo_session_ids",
    ):
        if _stale_list_key in tool_context.state:
            tool_context.state[_stale_list_key] = []
    for _stale_dict_key in (
        "global_runner_states",
        "route_friction_map",
        "_echo_states",
        "influence_deltas",
        "_prev_tick_resources",
        "_prev_medical_saves",
    ):
        if _stale_dict_key in tool_context.state:
            tool_context.state[_stale_dict_key] = {}
    for _stale_none_key in (
        "last_coaching_signature",
        "last_resource_snapshot",
        "_raw_principle_scores",
        "influence_network",
    ):
        if _stale_none_key in tool_context.state:
            tool_context.state[_stale_none_key] = None
    if "_principle_scores_normalized" in tool_context.state:
        tool_context.state["_principle_scores_normalized"] = False

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

    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return {
            "status": "error",
            "message": f"Invalid runner count: {count!r}. Expected a positive integer.",
        }

    # Always use GATEWAY_INTERNAL_URL. All internal systems (Cloud Run,
    # Agent Engine, local) communicate via the internal URL. The gateway's
    # Cloud Run ingress must be set to "all" so that AE agents (which run
    # in Google's tenant project) can reach it. IAP protects the public
    # GATEWAY_URL for browser access; service-to-service calls bypass IAP
    # via the internal URL.
    gateway_url = get_gateway_url()
    spawn_url = f"{gateway_url}/api/v1/spawn"
    runner_type = tool_context.state.get("runner_type", DEFAULT_RUNNER_TYPE)
    simulation_id = tool_context.state.get("simulation_id", "")

    # Belt-and-suspenders: enforce the state's capped runner_count.
    # If state doesn't have runner_count (state propagation failure),
    # fall back to the environment cap for the runner type.
    capped_count = tool_context.state.get("runner_count")
    if not isinstance(capped_count, int) or isinstance(capped_count, bool) or capped_count <= 0:
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
    raw_echo_configs = tool_context.state.get("echo_configs")
    echo_configs = raw_echo_configs if isinstance(raw_echo_configs, list) else []
    echo_config_by_type = {"ai_echo": [], "ai_echo_reactive": [], "ai_echo_autopilot": []}
    for ec in echo_configs:
        if not isinstance(ec, dict):
            continue
        raw_tier = ec.get("tier", 2)
        tier = raw_tier if isinstance(raw_tier, int) and not isinstance(raw_tier, bool) and raw_tier in (1, 2, 3) else 2
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

    try:
        spawn_user_id = resolve_tool_context_user_id(tool_context, require_when_saas=False)
    except ValueError as e:
        logger.error("spawn_runners: invalid user_id context: %s", e)
        return {"status": "error", "message": str(e)}

    # OIDC for OSS Cloud Run IAM mode (gateway has roles/run.invoker enforced).
    # get_id_token=None (local dev / no ADC) -> no header, gateway accepts unauth.
    # Also forward the per-user Firebase/session token from user_auth_context
    # (Authorization + Cookie when no OIDC header is attached, or X-User-Authorization
    # + Cookie when OIDC occupies Authorization).
    # Inside AgentTool(simulation_pipeline) sub-invocations, ADK creates a fresh
    # random child session.id that differs from the outer session_id/context_id,
    # while preserving state["simulation_id"]. Check ordered candidate IDs.
    user_token: str | None = None
    raw_session = getattr(tool_context, "session", None) if tool_context is not None else None
    raw_session_id = getattr(raw_session, "id", None) if raw_session is not None else None

    async def _safe_get_context_id(sid: str) -> str | None:
        try:
            if spawn_user_id is not None:
                return await simulation_registry.get_context_id(sid, user_id=spawn_user_id)
            return await simulation_registry.get_context_id(sid)
        except ValueError as exc:
            logger.debug("spawn_runners: skipped context_id lookup for %s (%s)", sid, exc)
            return None

    candidate_ids: list[str] = []
    seen_ids: set[str] = set()

    def _add_candidate(cid: object) -> None:
        if isinstance(cid, str) and cid.strip() and cid not in seen_ids:
            seen_ids.add(cid)
            candidate_ids.append(cid)

    if isinstance(raw_session_id, str) and raw_session_id.strip():
        _add_candidate(raw_session_id)
        _add_candidate(await _safe_get_context_id(raw_session_id))

    if isinstance(simulation_id, str) and simulation_id.strip() and simulation_id.strip() != "\u2014":
        _add_candidate(simulation_id)
        _add_candidate(await _safe_get_context_id(simulation_id))

    for cand_id in candidate_ids:
        tok = user_auth_context.get_session_token(cand_id)
        if tok and user_auth_context._is_valid_bearer_token(tok):
            user_token = tok
            break

    headers: dict[str, str] = {}
    audience = oidc_auth.resolve_audience(gateway_url)
    token = oidc_auth.get_id_token(audience)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if user_token:
        if "Authorization" in headers:
            headers["X-User-Authorization"] = f"Bearer {user_token}"
        else:
            headers["Authorization"] = f"Bearer {user_token}"
        headers["Cookie"] = f"__session={user_token}"

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
                echo_session_ids = []
                import inspect

                from agents.utils.redis_pool import get_shared_redis_client
                redis = get_shared_redis_client()

                for s in raw_sessions:
                    if isinstance(s, dict):
                        sid = s.get("sessionId")
                        atype = s.get("agentType")
                        if atype == runner_type or atype not in echo_config_by_type:
                            if sid:
                                runner_session_ids.append(sid)
                        elif atype in echo_config_by_type:
                            if sid:
                                echo_session_ids.append(sid)
                            if echo_config_by_type[atype]:
                                # Pop one config for this type and store it in Redis
                                ec = dict(echo_config_by_type[atype].pop(0))
                                quest_principles = tool_context.state.get("principles")
                                if quest_principles and "_principles" not in ec:
                                    ec["_principles"] = list(quest_principles)
                                if redis and sid:
                                    maybe_set = redis.set(
                                        tenant_keys.echo_config_key(sid, user_id=spawn_user_id),
                                        strict_json_dumps(ec),
                                        ex=86400,
                                    )
                                    if inspect.isawaitable(maybe_set):
                                        await maybe_set
                    elif s:
                        runner_session_ids.append(s)

        # Store session IDs in state for downstream tools
        tool_context.state["runner_session_ids"] = runner_session_ids
        tool_context.state["echo_session_ids"] = echo_session_ids
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
            raw_sim_config = tool_context.state.get("simulation_config")
            if isinstance(raw_sim_config, dict) and raw_sim_config:
                sim_config = dict(raw_sim_config)
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

    skip_pubsub = _should_skip_pubsub(tool_context)
    echo_session_ids = tool_context.state.get("echo_session_ids") or []
    tracked_session_ids = set(runner_session_ids) | set(echo_session_ids)

    try:
        await RaceCollector.start(
            session_id=session_id,
            runner_session_ids=tracked_session_ids,
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

    config = tool_context.state.get("simulation_config") or {}
    max_ticks = tool_context.state.get("max_ticks") or DEFAULT_MAX_TICKS
    race_distance_mi = config.get("race_distance_mi") or DEFAULT_RACE_DISTANCE_MI

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

    echo_session_ids = tool_context.state.get("echo_session_ids") or []
    tracked_session_ids = set(runner_session_ids) | set(echo_session_ids)

    skip_pubsub = _should_skip_pubsub(tool_context)
    if not RaceCollector.is_running(session_id):
        try:
            await RaceCollector.start(
                session_id=session_id,
                runner_session_ids=tracked_session_ids,
                skip_pubsub=skip_pubsub,
            )
            logger.info(
                "fire_start_gun: auto-started RaceCollector for session %s (%d runners, skip_pubsub=%s)",
                session_id,
                len(runner_session_ids),
                skip_pubsub,
            )
        except Exception as e:
            logger.error("fire_start_gun: failed to start RaceCollector: %s", e, exc_info=True)
            return {"status": "error", "message": f"Failed to start telemetry stream: {e}"}

    # --- Spawn readiness gate ---
    # Wait for all runners (and AI Echoes) to be registered in the simulation
    # registry before broadcasting.  Dispatchers process spawn events
    # asynchronously via BLPOP; without this gate the START_GUN fires before
    # all agents have subscribed to the broadcast channel, causing them to be lost.
    all_spawned_ids = list(runner_session_ids) + list(echo_session_ids)
    try:
        await wait_for_runners_ready(all_spawned_ids, simulation_id=simulation_id, timeout_seconds=60)
    except Exception as e:
        logger.error("fire_start_gun: readiness gate failed: %s", e, exc_info=True)
        return {"status": "error", "message": f"Failed waiting for stakeholders to register: {e}"}

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
