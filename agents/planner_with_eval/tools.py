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

"""Planner-with-eval specific tools — simulator collaboration."""

import asyncio
import logging
from typing import Any
from uuid import uuid4

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_SIMULATOR_FAILURE_PREFIXES = ("Request failed:", "No request provided")
_SIMULATOR_FAILURE_SUFFIX = "failed to generate response"
_SIMULATOR_PROGRESS_PREFIX = "Processing request..."


def _simulator_failure_reason(response: object) -> str | None:
    """Return a failure description if the simulator call failed, else None.

    Handles call_agent error dicts (``{"status": "error", "message": ...}``),
    relayed A2A task failure dicts (``{"status": "success", "response": "Request failed: ..."}``),
    and raw failure strings (``"Request failed: ..."`` / ``"... failed to generate response"``).
    """
    text: str | None = None
    if isinstance(response, dict):
        if response.get("status") == "error":
            return str(response.get("message") or response.get("error") or "unknown simulator error")
        body = response.get("response")
        if isinstance(body, str):
            text = body
    elif isinstance(response, str):
        text = response

    if isinstance(text, str):
        cleaned = text.strip()
        while cleaned.startswith(_SIMULATOR_PROGRESS_PREFIX):
            cleaned = cleaned[len(_SIMULATOR_PROGRESS_PREFIX) :].lstrip()
        if cleaned.startswith(_SIMULATOR_FAILURE_PREFIXES) or cleaned.endswith(_SIMULATOR_FAILURE_SUFFIX):
            return cleaned[:500]
    return None


async def start_simulation(
    action: str = "execute",
    message: str = "Executing marathon simulation",
    tool_context: ToolContext = None,
    simulation_config: dict | None = None,
    runner_type: str | None = None,
) -> dict:
    """Initialize a simulation session and return the simulation_id.

    This tool returns immediately, making the simulation_id visible in the
    telemetry dashboard (via the DashLogPlugin tool_end callback) BEFORE
    the simulation actually begins.

    Call this BEFORE submit_plan_to_simulator(action="execute").

    Args:
        action: Either 'verify' or 'execute'.
        message: A narrative summary of the plan for the simulator.
        tool_context: ADK tool context.
        simulation_config: Optional simulation config dict.
        runner_type: Optional runner type (e.g. 'runner', 'runner_gke',
            'runner_cloudrun'). Injected into simulation_config.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if not tool_context or state is None or not hasattr(state, "get"):
        return {"status": "error", "message": "No tool context available."}

    route_geojson = state.get("marathon_route")
    if not route_geojson:
        return {
            "status": "error",
            "message": "No marathon route found in session state. Run 'plan_marathon_route' first.",
        }

    # Re-entrancy guard: if a simulation was already executed in the SAME
    # invocation (LLM response), reject. The invocation_id changes when a
    # new user message arrives, so legitimate re-runs are allowed.
    executed_in = state.get("simulation_executed_invocation")
    if executed_in and executed_in == tool_context.invocation_id:
        logger.warning("start_simulation: simulation already executed in this invocation, rejecting")
        return {
            "status": "error",
            "message": "Simulation already complete. Do NOT call start_simulation or "
            "submit_plan_to_simulator again. Proceed to: "
            "record_simulation, then validate_and_emit_a2ui.",
        }

    if runner_type:
        simulation_config = dict(simulation_config) if simulation_config else {}
        simulation_config["runner_type"] = runner_type

    simulator_session_id = str(uuid4())

    # Write a unique token BEFORE setting the session ID so that
    # submit_plan_to_simulator (running concurrently) can detect
    # a fresh start_simulation vs a stale value from a prior run.
    state["_sim_token"] = simulator_session_id
    state["simulator_session_id"] = simulator_session_id

    # Set simulation_id in planner state so all subsequent messages carry it.
    state["simulation_id"] = simulator_session_id

    # Persist simulation_config so submit_plan_to_simulator can read it
    # from state when the LLM omits it (the "carry forward" contract).
    if simulation_config:
        state["simulation_config"] = simulation_config

    from agents.utils.gateway import register_simulation_with_gateway

    await register_simulation_with_gateway(simulator_session_id, tool_context=tool_context)

    logger.info(f"PLANNER: Simulation initialized (simulation_id={simulator_session_id}, action={action})")

    return {
        "status": "ready",
        "simulation_id": simulator_session_id,
        "action": action,
        "message": f"Simulation session initialized. Call submit_plan_to_simulator to {action}.",
    }


# [START submit_to_simulator]
async def submit_plan_to_simulator(
    action: str = "execute",
    message: str = "Executing marathon simulation",
    tool_context: ToolContext = None,
    simulation_config: dict | None = None,
    runner_type: str | None = None,
) -> dict:
    """Submit the marathon plan to the simulator agent for verification or execution.

    For execute actions, call start_simulation first to initialize the session
    and make the simulation_id visible in the telemetry dashboard.

    Args:
        action: Either 'verify' or 'execute'.
        message: A narrative summary of the plan for the simulator.
        tool_context: ADK tool context.
        simulation_config: Optional config dict with keys: duration_seconds,
            tick_interval_seconds, runner_count.
        runner_type: Optional runner type (e.g. 'runner', 'runner_gke',
            'runner_cloudrun'). Injected into simulation_config.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if not tool_context or state is None or not hasattr(state, "get"):
        return {"status": "error", "message": "No tool context available."}

    from agents.utils.communication import call_agent, resolve_tool_context_user_id, strict_json_dumps

    logger.info(f"PLANNER: Submitting plan to simulator with action: {action}")

    route_geojson = state.get("marathon_route")
    if not route_geojson:
        return {
            "status": "error",
            "message": "No marathon route found in session state. Run 'plan_marathon_route' first.",
        }

    # Re-entrancy guard: block duplicate execute calls within the SAME
    # invocation (LLM response). The LLM may ignore prompt STOP instructions
    # and loop, calling this tool multiple times. This code-level guard is
    # deterministic — it cannot be bypassed.
    # The invocation_id changes when a new user message arrives, so
    # legitimate user-initiated re-runs (e.g. "Re-run Simulation" button)
    # are allowed because they start a new invocation.
    executed_in = state.get("simulation_executed_invocation")
    if action == "execute" and executed_in and executed_in == tool_context.invocation_id:
        logger.warning("submit_plan_to_simulator: simulation already executed in this invocation, rejecting duplicate")
        return {
            "status": "error",
            "message": "Simulation already complete. Do NOT call start_simulation or "
            "submit_plan_to_simulator again. Proceed to: "
            "record_simulation, then validate_and_emit_a2ui.",
        }

    try:
        resolved_user_id = resolve_tool_context_user_id(tool_context)
    except ValueError as exc:
        logger.error("PLANNER: Cannot submit simulation without valid user_id: %s", exc)
        return {"status": "error", "message": str(exc)}

    if runner_type:
        simulation_config = dict(simulation_config) if simulation_config else {}
        simulation_config["runner_type"] = runner_type

    # Wait for start_simulation to write a FRESH simulator_session_id.
    # Both tools are dispatched in the same LLM response.  The state
    # may already contain a stale simulator_session_id from a prior
    # simulation on this session.  We use _sim_token (written by
    # start_simulation just before simulator_session_id) to detect
    # freshness: when _sim_token == simulator_session_id, the value
    # was written by the current start_simulation call, not a prior one.
    simulator_session_id = tool_context.state.get("simulator_session_id")
    sim_token = tool_context.state.get("_sim_token")

    # Poll until start_simulation writes a fresh value (token matches ID)
    # or until we timeout (2s).
    if not simulator_session_id or simulator_session_id != sim_token:
        for _ in range(20):  # 20 x 0.1s = 2s max
            await asyncio.sleep(0.1)
            simulator_session_id = tool_context.state.get("simulator_session_id")
            sim_token = tool_context.state.get("_sim_token")
            if simulator_session_id and simulator_session_id == sim_token:
                break
    if not simulator_session_id:
        if action == "execute":
            # Re-check guard after async wait to prevent double-execution on rapid clicks
            executed_in = tool_context.state.get("simulation_executed_invocation")
            if executed_in and executed_in == tool_context.invocation_id:
                logger.warning("submit_plan_to_simulator: simulation already executed in this invocation during wait, rejecting fallback")
                return {
                    "status": "error",
                    "message": "Simulation already complete. Do NOT call start_simulation or submit_plan_to_simulator again. Proceed to: record_simulation, then validate_and_emit_a2ui.",
                }
            # The LLM skipped start_simulation. Call it ourselves so the
            # frontend receives the simulation_id through the normal channel
            # (DashLogPlugin tool_end callback).
            logger.warning("submit_plan_to_simulator: start_simulation was not called; invoking it now")
            start_result = await start_simulation(
                action=action,
                message=message,
                tool_context=tool_context,
                simulation_config=simulation_config,
            )
            simulator_session_id = start_result.get("simulation_id") or str(uuid4())
            if start_result.get("status") == "error":
                return start_result
        else:
            # For verify, just generate a session ID without setting
            # simulation_id (which would poison the frontend).
            simulator_session_id = str(uuid4())
            tool_context.state["simulator_session_id"] = simulator_session_id

    # Set simulation_id for plugin emissions only during execute.
    # During verify, simulation_id must NOT be set — it causes the
    # frontend to receive simulator events on an unknown session,
    # triggering an unsubscribe that drops all subsequent messages
    # (including the tool_end for this tool and the A2UI card).
    if action == "execute" and not tool_context.state.get("simulation_id"):
        tool_context.state["simulation_id"] = simulator_session_id

    # Store large data in Redis side-channel (route GeoJSON, traffic
    # assessment) so the simulator can read it directly without the LLM
    # having to pass multi-KB JSON through function call arguments.
    from agents.utils.simdata import SimDataError, store_simulation_data

    try:
        store_kwargs: dict[str, Any] = {
            "simulation_id": simulator_session_id,
            "route_geojson": route_geojson,
            "traffic_assessment": tool_context.state.get("traffic_assessment"),
        }
        if resolved_user_id is not None:
            store_kwargs["user_id"] = resolved_user_id
        stored = await store_simulation_data(**store_kwargs)
    except SimDataError as exc:
        logger.error(
            "submit_plan_to_simulator: failed to store simulation data in Redis for %s: %s",
            simulator_session_id,
            exc,
        )
        return {
            "status": "error",
            "message": f"Failed to store simulation data in Redis: {exc}",
        }

    # Build the payload from this tool's own arguments.
    # Fall back to simulation_config stored by start_simulation if not
    # passed directly (the "carry forward" contract).
    effective_config = simulation_config or tool_context.state.get("simulation_config")
    payload: dict[str, object] = {
        "action": action,
        "narrative": message,
        # Pass simulation_id in the payload body, not just as the A2A
        # context_id.  On Agent Engine the A2A transport may assign a
        # different context_id, so the simulator must read the
        # authoritative simulation_id from the payload.
        "simulation_id": simulator_session_id,
    }
    if resolved_user_id is not None:
        payload["user_id"] = resolved_user_id
    if effective_config:
        payload["simulation_config"] = effective_config
    # When Redis is unavailable, fall back to including route and traffic
    # data directly in the A2A payload so the simulator can still proceed.
    if not stored:
        logger.warning("submit_plan_to_simulator: Redis unavailable, including route in A2A payload as fallback")
        if route_geojson:
            payload["route"] = route_geojson
        ta = tool_context.state.get("traffic_assessment")
        if ta:
            payload["traffic_assessment"] = ta

    try:
        call_kwargs: dict[str, Any] = {
            "tool_context": tool_context,
            "agent_name": "simulator",
            "message": strict_json_dumps(payload),
            "session_id": simulator_session_id,
        }
        if resolved_user_id is not None:
            call_kwargs["user_id"] = resolved_user_id
        response = await call_agent(**call_kwargs)
        failure = _simulator_failure_reason(response)
        if failure:
            logger.error(
                "PLANNER: Simulator %s failed for simulation %s: %s",
                action,
                simulator_session_id,
                failure,
            )
            tool_context.state["simulation_error"] = failure
            tool_context.state["simulation_result"] = None
            return {
                "status": "error",
                "simulation_id": simulator_session_id,
                "message": f"Simulator {action} failed: {failure}",
            }

        # Persist the simulator response to session state so downstream
        # memory tools (record_simulation) can
        # read it without the LLM having to transcribe the JSON back
        # through a function-call argument.
        tool_context.state["simulation_result"] = response
        tool_context.state["simulation_error"] = None

        # Mark execution complete so subsequent LLM loops are blocked.
        # Store the invocation_id so the guard is scoped to this invocation.
        # A new user message starts a new invocation, allowing re-runs.
        if action == "execute":
            tool_context.state["simulation_executed_invocation"] = tool_context.invocation_id

        return {
            "status": "success",
            "simulation_id": simulator_session_id,
            "message": f"Successfully sent plan to simulator agent: {action}",
            "simulator_response": response,
        }
    except Exception as e:
        logger.error(f"PLANNER: Failed to call simulator agent: {e}")
        return {"status": "error", "message": f"Failed to call simulator agent: {str(e)}"}


# [END submit_to_simulator]
