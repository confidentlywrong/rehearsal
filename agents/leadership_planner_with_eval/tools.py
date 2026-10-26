# Copyright 2026 The Confidently Wrong Authors
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

"""Leadership planner-with-eval specific tools — simulator collaboration.

Mirrors agents/planner_with_eval/tools.py but submits QuestConfig-compatible
scenario data instead of marathon route GeoJSON.
"""

import asyncio
import json
import logging
import pathlib
from uuid import uuid4

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default route GeoJSON for 3D map visualization (Option C: Hybrid HUD).
# Runners move along this route on the 3D map while leadership metrics
# are overlaid via the HUD. Uses the same Las Vegas Strip route as the
# marathon demos.
# ---------------------------------------------------------------------------


def _load_default_route_geojson() -> dict | None:
    """Load the default route GeoJSON from cached_routes/cache-1.json.

    Returns the parsed GeoJSON dict, or None if the file is not available.
    """
    # The cached routes live in the frontend build directory, but also
    # in the repository root at web/frontend/cached_routes/
    candidates = [
        pathlib.Path(__file__).resolve().parents[2] / "web" / "frontend" / "cached_routes" / "cache-1.json",
        pathlib.Path(__file__).resolve().parents[3] / "web" / "frontend" / "cached_routes" / "cache-1.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                route_geojson = json.loads(path.read_text())
                logger.info("Loaded default route GeoJSON from %s", path)
                return route_geojson
            except Exception as e:
                logger.warning("Failed to load default route from %s: %s", path, e)

    logger.warning("No default route GeoJSON found in any candidate path")
    return None


def _normalize_sim_config(
    simulation_config: dict | None,
    runner_type: str | None,
) -> dict:
    """Normalize simulation_config: set default runner_type, alias stakeholder_count.

    L18/L19 fix: Extracted from duplicate blocks in start_simulation and
    submit_plan_to_simulator. Only overrides runner_type when the caller
    did not explicitly set one in simulation_config.
    """
    simulation_config = simulation_config.copy() if simulation_config else {}
    # Only default runner_type if neither the caller nor config provided one.
    if "runner_type" not in simulation_config:
        runner_type = runner_type or "leadership_runner_autopilot"
        simulation_config["runner_type"] = runner_type
    # Alias stakeholder_count to runner_count for simulator compatibility
    if "stakeholder_count" in simulation_config:
        simulation_config["runner_count"] = simulation_config.pop("stakeholder_count")
    return simulation_config


def _ensure_runner_count_from_scenario(simulation_config: dict, scenario: dict | None) -> dict:
    """Defensive fallback: if LLM forgot to pass stakeholder_count, extract
    from the designed scenario in state.

    Prevents the "1 runner" bug where the LLM omits simulation_config entirely
    and downstream defaults shrink the simulation to a token size. The
    scenario's stakeholder_count is the source of truth (set by
    design_leadership_scenario; default 1000).
    """
    if "runner_count" in simulation_config:
        return simulation_config
    if not scenario:
        return simulation_config
    scenario_count = None
    if isinstance(scenario, dict):
        scenario_count = scenario.get("stakeholder_count")
    if scenario_count and isinstance(scenario_count, int) and scenario_count > 0:
        simulation_config["runner_count"] = scenario_count
        logger.info(
            "LEADERSHIP_PLANNER: simulation_config missing runner_count; "
            "auto-populated from scenario.stakeholder_count=%d",
            scenario_count,
        )
    return simulation_config


# Failure texts emitted by agents.utils.simulation_executor when the
# simulator's A2A task ends in TaskState.failed. call_agent surfaces these as
# the response body of a {"status": "success"} dict, so they must be detected
# explicitly.
_SIMULATOR_FAILURE_PREFIXES = ("Request failed:", "No request provided")
_SIMULATOR_FAILURE_SUFFIX = "failed to generate response"
_SIMULATOR_PROGRESS_PREFIX = "Processing request..."

# Per-run telemetry copied from the simulation side-channel into planner
# state. Reset to these empty values at the start of every execute run.
_PER_RUN_STATE_DEFAULTS: dict[str, list | dict] = {
    "echo_voice_log": [],
    "decision_log": [],
    "contradiction_log": [],
    "route_friction_map": {},
    "alert_history": [],
    "resources": {},
    "triggered_black_swans": [],
    "vitals_trend": [],
    "defected_echo_ids": [],
    "aligned_echo_ids": [],
}


def _simulator_failure_reason(response: object) -> str | None:
    """Return a failure description if the simulator call failed, else None.

    Handles both shapes of failure:
      * call_agent caught an exception → ``{"status": "error", "message": ...}``
      * the simulator task failed → ``{"status": "success", "response":
        "Request failed: <error>"}`` (failure text relayed as the body).
    """
    if not isinstance(response, dict):
        return None
    if response.get("status") == "error":
        return str(response.get("message") or "unknown simulator error")
    body = response.get("response")
    if isinstance(body, str):
        # call_agent concatenates every status message of the A2A task, so
        # the executor's progress note precedes the final failure text.
        text = body.strip()
        while text.startswith(_SIMULATOR_PROGRESS_PREFIX):
            text = text[len(_SIMULATOR_PROGRESS_PREFIX):].lstrip()
        if text.startswith(_SIMULATOR_FAILURE_PREFIXES) or text.endswith(_SIMULATOR_FAILURE_SUFFIX):
            return text[:500]
    return None


async def start_simulation(
    action: str,
    message: str,
    tool_context: ToolContext,
    simulation_config: dict | None = None,
    runner_type: str | None = None,
) -> dict:
    """Initialize a leadership simulation session and return the simulation_id.

    This tool returns immediately, making the simulation_id visible in the
    telemetry dashboard (via the DashLogPlugin tool_end callback) BEFORE
    the simulation actually begins.

    Call this BEFORE submit_plan_to_simulator(action="execute").

    Args:
        action: Either 'verify' or 'execute'.
        message: A narrative summary of the scenario for the simulator.
        tool_context: ADK tool context.
        simulation_config: Optional simulation config dict (accepts stakeholder_count).
        runner_type: Optional runner type (e.g. 'runner', 'runner_gke',
            'runner_cloudrun'). Injected into simulation_config.
    """
    tool_context.state["simulator_session_id"] = None
    scenario = tool_context.state.get("leadership_scenario")
    if not scenario:
        return {
            "status": "error",
            "message": "No leadership scenario found in session state. Run 'design_leadership_scenario' first.",
        }

    # Re-entrancy guard: if a simulation was already executed in the SAME
    # invocation (LLM response), reject. The invocation_id changes when a
    # new user message arrives, so legitimate re-runs are allowed.
    executed_in = tool_context.state.get("simulation_executed_invocation")
    if executed_in and executed_in == tool_context.invocation_id:
        logger.warning("start_simulation: simulation already executed in this invocation, rejecting")
        return {
            "status": "error",
            "message": "Simulation already complete. Do NOT call start_simulation or "
            "submit_plan_to_simulator again. Proceed to: "
            "emit_sim_results_a2ui, then emit_action_plan_a2ui.",
        }

    simulation_config = _normalize_sim_config(simulation_config, runner_type)
    simulation_config = _ensure_runner_count_from_scenario(simulation_config, scenario)

    simulator_session_id = str(uuid4())

    # Write a unique token BEFORE setting the session ID so that
    # submit_plan_to_simulator (running concurrently) can detect
    # a fresh start_simulation vs a stale value from a prior run.
    tool_context.state["_sim_token"] = simulator_session_id
    tool_context.state["simulator_session_id"] = simulator_session_id

    # Set simulation_id in planner state so all subsequent messages carry it.
    tool_context.state["simulation_id"] = simulator_session_id

    # Persist simulation_config so submit_plan_to_simulator can read it
    # from state when the LLM omits it (the "carry forward" contract).
    # Always create config dict so persona is injected even when the LLM
    if "persona" not in simulation_config:
        simulation_config["persona"] = tool_context.state.get("selected_persona", "pragmatist")
    tool_context.state["simulation_config"] = simulation_config
    tool_context.state["expected_stakeholder_count"] = simulation_config.get("runner_count", 1_000)

    logger.info(f"LEADERSHIP_PLANNER: Simulation initialized (simulation_id={simulator_session_id}, action={action})")

    return {
        "status": "ready",
        "simulation_id": simulator_session_id,
        "action": action,
        "message": f"Leadership simulation session initialized. Call submit_plan_to_simulator to {action}.",
    }


# [START submit_to_simulator]
async def submit_plan_to_simulator(
    action: str,
    message: str,
    tool_context: ToolContext,
    simulation_config: dict | None = None,
    runner_type: str | None = None,
) -> dict:
    """Submit the leadership scenario to the simulator agent for execution.

    For execute actions, call start_simulation first to initialize the session
    and make the simulation_id visible in the telemetry dashboard.

    The leadership scenario (QuestConfig-compatible dict) is passed as
    quest_config in the payload. The simulator's prepare_simulation reads it
    and injects it into session state via inject_quest_into_state().

    Args:
        action: Either 'verify' or 'execute'.
        message: A narrative summary of the scenario for the simulator.
        tool_context: ADK tool context.
        simulation_config: Optional config dict with keys: stakeholder_count.
        runner_type: Optional runner type (e.g. 'runner', 'runner_gke',
            'runner_cloudrun'). Injected into simulation_config.
    """
    from agents.utils.communication import call_agent

    simulation_config = _normalize_sim_config(simulation_config, runner_type)

    logger.info(f"LEADERSHIP_PLANNER: Submitting scenario to simulator with action: {action}")

    scenario = tool_context.state.get("leadership_scenario")
    if not scenario:
        return {
            "status": "error",
            "message": "No leadership scenario found in session state. Run 'design_leadership_scenario' first.",
        }

    # Defensive: if LLM omitted stakeholder_count, fall back to the scenario's
    # designed value so the simulator doesn't shrink to a default of 1.
    simulation_config = _ensure_runner_count_from_scenario(simulation_config, scenario)

    # Re-entrancy guard: block duplicate execute calls within the SAME invocation.
    executed_in = tool_context.state.get("simulation_executed_invocation")
    if action == "execute" and executed_in and executed_in == tool_context.invocation_id:
        logger.warning("submit_plan_to_simulator: simulation already executed in this invocation, rejecting duplicate")
        return {
            "status": "error",
            "message": "Simulation already complete. Do NOT call start_simulation or "
            "submit_plan_to_simulator again. Proceed to: "
            "emit_sim_results_a2ui, then emit_action_plan_a2ui.",
        }

    # Wait for start_simulation to write a FRESH simulator_session_id.
    # Both tools are dispatched in the same LLM response. Use _sim_token
    # to detect freshness.
    simulator_session_id = tool_context.state.get("simulator_session_id")
    sim_token = tool_context.state.get("_sim_token")

    if not simulator_session_id or simulator_session_id != sim_token:
        if action == "execute":
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
                    "message": "Simulation already complete. Do NOT call start_simulation or submit_plan_to_simulator again. Proceed to: emit_sim_results_a2ui, then emit_action_plan_a2ui.",
                }
            # The LLM skipped start_simulation. Call it ourselves.
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
            simulator_session_id = str(uuid4())
            tool_context.state["simulator_session_id"] = simulator_session_id

    # Set simulation_id for plugin emissions only during execute.
    if action == "execute" and not tool_context.state.get("simulation_id"):
        tool_context.state["simulation_id"] = simulator_session_id

    # Store large data in Redis side-channel, following the upstream pattern.
    # The quest_config (dynamic leadership scenario) is stored alongside the
    # default route GeoJSON. This keeps the A2A payload small — only action,
    # narrative, and simulation_id — so gemini-flash-lite can reliably parse
    # the action field and route to simulation_pipeline.
    from agents.utils.simdata import store_simulation_data

    route_geojson = _load_default_route_geojson()
    stored = await store_simulation_data(
        simulation_id=simulator_session_id,
        route_geojson=route_geojson,
        quest_config=scenario,
    )
    if not stored:
        logger.warning("submit_plan_to_simulator: failed to store data in Redis")

    # Build the payload — keep it small (upstream pattern).
    # Large data (route, quest_config) is in Redis. Only included in
    # the payload as fallback when Redis is unavailable.
    effective_config = simulation_config or tool_context.state.get("simulation_config")
    payload: dict[str, object] = {
        "action": action,
        "narrative": message,
        "simulation_id": simulator_session_id,
    }
    if effective_config:
        payload["simulation_config"] = effective_config
    # When Redis is unavailable, fall back to including data directly
    # in the A2A payload so the simulator can still proceed (upstream parity).
    if not stored:
        logger.warning("submit_plan_to_simulator: Redis unavailable, including data in A2A payload as fallback")
        payload["quest_config"] = scenario
        if route_geojson:
            payload["route"] = route_geojson

    try:
        response = await call_agent(
            tool_context=tool_context,
            agent_name="simulator",
            message=json.dumps(payload),
            session_id=simulator_session_id,
        )

        # Load simulation telemetry from Redis side-channel. Post-simulation
        # tools (result cards, translation diagnostic, stakeholder
        # psychology) read echo_voice_log, resources, vitals_trend etc.
        sim_data: dict | None = None
        try:
            from agents.utils.simdata import load_simulation_data
            sim_data = await load_simulation_data(simulator_session_id)
        except Exception:
            logger.warning("submit_plan_to_simulator: failed to load side-channel data",
                           exc_info=True)

        if action == "execute":
            # Reset per-run telemetry so a field missing from this run's
            # side-channel (or a failed run) cannot carry over the previous
            # run's values into the result cards and diagnostics.
            for sk, empty in _PER_RUN_STATE_DEFAULTS.items():
                tool_context.state[sk] = type(empty)()

        failure = _simulator_failure_reason(response)
        # compile_results writes vitals_trend to the side-channel, so its
        # presence proves the simulation pipeline ran to completion even when
        # the simulator's final relay message is empty or errored.
        has_compiled_results = action == "execute" and bool(
            isinstance(sim_data, dict) and sim_data.get("vitals_trend")
        )
        if failure and has_compiled_results:
            logger.warning(
                "LEADERSHIP_PLANNER: Simulator reported '%s' for simulation %s, "
                "but compiled results exist in the side-channel; treating as completed",
                failure,
                simulator_session_id,
            )
            response = {
                "status": "success",
                "agent": "simulator",
                "response": "Simulation completed; results loaded from the simulation data side-channel.",
                "simulator_warning": failure,
            }
        elif failure:
            logger.error(
                "LEADERSHIP_PLANNER: Simulator %s failed for simulation %s: %s",
                action,
                simulator_session_id,
                failure,
            )
            tool_context.state["simulation_error"] = failure
            tool_context.state["simulation_result"] = None
            return {
                "status": "error",
                "simulation_id": simulator_session_id,
                "message": (
                    f"The simulator failed and produced no simulation results: {failure}. "
                    "Tell the user the simulation failed, quote this error, and end your turn here."
                ),
            }

        # Persist the simulator response to session state
        tool_context.state["simulation_result"] = response
        tool_context.state["simulation_error"] = None

        # Mark execution complete so subsequent LLM loops are blocked.
        if action == "execute":
            tool_context.state["simulation_executed_invocation"] = tool_context.invocation_id

        if isinstance(sim_data, dict) and sim_data:
            for sk in (
                "echo_voice_log",
                "decision_log",
                "contradiction_log",
                "route_friction_map",
                "alert_history",
                "resources",
                "triggered_black_swans",
                "vitals_trend",
            ):
                if sim_data.get(sk) is not None:
                    tool_context.state[sk] = sim_data[sk]
            terminal = sim_data.get("terminal_echo_ids")
            if isinstance(terminal, dict):
                tool_context.state["defected_echo_ids"] = terminal.get("defected", [])
                tool_context.state["aligned_echo_ids"] = terminal.get("aligned", [])

        return {
            "status": "success",
            "simulation_id": simulator_session_id,
            "message": f"Successfully sent scenario to simulator agent: {action}. IMPORTANT: You MUST now load the 'consultant' skill and call 'emit_sim_results_a2ui' with the evaluation scores and coaching insights, then call 'emit_action_plan_a2ui'. Do NOT STOP until both cards have been emitted.",
            "simulator_response": response,
        }
    except Exception as e:
        logger.error(f"LEADERSHIP_PLANNER: Failed to call simulator agent: {e}")
        return {"status": "error", "message": f"Failed to call simulator agent: {str(e)}"}


# [END submit_to_simulator]
