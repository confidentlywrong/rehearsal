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

"""Leadership planner-with-eval specific tools — simulator collaboration.

Mirrors agents/planner_with_eval/tools.py but submits QuestConfig-compatible
scenario data instead of marathon route GeoJSON.
"""

import asyncio
import json
import logging
import math
import os
import pathlib
from typing import Any
from uuid import uuid4

from google.adk.tools.tool_context import ToolContext

from agents.utils.sim_defaults import DEFAULT_RUNNER_COUNT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional route GeoJSON for 3D map visualization (Option C: Hybrid HUD).
# Loaded only when explicitly provided in session state, scenario dict, or
# DEFAULT_ROUTE_GEOJSON_PATH env var.
# ---------------------------------------------------------------------------


def _load_default_route_geojson(
    tool_context: ToolContext | None = None,
    scenario: dict | None = None,
) -> dict | None:
    """Load route GeoJSON from explicit state, scenario, or DEFAULT_ROUTE_GEOJSON_PATH.

    Returns the parsed GeoJSON dict, or None when unconfigured. Raises
    FileNotFoundError or ValueError if DEFAULT_ROUTE_GEOJSON_PATH is set
    but missing, unreadable, or invalid JSON.
    """
    if tool_context is not None:
        state = getattr(tool_context, "state", None)
        if state is not None and hasattr(state, "get"):
            state_route = state.get("route_geojson")
            if isinstance(state_route, dict) and state_route:
                return state_route

    if isinstance(scenario, dict):
        scenario_route = scenario.get("route_geojson")
        if isinstance(scenario_route, dict) and scenario_route:
            return scenario_route

    env_path = os.environ.get("DEFAULT_ROUTE_GEOJSON_PATH", "").strip()
    if not env_path:
        return None

    path = pathlib.Path(env_path)
    if not path.is_file():
        raise FileNotFoundError(f"DEFAULT_ROUTE_GEOJSON_PATH not found: {path}")
    try:
        route_geojson = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid GeoJSON at DEFAULT_ROUTE_GEOJSON_PATH ({path}): {exc}") from exc
    if not isinstance(route_geojson, dict):
        raise ValueError(f"DEFAULT_ROUTE_GEOJSON_PATH ({path}) must contain a JSON object")
    logger.info("Loaded route GeoJSON from DEFAULT_ROUTE_GEOJSON_PATH=%s", path)
    return route_geojson


_MAX_STAKEHOLDER_COUNT = 1_000_000


def _coerce_positive_int(raw: Any) -> int | None:
    """Coerce numeric / string counts (e.g. '1,000', 500.0) to bounded positive int."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if 0 < raw <= _MAX_STAKEHOLDER_COUNT else None
    if isinstance(raw, float):
        if not math.isfinite(raw) or raw <= 0 or raw > _MAX_STAKEHOLDER_COUNT:
            return None
        val = int(round(raw))
        return val if 0 < val <= _MAX_STAKEHOLDER_COUNT else None
    if isinstance(raw, str):
        cleaned = raw.strip().replace(",", "").replace("_", "")
        if not cleaned:
            return None
        try:
            fv = float(cleaned)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(fv) or fv <= 0 or fv > _MAX_STAKEHOLDER_COUNT:
            return None
        val = int(round(fv))
        return val if 0 < val <= _MAX_STAKEHOLDER_COUNT else None
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
    simulation_config = simulation_config.copy() if isinstance(simulation_config, dict) else {}
    # Only default runner_type if neither the caller nor config provided one.
    if not simulation_config.get("runner_type"):
        runner_type = runner_type or "leadership_runner_autopilot"
        simulation_config["runner_type"] = runner_type
    # Alias stakeholder_count to runner_count for simulator compatibility
    if "stakeholder_count" in simulation_config:
        raw_sc = simulation_config.pop("stakeholder_count")
        coerced_sc = _coerce_positive_int(raw_sc)
        if coerced_sc is not None:
            simulation_config["runner_count"] = coerced_sc
    if "runner_count" in simulation_config:
        coerced_rc = _coerce_positive_int(simulation_config.get("runner_count"))
        if coerced_rc is not None:
            simulation_config["runner_count"] = coerced_rc
        else:
            simulation_config.pop("runner_count", None)
    return simulation_config


def _ensure_runner_count_from_scenario(simulation_config: dict, scenario: dict | None) -> dict:
    """Defensive fallback: if LLM forgot to pass stakeholder_count, extract
    from the designed scenario in state.

    Prevents the "1 runner" bug where the LLM omits simulation_config entirely
    and downstream defaults shrink the simulation to a token size. The
    scenario's stakeholder_count is the source of truth (set by
    design_leadership_scenario; default 1000).
    """
    scenario_count = None
    if isinstance(scenario, dict):
        scenario_count = _coerce_positive_int(scenario.get("stakeholder_count"))
    current_rc = _coerce_positive_int(simulation_config.get("runner_count"))
    if current_rc is not None:
        simulation_config["runner_count"] = current_rc
    else:
        simulation_config.pop("runner_count", None)
    if (
        current_rc is None
        or (
            current_rc <= DEFAULT_RUNNER_COUNT
            and scenario_count is not None
            and scenario_count > DEFAULT_RUNNER_COUNT
        )
    ):
        if scenario_count is not None and scenario_count > 0:
            simulation_config["runner_count"] = scenario_count
            logger.info(
                "LEADERSHIP_PLANNER: simulation_config runner_count set from scenario.stakeholder_count=%d",
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
        # call_agent concatenates every status message of the A2A task, so
        # the executor's progress note precedes the final failure text.
        cleaned = text.strip()
        while cleaned.startswith(_SIMULATOR_PROGRESS_PREFIX):
            cleaned = cleaned[len(_SIMULATOR_PROGRESS_PREFIX) :].lstrip()
        if cleaned.startswith(_SIMULATOR_FAILURE_PREFIXES) or cleaned.endswith(_SIMULATOR_FAILURE_SUFFIX):
            return cleaned[:500]
    return None


async def _recover_scenario_from_memory(tool_context: ToolContext, state: Any) -> dict | None:
    """Hydrate missing session scenario state from stored scenario memory when an ID is available."""
    candidate_ids: list[str] = []
    active_sid = state.get("active_scenario_id")
    if isinstance(active_sid, str) and active_sid.strip():
        candidate_ids.append(active_sid.strip())
    selected_ids = state.get("selected_context_scenario_ids")
    if isinstance(selected_ids, list):
        for sid in selected_ids:
            if isinstance(sid, str) and sid.strip() and sid.strip() not in candidate_ids:
                candidate_ids.append(sid.strip())
    if not candidate_ids:
        return None
    try:
        from agents.leadership_planner_with_memory.memory import tools as mem_tools

        owner_id = mem_tools._resolve_owner_id(tool_context)
        for sid in candidate_ids:
            stored = await mem_tools._store.get_scenario(sid, owner_id=owner_id)
            if stored and isinstance(stored.scenario_data, dict):
                state["leadership_scenario"] = stored.scenario_data
                state["active_scenario_id"] = stored.scenario_id
                if stored.evaluation_score is not None or isinstance(stored.evaluation_result, dict):
                    merged_eval = {
                        **(stored.evaluation_result if isinstance(stored.evaluation_result, dict) else {}),
                        **({"overall_score": stored.evaluation_score} if stored.evaluation_score is not None else {}),
                    }
                    state["leadership_evaluation"] = merged_eval
                    state["evaluation_result"] = merged_eval
                logger.info("LEADERSHIP_PLANNER: Hydrated missing leadership_scenario from stored scenario %s", sid)
                return stored.scenario_data
    except Exception:
        logger.debug("LEADERSHIP_PLANNER: Scenario memory hydration failed", exc_info=True)
    return None


async def start_simulation(
    action: str = "execute",
    message: str = "Executing leadership simulation",
    tool_context: ToolContext = None,
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
        runner_type: Optional runner type (e.g. 'leadership_runner_autopilot',
            'leadership_runner', 'leadership_runner_gke',
            'leadership_runner_cloudrun'). Injected into simulation_config.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if not tool_context or state is None or not hasattr(state, "get") or not hasattr(state, "__setitem__"):
        return {"status": "error", "message": "No tool context available."}

    state["simulator_session_id"] = None
    scenario = state.get("leadership_scenario") or await _recover_scenario_from_memory(tool_context, state)
    if not scenario:
        return {
            "status": "error",
            "message": "No leadership scenario found in session state. Run 'design_leadership_scenario' first.",
        }

    # Re-entrancy guard: if a simulation was already executed in the SAME
    # invocation (LLM response), reject. The invocation_id changes when a
    # new user message arrives, so legitimate re-runs are allowed.
    invocation_id = getattr(tool_context, "invocation_id", None)
    executed_in = state.get("simulation_executed_invocation")
    if executed_in and invocation_id is not None and executed_in == invocation_id:
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
    state["_sim_token"] = simulator_session_id
    state["simulator_session_id"] = simulator_session_id

    # Set simulation_id in planner state so all subsequent messages carry it.
    state["simulation_id"] = simulator_session_id

    # Persist simulation_config so submit_plan_to_simulator can read it
    # from state when the LLM omits it (the "carry forward" contract).
    # Always create config dict so persona is injected even when the LLM
    if "persona" not in simulation_config:
        simulation_config["persona"] = state.get("selected_persona", "pragmatist")
    state["simulation_config"] = simulation_config
    state["expected_stakeholder_count"] = simulation_config.get(
        "runner_count", DEFAULT_RUNNER_COUNT
    )

    from agents.utils.gateway import register_simulation_with_gateway

    await register_simulation_with_gateway(simulator_session_id, tool_context=tool_context)

    logger.info(f"LEADERSHIP_PLANNER: Simulation initialized (simulation_id={simulator_session_id}, action={action})")

    return {
        "status": "ready",
        "simulation_id": simulator_session_id,
        "action": action,
        "message": f"Leadership simulation session initialized. Call submit_plan_to_simulator to {action}.",
    }


# [START submit_to_simulator]
async def submit_plan_to_simulator(
    action: str = "execute",
    message: str = "Executing leadership simulation",
    tool_context: ToolContext = None,
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
        runner_type: Optional runner type (e.g. 'leadership_runner_autopilot',
            'leadership_runner', 'leadership_runner_gke',
            'leadership_runner_cloudrun'). Injected into simulation_config.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if not tool_context or state is None or not hasattr(state, "get") or not hasattr(state, "__setitem__"):
        return {"status": "error", "message": "No tool context available."}

    from agents.utils.communication import call_agent, resolve_tool_context_user_id, strict_json_dumps

    caller_supplied_config = bool(simulation_config or runner_type)
    raw_caller_config = simulation_config.copy() if isinstance(simulation_config, dict) else None
    raw_caller_runner_type = runner_type

    logger.info(f"LEADERSHIP_PLANNER: Submitting scenario to simulator with action: {action}")

    scenario = state.get("leadership_scenario") or await _recover_scenario_from_memory(tool_context, state)
    if not scenario:
        return {
            "status": "error",
            "message": "No leadership scenario found in session state. Run 'design_leadership_scenario' first.",
        }

    # Re-entrancy guard: block duplicate execute calls within the SAME invocation.
    invocation_id = getattr(tool_context, "invocation_id", None)
    executed_in = state.get("simulation_executed_invocation")
    if action == "execute" and executed_in and invocation_id is not None and executed_in == invocation_id:
        logger.warning("submit_plan_to_simulator: simulation already executed in this invocation, rejecting duplicate")
        return {
            "status": "error",
            "message": "Simulation already complete. Do NOT call start_simulation or "
            "submit_plan_to_simulator again. Proceed to: "
            "emit_sim_results_a2ui, then emit_action_plan_a2ui.",
        }

    try:
        resolved_user_id = resolve_tool_context_user_id(tool_context)
    except ValueError as exc:
        logger.error("LEADERSHIP_PLANNER: Cannot submit simulation without valid user_id: %s", exc)
        return {"status": "error", "message": str(exc)}

    # Wait for start_simulation to write a FRESH simulator_session_id.
    # Both tools are dispatched in the same LLM response. Use _sim_token
    # to detect freshness.
    simulator_session_id = state.get("simulator_session_id")
    sim_token = state.get("_sim_token")

    if not simulator_session_id or simulator_session_id != sim_token:
        if action == "execute":
            for _ in range(20):  # 20 x 0.1s = 2s max
                await asyncio.sleep(0.1)
                simulator_session_id = state.get("simulator_session_id")
                sim_token = state.get("_sim_token")
                if simulator_session_id and simulator_session_id == sim_token:
                    break
    if not simulator_session_id:
        if action == "execute":
            # Re-check guard after async wait to prevent double-execution on rapid clicks
            executed_in = state.get("simulation_executed_invocation")
            if executed_in and invocation_id is not None and executed_in == invocation_id:
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
                simulation_config=raw_caller_config,
                runner_type=raw_caller_runner_type,
            )
            simulator_session_id = start_result.get("simulation_id") or str(uuid4())
            if start_result.get("status") == "error":
                return start_result
        else:
            simulator_session_id = str(uuid4())
            state["simulator_session_id"] = simulator_session_id

    # Honor the carry-forward contract from start_simulation: when the caller
    # omitted simulation_config and runner_type, inherit state["simulation_config"].
    stored_config = state.get("simulation_config")
    if not caller_supplied_config and isinstance(stored_config, dict) and stored_config:
        simulation_config = _normalize_sim_config(stored_config, None)
    else:
        simulation_config = _normalize_sim_config(raw_caller_config, raw_caller_runner_type)
        if isinstance(stored_config, dict):
            for key, val in stored_config.items():
                simulation_config.setdefault(key, val)
    if "persona" not in simulation_config:
        simulation_config["persona"] = state.get("selected_persona", "pragmatist")

    # Defensive: if LLM omitted stakeholder_count, fall back to the scenario's
    # designed value so the simulator doesn't shrink to a default of 1.
    simulation_config = _ensure_runner_count_from_scenario(simulation_config, scenario)

    # Set simulation_id for plugin emissions only during execute.
    if action == "execute" and not state.get("simulation_id"):
        state["simulation_id"] = simulator_session_id

    # Store large data in Redis side-channel, following the upstream pattern.
    # The quest_config (dynamic leadership scenario) is stored alongside any
    # explicitly provided route GeoJSON. This keeps the A2A payload small — only
    # action, narrative, and simulation_id — so lightweight router models can
    # reliably parse the action field and route to simulation_pipeline.
    from agents.utils.simdata import SimDataError, store_simulation_data

    try:
        route_geojson = _load_default_route_geojson(tool_context=tool_context, scenario=scenario)
        store_kwargs: dict[str, Any] = {
            "simulation_id": simulator_session_id,
            "route_geojson": route_geojson,
            "quest_config": scenario,
        }
        if resolved_user_id is not None:
            store_kwargs["user_id"] = resolved_user_id
        stored = await store_simulation_data(**store_kwargs)
    except (SimDataError, FileNotFoundError, ValueError) as exc:
        logger.error(
            "LEADERSHIP_PLANNER: Failed preparing/storing simulation data for %s: %s",
            simulator_session_id,
            exc,
            exc_info=True,
        )
        return {
            "status": "error",
            "simulation_id": simulator_session_id,
            "message": f"Failed to store simulation data: {exc}",
        }
    if not stored:
        logger.warning("submit_plan_to_simulator: failed to store data in Redis")

    # Build the payload — keep it small (upstream pattern).
    # Large data (route, quest_config) is in Redis. Only included in
    # the payload as fallback when Redis is unavailable.
    effective_config = simulation_config or state.get("simulation_config")
    payload: dict[str, object] = {
        "action": action,
        "narrative": message,
        "simulation_id": simulator_session_id,
    }
    if resolved_user_id is not None:
        payload["user_id"] = resolved_user_id
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
        call_kwargs: dict[str, Any] = {
            "tool_context": tool_context,
            "agent_name": "simulator",
            "message": strict_json_dumps(payload),
            "session_id": simulator_session_id,
        }
        if resolved_user_id is not None:
            call_kwargs["user_id"] = resolved_user_id
        response = await call_agent(**call_kwargs)

        # Load simulation telemetry from Redis side-channel. Post-simulation
        # tools (result cards, translation diagnostic, stakeholder
        # psychology) read echo_voice_log, resources, vitals_trend etc.
        sim_data: dict | None = None
        try:
            from agents.utils.simdata import load_simulation_data
            sim_data = await (
                load_simulation_data(simulator_session_id, user_id=resolved_user_id)
                if resolved_user_id is not None
                else load_simulation_data(simulator_session_id)
            )
        except SimDataError as exc:
            logger.error(
                "submit_plan_to_simulator: side-channel read failed for %s: %s",
                simulator_session_id,
                exc,
                exc_info=True,
            )
            if action == "execute":
                for sk, empty in _PER_RUN_STATE_DEFAULTS.items():
                    state[sk] = type(empty)()
            state["simulation_error"] = str(exc)
            state["simulation_result"] = None
            return {
                "status": "error",
                "simulation_id": simulator_session_id,
                "message": f"Failed to load simulation telemetry from side-channel: {exc}",
            }
        except Exception as exc:
            logger.warning(
                "submit_plan_to_simulator: failed to load side-channel data: %s",
                exc,
                exc_info=True,
            )

        if action == "execute":
            # Reset per-run telemetry so a field missing from this run's
            # side-channel (or a failed run) cannot carry over the previous
            # run's values into the result cards and diagnostics.
            for sk, empty in _PER_RUN_STATE_DEFAULTS.items():
                state[sk] = type(empty)()

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
            state["simulation_error"] = failure
            state["simulation_result"] = None
            return {
                "status": "error",
                "simulation_id": simulator_session_id,
                "message": (
                    f"The simulator failed and produced no simulation results: {failure}. "
                    "Tell the user the simulation failed, quote this error, and end your turn here."
                ),
            }

        # Persist the simulator response to session state
        state["simulation_result"] = response
        state["simulation_error"] = None

        # Mark execution complete so subsequent LLM loops are blocked.
        if action == "execute" and invocation_id is not None:
            state["simulation_executed_invocation"] = invocation_id

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
                    state[sk] = sim_data[sk]
            terminal = sim_data.get("terminal_echo_ids")
            if isinstance(terminal, dict):
                state["defected_echo_ids"] = terminal.get("defected", [])
                state["aligned_echo_ids"] = terminal.get("aligned", [])

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
