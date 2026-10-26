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

"""Post-race tools for compiling results and cleaning up.

These tools wrap up the simulation after the race phase completes:
1. compile_results — aggregate tick snapshots into final metrics
2. stop_race_collector — shut down the Redis subscription
3. call_agent — delegate inter-agent communication for optional reporting
"""

import logging

import redis.exceptions
from google.adk.tools.tool_context import ToolContext

from agents.simulator.collector import RaceCollector
from agents.utils.simdata import store_simulation_data
from agents.utils.sim_defaults import DEFAULT_RACE_DISTANCE_MI
from agents.simulator.longitudinal import (
    SimulationRunRecord,
    SimulationRunStore,
    compute_peer_benchmarks,
    compute_learning_trajectory,
    recommend_next_quest,
)

logger = logging.getLogger(__name__)


async def compile_results(tool_context: ToolContext) -> dict:
    """Aggregate all tick snapshots into final race metrics.

    Reads ``tick_snapshots`` from session state and produces:
    - vitals_trend: per-tick averages (velocity, water, distance)
    - final_status_counts: runner status distribution from the last tick
    - notable_events: collected notable events from all ticks
    - sampling_quality: ratio of ticks with reporting runners
    - avg_runners_reporting: mean runners per tick
    - finished_count / dnf_count: race completion statistics

    Args:
        tool_context: ADK tool context for session state access.

    Returns:
        dict with status and aggregated race results.
    """
    state = tool_context.state
    snapshots = state.get("tick_snapshots", [])
    total_ticks = len(snapshots)
    runner_count = state.get("runner_count", 0)
    finished_ids = set(state.get("finished_runner_ids", []))
    finished_count = len(finished_ids)

    # Prefer the untruncated vitals_trend_full accumulator (H3 fix) if
    # available.  Falls back to snapshot-based computation when it isn't.
    vitals_trend: list[dict] = state.get("vitals_trend_full", [])
    build_vitals_from_snapshots = not vitals_trend
    final_status_counts: dict[str, int] = {}
    persona_status_counts: dict[str, dict[str, int]] = {}
    notable_events: list[str] = []
    total_runners_reporting = 0
    ticks_with_runners = 0

    for snap in snapshots:
        # Per-tick vitals — only build from snapshots if the untruncated
        # accumulator is not available (backward compat / tests).
        if build_vitals_from_snapshots:
            vitals_trend.append(
                {
                    "tick": snap.get("tick", 0),
                    "real_time_minutes": snap.get("real_time_minutes", 0.0),
                    "avg_velocity": snap.get("avg_velocity", 0.0),
                    "avg_water": snap.get("avg_water", 0.0),
                    "avg_distance": snap.get("avg_distance", 0.0),
                }
            )

        # Final status counts — use the last tick's snapshot, not cumulative.
        # Each tick's status_counts already sums to the number of reporting
        # runners. Summing across ticks inflates the total (N runners x T ticks).
        final_status_counts = dict(snap.get("status_counts", {}))
        persona_status_counts = dict(snap.get("persona_status_counts", {}))

        # Collect notable events
        notable_events.extend(snap.get("notable_events", []))

        # Runner reporting stats
        runners = snap.get("runners_reporting", 0)
        total_runners_reporting += runners
        if runners > 0:
            ticks_with_runners += 1

    # Overwrite snapshot values with the global registry if available
    # (more robust than trusting the final snapshot which may be missing data)
    global_states = state.get("global_runner_states")
    persona_avg_progress: dict[str, float] = {}
    if global_states:
        final_status_counts = {}
        persona_status_counts = {}
        # Also compute average alignment progress per persona
        persona_distance_sums: dict[str, float] = {}
        persona_distance_counts: dict[str, int] = {}
        race_dist = (state.get("simulation_config") or {}).get("race_distance_mi", DEFAULT_RACE_DISTANCE_MI)

        for runner_id, info in global_states.items():
            r_status = info.get("status", "unknown")
            r_persona = info.get("persona", "unknown")
            r_distance = info.get("distance", 0.0)

            final_status_counts[r_status] = final_status_counts.get(r_status, 0) + 1
            if r_persona not in persona_status_counts:
                persona_status_counts[r_persona] = {}
            persona_status_counts[r_persona][r_status] = persona_status_counts[r_persona].get(r_status, 0) + 1

            persona_distance_sums[r_persona] = persona_distance_sums.get(r_persona, 0.0) + r_distance
            persona_distance_counts[r_persona] = persona_distance_counts.get(r_persona, 0) + 1

        for p_name, p_sum in persona_distance_sums.items():
            p_count = persona_distance_counts.get(p_name, 1)
            avg_dist = p_sum / p_count if p_count > 0 else 0.0
            persona_avg_progress[p_name] = round(100.0 * avg_dist / race_dist, 1) if race_dist > 0 else 0.0
    else:
        # Fallback: try last snapshot
        if snapshots:
            persona_avg_progress = dict(snapshots[-1].get("persona_avg_progress", {}))

    avg_runners_reporting = total_runners_reporting / total_ticks if total_ticks > 0 else 0
    sampling_quality = ticks_with_runners / total_ticks if total_ticks > 0 else 0
    dnf_count = max(0, runner_count - finished_count) if runner_count > 0 else 0

    if total_ticks == 0:
        logger.warning(
            "compile_results: no tick snapshots recorded — simulation may have "
            "failed to start or all runners crashed before the first tick"
        )
        return {
            "status": "error",
            "simulation_id": state.get("simulation_id"),
            "total_ticks": 0,
            "runner_count": runner_count,
            "finished_count": 0,
            "dnf_count": runner_count,
            "message": (
                "No simulation ticks were recorded. The simulation either "
                "failed to start or ended before any tick data was collected. "
                "Consider re-running the simulation."
            ),
        }

    logger.info(
        "compile_results: %d ticks, %d finished, %d DNF, avg %.1f runners/tick",
        total_ticks,
        finished_count,
        dnf_count,
        avg_runners_reporting,
    )

    result = {
        "status": "success",
        "simulation_id": state.get("simulation_id"),
        "total_ticks": total_ticks,
        "runner_count": runner_count,
        "finished_count": finished_count,
        "dnf_count": dnf_count,
        "vitals_trend": vitals_trend,
        "final_status_counts": final_status_counts,
        "persona_status_counts": persona_status_counts,
        "persona_avg_progress": persona_avg_progress,
        "notable_events": notable_events,
        "echo_voice_log": state.get("echo_voice_log", []),
        "avg_runners_reporting": avg_runners_reporting,
        "sampling_quality": round(sampling_quality, 2),
        "message": f"Aggregated {total_ticks} cycles: {finished_count} aligned, {dnf_count} disengaged",
        "resources": state.get("resources", {}),
        "decision_log": state.get("decision_log", []),
        "terminal_echo_ids": {
            "aligned": state.get("aligned_echo_ids", []),
            "defected": state.get("defected_echo_ids", []),
        },
    }

    if "principles" in state:
        result["contradiction_log"] = state.get("contradiction_log", [])
        result["alert_history"] = state.get("alert_history", [])
        result["triggered_black_swans"] = state.get("triggered_black_swans", [])

    # ------------------------------------------------------------------
    # C2 fix: Normalize raw principle_scores to 0-100 scale before
    # longitudinal tracking.  Raw scores are cumulative (+1 per applied
    # tick, -0.5 per neglected tick).  Normalize using max_ticks as the
    # ceiling so a principle applied every tick → 100.
    # ------------------------------------------------------------------
    raw_scores = state.get("principle_scores", [0.0] * 7)
    max_score_per_principle = max(total_ticks, 1)
    normalized_scores = [round(max(0.0, min(100.0, (s / max_score_per_principle) * 100.0)), 1) for s in raw_scores]
    state["principle_scores"] = normalized_scores

    # ------------------------------------------------------------------
    # Longitudinal tracking: save run, compute benchmarks, trajectory
    # ------------------------------------------------------------------
    try:
        # ADK State defines neither keys() nor __iter__, so dict(state) would
        # index state[0] and raise KeyError: 0. Use its to_dict() snapshot.
        state_snapshot = state.to_dict() if hasattr(state, "to_dict") else dict(state)
        run_record = SimulationRunRecord.from_session_state(state_snapshot)
        store = SimulationRunStore()
        store.save_run(run_record)

        # Compute peer benchmarks for this quest
        all_quest_runs = store.get_all_runs_for_quest(run_record.quest_id, run_record.participant_id)
        benchmarks = compute_peer_benchmarks(
            current_scores=run_record.principle_scores,
            all_runs=all_quest_runs,
        )
        result["peer_benchmarks"] = benchmarks

        # Compute participant's learning trajectory
        participant_runs = store.get_participant_runs(run_record.participant_id)
        trajectory = compute_learning_trajectory(participant_runs)
        result["learning_trajectory"] = trajectory

        # Recommend next quest
        result["next_quest"] = recommend_next_quest(participant_runs)

        logger.info(
            "Longitudinal: saved run %s, rank %d/%d (%.0f%% percentile)",
            run_record.run_id,
            benchmarks["rank"],
            benchmarks["rank_total"],
            benchmarks["percentile"],
        )
    except Exception as e:
        logger.warning("Longitudinal tracking failed (non-fatal): %s", e)

    # Persist key aggregates to session state so downstream agents
    # (e.g. emit_sim_results_a2ui) can access them reliably.
    tool_context.state["persona_status_counts"] = persona_status_counts
    tool_context.state["persona_avg_progress"] = persona_avg_progress
    tool_context.state["runner_count"] = runner_count

    # Also persist to Redis side-channel so persona data survives the
    # A2A text boundary when the planner reads simulation results.
    simulation_id = state.get("simulation_id")
    if simulation_id:
        await store_simulation_data(
            simulation_id=simulation_id,
            persona_status_counts=persona_status_counts,
            persona_avg_progress=persona_avg_progress,
            runner_count=runner_count,
            echo_voice_log=state.get("echo_voice_log"),
            decision_log=state.get("decision_log"),
            contradiction_log=state.get("contradiction_log"),
            route_friction_map=state.get("route_friction_map"),
            terminal_echo_ids={
                "aligned": state.get("aligned_echo_ids", []),
                "defected": state.get("defected_echo_ids", []),
            },
            vitals_trend=vitals_trend,
            alert_history=state.get("alert_history", []),
            resources=state.get("resources", {}),
            triggered_black_swans=state.get("triggered_black_swans", []),
        )

    return result


async def stop_race_collector(tool_context: ToolContext) -> dict:
    """Stop the RaceCollector for the current session.

    Looks up the collector by session_id and calls stop() to cancel the
    background Redis subscription task, unsubscribe, and clean up resources.

    Args:
        tool_context: ADK tool context for session state access.

    Returns:
        dict with status key.
    """
    session_id = tool_context.session.id
    collector = RaceCollector.get(session_id)

    if collector is not None:
        try:
            await collector.stop()
        except (redis.exceptions.ConnectionError, ConnectionError, OSError) as e:
            # The Redis connection may have been closed by the server due to
            # idle timeout after the race completed.  This is benign — the
            # race data is already collected; we're just cleaning up.
            logger.warning("stop_race_collector: Redis cleanup error (benign): %s", e)
        logger.info("stop_race_collector: stopped collector for session %s", session_id)
    else:
        logger.info("stop_race_collector: no collector found for session %s", session_id)

    # Publish end_simulation so dispatchers remove runner sessions.
    simulation_id = tool_context.state.get("simulation_id")
    if simulation_id:
        try:
            from agents.simulator.broadcast import publish_end_simulation

            await publish_end_simulation(simulation_id)
        except Exception as e:
            logger.warning("stop_race_collector: failed to publish end_simulation: %s", e)

    # Clear simulation flags to prevent the race engine from re-running
    # if the root LLM re-invokes simulation_pipeline after completion.
    tool_context.state["simulation_ready"] = False
    tool_context.state["simulation_in_progress"] = False

    return {
        "status": "success",
        "simulation_id": tool_context.state.get("simulation_id"),
        "session_id": session_id,
        "message": "Stakeholder telemetry stopped" if collector is not None else "No telemetry stream to stop",
    }


async def call_agent(agent_name: str, message: str, tool_context: ToolContext) -> dict:
    """Delegate inter-agent communication via the shared A2A utility.

    Args:
        agent_name: Name of the target agent (e.g., 'runner', 'planner').
        message: The instruction or query to send.
        tool_context: ADK tool context for A2A client access.

    Returns:
        dict with status and response from the target agent.
    """
    from agents.utils.communication import call_agent as _call_agent

    return await _call_agent(agent_name=agent_name, message=message, tool_context=tool_context)
