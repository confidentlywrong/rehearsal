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

"""Race-tick tools for per-tick simulation advancement.

These tools are called by the tick LoopAgent on each iteration:
1. advance_tick — broadcast, sleep, drain collector, aggregate, snapshot
2. check_race_complete — escalate when max ticks reached
"""

import asyncio
import copy
import logging
import math
import random
import time
from typing import Any

from google.adk.tools.tool_context import ToolContext

from agents.simulator.broadcast import publish_to_runners
from agents.utils import simulation_registry
from agents.utils import tenant_keys
from agents.utils.communication import resolve_tool_context_user_id
from agents.utils.pulses import emit_gateway_message
from agents.simulator.coaching import generate_micro_feedback, build_coaching_card
from agents.simulator.collector import RaceCollector, _buffer_key as _collector_buffer_key
from agents.simulator.decision_classifier import (
    classify_decision_heuristic,
    apply_resource_impacts,
    apply_black_swan_events,
)
from agents.simulator.ecsi import detect_contention
from agents.simulator.influence_network import InfluenceNetwork, build_default_influence_edges
from agents.simulator.live_dashboard import build_live_dashboard
from agents.simulator.truth_engine import detect_contradictions
from agents.ai_echo.process_decision_round import format_role_display
from agents.utils.runner_protocol import build_tick_event, serialize_runner_event
from agents.utils.sim_defaults import DEFAULT_MAX_TICKS, DEFAULT_RACE_DISTANCE_MI, DEFAULT_TICK_INTERVAL_SECONDS

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce numeric value to finite float, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _finite_int(value: Any, default: int = 0) -> int:
    """Coerce numeric value to finite int, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        try:
            num = float(value)
        except OverflowError:
            return default
        return value if math.isfinite(num) else default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return int(round(num)) if math.isfinite(num) else default

# Tool names whose tool_end payloads carry per-tick runner telemetry.
# ``process_tick`` is emitted by the marathon runner; ``process_leadership_tick``
# is emitted by the leadership_runner and returns isomorphic keys
# (velocity / water / distance_mi / runner_status / inner_thought), so it
# can flow through the same aggregation path.
RUNNER_TICK_TOOLS: frozenset[str] = frozenset({"process_tick", "process_leadership_tick"})

_ECHO_TOOL_NAMES = {"process_decision_round", "process_decision_round_tool"}


def _safe_sid(raw_sid: Any) -> str:
    """Coerce a session_id field into a hashable string cleanly."""
    if isinstance(raw_sid, str):
        return raw_sid
    return str(raw_sid) if raw_sid is not None else ""


def _tick_matches(msg_tick: Any, current_tick: int) -> bool:
    """Return True when msg_tick matches current_tick (or is omitted), rejecting bool/NaN/overflow."""
    if msg_tick is None:
        return True
    if isinstance(msg_tick, bool) or not isinstance(msg_tick, (int, float)):
        return False
    try:
        num = float(msg_tick)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(num) and num == current_tick


def _collect_reporting_runners(messages: list[dict], dest: set[str], *, current_tick: int = -1) -> None:
    """Extract unique runner session IDs with per-tick payloads into *dest*.

    Matches both ``process_tick`` (marathon) and ``process_leadership_tick``
    (leadership) payloads. When *current_tick* >= 0, messages whose
    ``result.tick`` doesn't match are silently skipped (stale-message filtering).
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        payload = msg.get("payload", {})
        if isinstance(payload, dict) and payload.get("tool_name") in RUNNER_TICK_TOOLS:
            if current_tick >= 0:
                result = payload.get("result", {})
                if isinstance(result, dict):
                    if not _tick_matches(result.get("tick"), current_tick):
                        continue
            sid = _safe_sid(msg.get("session_id"))
            if sid:
                dest.add(sid)


def _extract_decision_text(messages: list[dict]) -> str:
    """Build a decision-text snippet from stakeholder ``inner_thought`` fields.

    Drains the previous tick's runner messages and joins the ``inner_thought``
    of each unique session that reported a ``process_leadership_tick`` result.
    The classifier and contradiction detector use this string to evaluate
    the leader's most recent decision against stated principles; an empty
    string is returned when no leadership-tick payloads are present.
    """
    thoughts: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("tool_name") != "process_leadership_tick":
            continue
        result = payload.get("result", {})
        if not isinstance(result, dict):
            continue
        sid = _safe_sid(msg.get("session_id"))
        if sid in seen:
            continue
        thought = result.get("inner_thought")
        if isinstance(thought, str) and thought.strip():
            thoughts.append(thought.strip())
            if sid:
                seen.add(sid)
    return " | ".join(thoughts)


def _sample_friction_thoughts(
    messages: list[dict],
    *,
    current_tick: int = -1,
    max_samples: int = 3,
) -> list[str]:
    """Sample 2-3 stakeholder inner thoughts biased toward friction/resistance.

    Collects all inner_thought strings from this tick's leadership runner
    payloads, scores each by a friction heuristic (low capital, stalled, or
    collapsed → higher score), sorts descending, and returns the top
    *max_samples* as human-readable strings.

    Returns an empty list when no leadership-tick payloads are present
    (e.g. marathon runs or ticks with no runner responses).
    """
    candidates: list[tuple[float, str]] = []  # (friction_score, display_string)
    seen: set[str] = set()
    rng = random.Random(current_tick)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("tool_name") != "process_leadership_tick":
            continue
        result = payload.get("result", {})
        if not isinstance(result, dict):
            continue

        # Skip stale ticks
        if current_tick >= 0 and not _tick_matches(result.get("tick"), current_tick):
            continue

        sid = _safe_sid(msg.get("session_id"))
        if sid in seen:
            continue
        seen.add(sid)

        thought = result.get("inner_thought")
        if not isinstance(thought, str) or not thought.strip():
            continue

        # --- Friction score: higher = more interesting to surface ---
        score = 0.0
        capital = _finite_float(result.get("water"), 100.0)
        # Low capital → high friction (0-100 scale, inverted)
        score += (100.0 - capital) / 100.0 * 3.0  # up to 3.0
        # Stalled stakeholders get a boost
        if result.get("exhausted"):
            score += 2.0
        # Collapsed/deadlocked stakeholders get maximum friction
        if result.get("collapsed"):
            score += 4.0
        # Low momentum → some friction
        raw_vel = result.get("effective_velocity")
        if raw_vel is None:
            raw_vel = result.get("velocity")
        momentum = _finite_float(raw_vel, 0.0)
        if momentum < 0.3:
            score += 1.0
        # Small random jitter so we don't always show the exact same runners
        score += rng.random() * 0.5

        persona = result.get("persona") or "Stakeholder"
        capital_pct = round(capital)
        display = f'\U0001f4ad {persona} (capital: {capital_pct}%): "{thought.strip()}"'
        candidates.append((score, display))

    if not candidates:
        return []

    # Sort by friction score descending, take top N
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [display for _, display in candidates[:max_samples]]


async def _resolve_emit_target(tool_context: ToolContext | None) -> tuple[str, str | None, str | None]:
    """Resolve (emit_session_id, simulation_id, user_id) for tick gateway emission.

    Inside AgentTool(simulation_pipeline), ``tool_context.session.id`` is an
    ephemeral ADK child session ID while ``state["simulation_id"]`` carries the
    root session/context ID (or Vertex session ID). On Agent Engine,
    ``simulation_registry.get_context_id(...)`` maps Vertex session IDs back to
    the frontend's spawn UUID.
    """
    raw_session = getattr(tool_context, "session", None) if tool_context is not None else None
    raw_sid_val = getattr(raw_session, "id", None) if raw_session is not None else None
    raw_sid = raw_sid_val.strip() if isinstance(raw_sid_val, str) and raw_sid_val.strip() else None

    state_obj = getattr(tool_context, "state", None) if tool_context is not None else None
    raw_sim_val = state_obj.get("simulation_id") if state_obj is not None and hasattr(state_obj, "get") else None
    if isinstance(raw_sim_val, str):
        cleaned_sim = raw_sim_val.strip()
        sim_id = cleaned_sim if cleaned_sim and cleaned_sim != "\u2014" else None
    else:
        sim_id = None

    try:
        uid = resolve_tool_context_user_id(tool_context, require_when_saas=False)
    except ValueError as exc:
        logger.warning("advance_tick: invalid user_id context for gateway emit: %s", exc)
        uid = None

    async def _safe_get_context_id(sid: str) -> str | None:
        try:
            if uid is not None:
                return await simulation_registry.get_context_id(sid, user_id=uid)
            return await simulation_registry.get_context_id(sid)
        except ValueError as exc:
            logger.debug("advance_tick: skipped context_id lookup for %s (%s)", sid, exc)
            return None

    if raw_sid and raw_sid not in ("default", "system"):
        mapped = await _safe_get_context_id(raw_sid)
        if mapped:
            return mapped, sim_id, uid

    if sim_id and sim_id != raw_sid:
        mapped_sim = await _safe_get_context_id(sim_id)
        if mapped_sim:
            return mapped_sim, sim_id, uid

    if sim_id and (
        not (tenant_keys._saas_mode_enabled() and uid is None)
        or not raw_sid
        or raw_sid in ("default", "system")
    ):
        return sim_id, sim_id, uid

    return (raw_sid or "default"), sim_id, uid


async def advance_tick(tool_context: ToolContext) -> dict:
    """Advance the simulation by one tick.

    Reads current_tick, max_ticks, and config from session state. Publishes a
    broadcast to the ``simulation:broadcast`` Redis channel, sleeps for the
    tick interval, drains the RaceCollector buffer, aggregates runner
    telemetry, appends a snapshot to state, and emits a narrative pulse.

    Args:
        tool_context: ADK tool context for session state access.

    Returns:
        dict with aggregated tick statistics.
    """
    state = tool_context.state
    current_tick = _finite_int(state.get("current_tick"), 0)
    max_ticks = _finite_int(state.get("max_ticks"), DEFAULT_MAX_TICKS)
    raw_config = state.get("simulation_config")
    config = raw_config if isinstance(raw_config, dict) else {}
    tick_interval = _finite_float(config.get("tick_interval_seconds"), DEFAULT_TICK_INTERVAL_SECONDS)
    total_race_hours = _finite_float(config.get("total_race_hours"), 6.0)
    if total_race_hours <= 0:
        total_race_hours = 6.0
    session_id = tool_context.session.id

    simulation_id = state.get("simulation_id")

    # Compute simulated race time for this tick.  Tick 0 is the init tick
    # (minutes_per_tick=0, elapsed=0).  Movement ticks (1+) use
    # elapsed = tick * minutes_per_tick, matching build_tick_event().
    raw_minutes_per_tick = (total_race_hours * 60) / max_ticks if max_ticks > 0 else 0.0
    minutes_per_tick = raw_minutes_per_tick if math.isfinite(raw_minutes_per_tick) else 0.0
    raw_real_time = current_tick * minutes_per_tick
    real_time_minutes = raw_real_time if math.isfinite(raw_real_time) else 0.0

    # ------------------------------------------------------------------
    # Flush stale messages before broadcasting — but rescue finish data
    # ------------------------------------------------------------------
    # The RaceCollector may contain stale results from previous ticks.
    # Drain before broadcasting this tick to prevent contamination.
    # However, check stale messages for runner_status=="finished" so
    # late-arriving finish results are not permanently lost (fixes the
    # DNF mismatch where runners finishing near the end of a tick window
    # are missed).
    collector = RaceCollector.get(session_id)
    stale_messages: list[dict] = []
    if collector is not None:
        stale_messages = await collector.drain()
        # Rescue any finished runners from stale data
        raw_existing_finished = state.get("finished_runner_ids")
        existing_finished = (
            {x for x in raw_existing_finished if isinstance(x, str)}
            if isinstance(raw_existing_finished, (list, set, tuple))
            else set()
        )
        for msg in stale_messages:
            if not isinstance(msg, dict):
                continue
            payload = msg.get("payload", {})
            if isinstance(payload, dict):
                result = payload.get("result", payload)
                if isinstance(result, dict) and result.get("runner_status") == "finished":
                    sid = _safe_sid(msg.get("session_id"))
                    if sid:
                        existing_finished.add(sid)
        state["finished_runner_ids"] = list(existing_finished)

    # ------------------------------------------------------------------
    # Pre-Broadcast: Decision Classification & Black Swans
    # ------------------------------------------------------------------
    # The decision text for tick N's classification is derived from what
    # stakeholders communicated during tick N-1, captured here from the
    # stale messages we just drained.  We join each reporting runner's
    # ``inner_thought`` so the heuristic classifier and contradiction
    # detector operate on actual stakeholder signal instead of an empty
    # string.  Falls back to "" when no leadership-tick payloads are
    # present (e.g. marathon runs or the very first tick).
    decision_text = _extract_decision_text(stale_messages)

    principles = state.get("principles")
    # Run deterministic keyword-matching classification
    decision_tags = classify_decision_heuristic(decision_text, principles)

    # Evaluate Contradictions (Truth Engine)
    commitments = state.get("declared_commitments") or []
    contradictions = detect_contradictions(
        commitments,
        decision_tags,
        current_tick=current_tick,
        principles=principles or [],
    )
    if contradictions:
        raw_clog = state.get("contradiction_log")
        clog = list(raw_clog) if isinstance(raw_clog, list) else []
        clog.extend([c.to_dict() for c in contradictions])
        state["contradiction_log"] = clog
    contradiction_alert = contradictions[-1].to_dict() if contradictions else None

    # Accumulate decision log for post-simulation analysis tools
    raw_dlog = state.get("decision_log")
    dlog = list(raw_dlog) if isinstance(raw_dlog, list) else []
    dlog.append({
        "tick": current_tick,
        "decision_text": decision_text,
        "decision_tags": decision_tags,
    })
    state["decision_log"] = dlog

    # Apply resource impacts from decision
    raw_resource_state = state.get("resources")
    resource_state = copy.deepcopy(raw_resource_state) if isinstance(raw_resource_state, dict) else {}
    # Snapshot pre-impact resource values so the coaching layer can show
    # tick-over-tick deltas in its causal-context line. We use the last
    # tick's stored snapshot rather than the current pre-impact view so
    # the delta reflects all changes since the participant last saw a
    # coaching card.
    previous_resource_state = state.get("last_resource_snapshot")
    apply_resource_impacts(resource_state, decision_tags.get("resource_impact_suggestion", {}))

    # Evaluate Black Swan events
    black_swans = state.get("black_swan_events") or []
    raw_triggered = state.get("triggered_black_swans")
    triggered_history = list(raw_triggered) if isinstance(raw_triggered, list) else []
    apply_black_swan_events(current_tick, resource_state, black_swans, triggered_history)
    state["triggered_black_swans"] = triggered_history
    state["resources"] = resource_state


    # ------------------------------------------------------------------
    # C2 fix: Accumulate principle_scores from decision classification.
    # Without this, principle_scores stays at [0.0]*7, making the
    # longitudinal analysis (peer benchmarks, trajectories) non-functional.
    # ------------------------------------------------------------------
    principle_list = principles or []
    if principle_list and decision_text:
        raw_scores_val = state.get("principle_scores")
        raw_scores = [_finite_float(s, 0.0) for s in raw_scores_val] if isinstance(raw_scores_val, list) else [0.0] * 7
        applied = set(decision_tags.get("principles_applied", []))
        neglected = set(decision_tags.get("principles_neglected", []))
        for idx, p_name in enumerate(principle_list):
            if idx < len(raw_scores):
                if p_name in applied:
                    raw_scores[idx] += 1.0
                elif p_name in neglected:
                    raw_scores[idx] -= 0.5
        state["principle_scores"] = raw_scores

    # ------------------------------------------------------------------
    # Publish TICK event to simulation:broadcast Redis channel
    # (Uses upstream build_tick_event for framework compatibility.
    #  Leadership crucible decision data is injected as extra fields.)
    # ------------------------------------------------------------------
    # Bug-fix (2026-06-25): use the tenant-aware buffer-key helper so the
    # key shipped to runners matches the key the collector drains from.
    # The previous hardcoded literal silently dropped all runner direct-write
    # telemetry whenever SAAS_MODE=on (the literal key diverged from the
    # tenant-prefixed drain key).
    race_dist_cfg = _finite_float(config.get("race_distance_mi"), DEFAULT_RACE_DISTANCE_MI)
    if race_dist_cfg <= 0:
        race_dist_cfg = DEFAULT_RACE_DISTANCE_MI
    runner_session_ids_cfg = state.get("runner_session_ids") or []
    tick_event = build_tick_event(
        tick=current_tick,
        max_ticks=max_ticks,
        total_race_hours=total_race_hours,
        race_distance_mi=race_dist_cfg,
        collector_buffer_key=_collector_buffer_key(session_id),
        runner_count=len(runner_session_ids_cfg) if isinstance(runner_session_ids_cfg, list) else 0,
        # NOTE: persona intentionally omitted from broadcast so each runner
        # self-assigns via seeded random in initialize_leadership_stakeholder().
        # Previously broadcasting a single persona caused ALL runners to become
        # "pragmatist", leaving Visionary/Diplomat/Disruptor with no data.
    )
    # Inject leadership crucible decision data into the tick event
    tick_event.data["decision_text"] = decision_text
    tick_event.data["decision_tags"] = decision_tags
    tick_event.data["resource_state"] = resource_state
    tick_event.data["contradiction_alert"] = contradiction_alert
    raw_inf_net = state.get("influence_network")
    tick_event.data["influence_network"] = raw_inf_net if raw_inf_net is not None else []
    raw_inf_deltas = state.get("influence_deltas")
    tick_event.data["influence_deltas"] = raw_inf_deltas if raw_inf_deltas is not None else {}
    raw_rfm = state.get("route_friction_map")
    tick_event.data["route_friction_map"] = raw_rfm if raw_rfm is not None else {}
    raw_station_idx = state.get("station_index")
    tick_event.data["station_index"] = raw_station_idx if raw_station_idx is not None else {}
    raw_weather = state.get("weather_modifier")
    tick_event.data["weather_modifier"] = raw_weather if raw_weather is not None else {}

    # ------------------------------------------------------------------
    # Phase 3 → Phase 4 passthrough: per-Echo memory citations.
    # When the planner sourced personas from the analysis graph,
    # each echo carries `cited_node_ids` (and optionally
    # `source_doc_ids`). The simulator does not interpret them — it
    # passes them through on every tick so Phase 5 reflection (and the
    # SPA tick feed) can attribute utterances back to the memory nodes
    # the persona was built from. Empty dict for template-only runs.
    # See concepts/organizational_memory_and_learning.md §4 Phase 4.
    # ------------------------------------------------------------------
    echo_citations: dict[str, dict[str, list[str]]] = {}
    for echo in state.get("echo_configs", []) or []:
        if not isinstance(echo, dict):
            continue
        role = echo.get("role")
        if not role:
            continue
        cited = echo.get("cited_node_ids") or []
        src_docs = echo.get("source_doc_ids") or []
        if cited or src_docs:
            echo_citations[role] = {
                "cited_node_ids": list(cited),
                "source_doc_ids": list(src_docs),
            }
    tick_event.data["echo_citations"] = echo_citations

    # Try to get dependency graph and route map from quest config
    quest_config = state.get("quest_config", {})
    if isinstance(quest_config, dict):
        tick_event.data["gatekeeper_route_map"] = quest_config.get("gatekeeper_route_map", {})
        tick_event.data["dependency_graph"] = quest_config.get("influence_edges", [])
    else:
        # Pydantic model
        tick_event.data["gatekeeper_route_map"] = getattr(quest_config, "gatekeeper_route_map", {})
        tick_event.data["dependency_graph"] = getattr(quest_config, "influence_edges", [])

    # Compute exclude set from runners that finished/collapsed (upstream)
    # AND echoes that defected/aligned (leadership extension)
    # Don't exclude collapsed runners — they need tick events to potentially
    # call executive_intervention for recovery.
    raw_expected = state.get("runner_session_ids")
    expected_runner_ids = [x for x in raw_expected if isinstance(x, str)] if isinstance(raw_expected, list) else []
    raw_finished_excl = state.get("finished_runner_ids")
    exclude_set = (
        {x for x in raw_finished_excl if isinstance(x, str)}
        if isinstance(raw_finished_excl, (list, set, tuple))
        else set()
    )
    active_runner_ids = [sid for sid in expected_runner_ids if sid not in exclude_set]
    expected_count = len(active_runner_ids)
    exclude_ids = list(exclude_set) if exclude_set else None

    await publish_to_runners(
        serialize_runner_event(tick_event),
        simulation_id=simulation_id,
        exclude_runner_ids=exclude_ids,
    )
    poll_interval = max(0.01, _finite_float(config.get("poll_interval"), 0.2))
    max_collection = _finite_float(config.get("max_collection_seconds"), tick_interval)
    drain_interval = 0.2  # drain every 200ms during sleep window

    messages: list[dict] = []
    reported_runners: set[str] = set()

    if collector is None:
        logger.warning("advance_tick: no RaceCollector for session %s", session_id)
        await asyncio.sleep(max(0.0, tick_interval))
    elif tick_interval <= 0:
        # Zero-interval mode (tests / instant ticks): single drain, no sleep
        messages = await collector.drain()
        _collect_reporting_runners(messages, reported_runners, current_tick=current_tick)
    else:
        # Early-wake drain: collect results during the sleep window
        t_sleep_start = time.perf_counter()
        while time.perf_counter() - t_sleep_start < tick_interval:
            remaining = tick_interval - (time.perf_counter() - t_sleep_start)
            await asyncio.sleep(min(drain_interval, remaining))
            new_msgs = await collector.drain()
            if new_msgs:
                messages.extend(new_msgs)
                _collect_reporting_runners(new_msgs, reported_runners, current_tick=current_tick)
                if len(reported_runners) >= expected_count:
                    # All runners reported -- wait for remaining tick_interval floor
                    remaining = tick_interval - (time.perf_counter() - t_sleep_start)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    logger.info(
                        "PERF_TRACE: all %d runners reported at %.1fs (tick_interval=%.1f)",
                        expected_count,
                        time.perf_counter() - t_sleep_start,
                        tick_interval,
                    )
                    break

        # Post-sleep poll only if not all runners reported
        if len(reported_runners) < expected_count:
            poll_start = time.perf_counter()
            while len(reported_runners) < expected_count and (time.perf_counter() - poll_start) < max_collection:
                await asyncio.sleep(poll_interval)
                new_msgs = await collector.drain()
                if new_msgs:
                    messages.extend(new_msgs)
                    _collect_reporting_runners(new_msgs, reported_runners, current_tick=current_tick)

    if len(reported_runners) < expected_count:
        missing = set(active_runner_ids) - reported_runners
        logger.warning(
            "advance_tick: timeout waiting for runners; got %d/%d, missing: %s",
            len(reported_runners),
            expected_count,
            missing,
        )

    # ------------------------------------------------------------------
    # Aggregate runner telemetry from process_tick tool_end events
    # (Original upstream aggregation — needed for the return dict contract)
    # ------------------------------------------------------------------
    total_velocity = 0.0
    total_water = 0.0
    total_distance = 0.0
    total_comfort = 0.0
    comfort_count = 0  # Only count runners with explicit comfort data
    total_medical_saves = 0
    medical_events_this_tick: list[str] = []
    # Ensure _prev_medical_saves is properly initialized (not lazily)
    if "_prev_medical_saves" not in state:
        state["_prev_medical_saves"] = {}
    notable_events: list[str] = []
    finished_ids: list[str] = []
    collapsed_ids: list[str] = []
    recovered_ids: list[str] = []
    seen_runners: set[str] = set()

    if "global_runner_states" not in state:
        state["global_runner_states"] = {}

    raw_prev_saves = state.get("_prev_medical_saves")
    prev_medical_saves: dict[str, Any] = dict(raw_prev_saves) if isinstance(raw_prev_saves, dict) else {}
    raw_global_states = state.get("global_runner_states")
    global_runner_states: dict[str, dict] = dict(raw_global_states) if isinstance(raw_global_states, dict) else {}
    raw_collapsed_list = state.get("collapsed_runner_ids") or []
    collapsed_lookup = set(raw_collapsed_list) if isinstance(raw_collapsed_list, (list, set, tuple)) else set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("tool_name") not in RUNNER_TICK_TOOLS:
            continue
        result = payload.get("result", {})
        if not isinstance(result, dict):
            continue

        if not _tick_matches(result.get("tick"), current_tick):
            continue

        sid = _safe_sid(msg.get("session_id"))
        if sid in seen_runners:
            continue
        seen_runners.add(sid)

        eff_vel = _finite_float(result.get("effective_velocity"), 0.0)
        base_vel = _finite_float(result.get("velocity"), 0.0)
        total_velocity += eff_vel if eff_vel > 0 else base_vel
        total_water += _finite_float(result.get("water"), 0.0)
        total_distance += _finite_float(result.get("distance_mi"), 0.0)

        # Aggregate infrastructure impact metrics from per-runner health data
        health = result.get("health", {})
        if isinstance(health, dict) and "comfort" in health:
            comfort_val = _finite_float(health.get("comfort"), float("nan"))
            if math.isfinite(comfort_val):
                total_comfort += comfort_val
                comfort_count += 1
            saves = max(0, _finite_int(health.get("medical_saves"), 0))
            prev_saves = max(0, _finite_int(prev_medical_saves.get(sid), 0))
            new_saves = saves - prev_saves
            if new_saves > 0:
                total_medical_saves += new_saves
                persona_name = result.get("persona") or "stakeholder"
                medical_events_this_tick.append(f"{persona_name} recovered from burnout (critical intervention)")
            # Track cumulative saves per runner for delta calculation
            prev_medical_saves[sid] = saves

        runner_status = str(result.get("runner_status") or "unknown")
        persona = str(result.get("persona") or "unknown")
        raw_dist = result.get("distance_mi")
        if raw_dist is None:
            raw_dist = result.get("distance")
        distance = _finite_float(raw_dist, 0.0)

        # Update the global registry with the latest status and distance
        if sid:
            global_runner_states[sid] = {
                "status": runner_status,
                "persona": persona,
                "distance": distance,
            }

        if runner_status == "finished" and sid:
            finished_ids.append(sid)
        if runner_status == "collapsed" and sid:
            collapsed_ids.append(sid)

        # Check if a previously collapsed runner has recovered (executive intervention)
        if sid and runner_status != "collapsed" and sid in collapsed_lookup:
            recovered_ids.append(sid)

        notable = result.get("notable_event")
        if isinstance(notable, str) and notable:
            notable_events.append(notable)

    state["_prev_medical_saves"] = prev_medical_saves
    state["global_runner_states"] = global_runner_states

    runners_reporting = len(seen_runners)
    avg_velocity = _finite_float(total_velocity / runners_reporting, 0.0) if runners_reporting > 0 else 0.0
    avg_water = _finite_float(total_water / runners_reporting, 0.0) if runners_reporting > 0 else 0.0
    avg_distance = _finite_float(total_distance / runners_reporting, 0.0) if runners_reporting > 0 else 0.0

    # Recompute status_counts and persona_status_counts cumulatively
    status_counts: dict[str, int] = {}
    persona_status_counts: dict[str, dict[str, int]] = {}

    for sid, info in global_runner_states.items():
        if not isinstance(info, dict):
            continue
        r_status = str(info.get("status") or "unknown")
        r_persona = str(info.get("persona") or "unknown")

        status_counts[r_status] = status_counts.get(r_status, 0) + 1

        if r_persona not in persona_status_counts:
            persona_status_counts[r_persona] = {}
        persona_status_counts[r_persona][r_status] = persona_status_counts[r_persona].get(r_status, 0) + 1

    # Compute average alignment progress per persona (as % of race distance).
    # This gives a meaningful metric even when few/no runners fully "finish."
    race_dist = _finite_float(config.get("race_distance_mi"), DEFAULT_RACE_DISTANCE_MI)
    if race_dist <= 0:
        race_dist = DEFAULT_RACE_DISTANCE_MI
    persona_distance_sums: dict[str, float] = {}
    persona_distance_counts: dict[str, int] = {}
    for sid, info in global_runner_states.items():
        if not isinstance(info, dict):
            continue
        r_persona = str(info.get("persona") or "unknown")
        r_distance = _finite_float(info.get("distance"), 0.0)
        persona_distance_sums[r_persona] = persona_distance_sums.get(r_persona, 0.0) + r_distance
        persona_distance_counts[r_persona] = persona_distance_counts.get(r_persona, 0) + 1

    persona_avg_progress: dict[str, float] = {}
    for p_name, p_sum in persona_distance_sums.items():
        p_count = persona_distance_counts.get(p_name, 1)
        avg_dist = _finite_float(p_sum / p_count, 0.0) if p_count > 0 else 0.0
        raw_progress = (100.0 * avg_dist / race_dist) if race_dist > 0 else 0.0
        persona_avg_progress[p_name] = round(raw_progress, 1) if math.isfinite(raw_progress) else 0.0

    # Update finished runner tracking in state (O(1) dedup via set)
    raw_finished = state.get("finished_runner_ids")
    existing_finished = (
        set(raw_finished) if isinstance(raw_finished, (list, tuple, set)) else set()
    )
    existing_finished.update(finished_ids)
    state["finished_runner_ids"] = list(existing_finished)

    # Update collapsed runner tracking in state (O(1) dedup via set)
    raw_collapsed = state.get("collapsed_runner_ids")
    existing_collapsed = (
        set(raw_collapsed) if isinstance(raw_collapsed, (list, tuple, set)) else set()
    )
    existing_collapsed.update(collapsed_ids)
    for rid in recovered_ids:
        existing_collapsed.discard(rid)
    state["collapsed_runner_ids"] = list(existing_collapsed)

    # ------------------------------------------------------------------
    # Aggregate Echo telemetry from process_decision_round tool_end events
    # (Leadership Crucible extension — coexists with runner aggregation)
    # ------------------------------------------------------------------
    aligned_ids: list[str] = []
    defected_ids: list[str] = []
    seen_echoes: set[str] = set()
    echo_stance_counts: dict[str, int] = {}  # Separate from runner status_counts
    echo_sentiments_snapshot: dict[str, dict] = {}
    echo_actions_snapshot: list[dict] = []
    current_echo_sentiments_by_role: dict[str, float] = {}
    echo_notable_events: set[str] = set()
    echo_states_raw = state.get("_echo_states")
    echo_states = dict(echo_states_raw) if isinstance(echo_states_raw, dict) else {}
    route_friction_raw = state.get("route_friction_map")
    route_friction_map = dict(route_friction_raw) if isinstance(route_friction_raw, dict) else {}
    raw_voice_log = state.get("echo_voice_log")
    echo_voice_log = list(raw_voice_log) if isinstance(raw_voice_log, list) else []
    echo_voice_log_updated = False

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("tool_name") not in _ECHO_TOOL_NAMES:
            continue
        result = payload.get("result", {})
        if not isinstance(result, dict):
            continue

        if not _tick_matches(result.get("tick"), current_tick):
            continue

        sid = _safe_sid(msg.get("session_id"))
        if sid in seen_echoes:
            continue
        seen_echoes.add(sid)

        raw_stance = result.get("stance")
        stance = raw_stance if isinstance(raw_stance, str) and raw_stance else "neutral"
        echo_stance_counts[stance] = echo_stance_counts.get(stance, 0) + 1

        raw_role = result.get("role")
        has_valid_role = isinstance(raw_role, str) and bool(raw_role)
        role = raw_role if has_valid_role else "Echo"

        if result.get("aligned") and sid:
            aligned_ids.append(role if has_valid_role else sid)
        if result.get("defected") and sid:
            defected_ids.append(role if has_valid_role else sid)

        raw_role_disp = result.get("role_display")
        role_disp = (
            raw_role_disp
            if isinstance(raw_role_disp, str) and raw_role_disp
            else format_role_display(role)
        )
        sentiment_val = result.get("sentiment")
        sentiment_num = (
            _finite_float(sentiment_val, 0.0)
            if isinstance(sentiment_val, (int, float)) and not isinstance(sentiment_val, bool)
            else 0.0
        )
        resistance_val = result.get("resistance")
        resistance_num = (
            _finite_float(resistance_val, 0.3)
            if isinstance(resistance_val, (int, float)) and not isinstance(resistance_val, bool)
            else 0.3
        )

        echo_sentiments_snapshot[sid or role] = {
            "role": role,
            "stance": stance,
            "sentiment": sentiment_num,
        }
        current_echo_sentiments_by_role[role] = sentiment_num
        echo_states[role] = {
            "stance": stance,
            "sentiment": sentiment_num,
            "resistance": resistance_num,
        }

        thought = result.get("inner_thought")
        if thought:
            thought_evt = f"{role_disp}: {thought}"
            notable_events.append(thought_evt)
            echo_notable_events.add(thought_evt)

        notable_evt = result.get("notable_event")
        if notable_evt and isinstance(notable_evt, str) and notable_evt.strip():
            clean_evt = notable_evt.strip()
            notable_events.append(clean_evt)
            echo_notable_events.add(clean_evt)

        auto_act = result.get("autonomous_action")
        resource_claims = result.get("resource_claims") or []
        if auto_act and isinstance(auto_act, str) and auto_act.strip():
            clean_act = auto_act.strip()
            notable_events.append(clean_act)
            echo_notable_events.add(clean_act)
        if (auto_act and isinstance(auto_act, str) and auto_act.strip()) or resource_claims:
            echo_actions_snapshot.append(
                {
                    "role": role,
                    "action": auto_act.strip() if isinstance(auto_act, str) else "",
                    "resource_claims": [str(c) for c in resource_claims if c is not None]
                    if isinstance(resource_claims, list)
                    else [],
                }
            )

        # Persist echo voices in a dedicated log (not subject to
        # tick_snapshots truncation) so compile_results can surface
        # the full stakeholder narrative in the debrief.
        echo_voice_log.append(
            {
                "tick": current_tick,
                "role": role,
                "role_display": role_disp,
                "stance": stance,
                "sentiment": sentiment_num,
                "inner_thought": thought if isinstance(thought, str) else "",
                "notable_event": notable_evt if isinstance(notable_evt, str) else "",
                "autonomous_action": auto_act if isinstance(auto_act, str) else "",
                "resistance": resistance_num,
            }
        )
        echo_voice_log_updated = True

        wall_severity = result.get("wall_severity")
        if isinstance(wall_severity, (int, float)) and not isinstance(wall_severity, bool):
            wall_val = _finite_float(wall_severity, float("nan"))
            if math.isfinite(wall_val):
                route_friction_map[role] = wall_val

    if echo_states:
        state["_echo_states"] = echo_states
    if echo_voice_log_updated:
        state["echo_voice_log"] = echo_voice_log
    state["route_friction_map"] = route_friction_map
    echoes_reporting = len(seen_echoes)

    existing_aligned = set(
        x for x in (state.get("aligned_echo_ids") or []) if isinstance(x, str)
    )
    existing_aligned.update(x for x in aligned_ids if isinstance(x, str))
    state["aligned_echo_ids"] = list(existing_aligned)

    existing_defected = set(
        x for x in (state.get("defected_echo_ids") or []) if isinstance(x, str)
    )
    existing_defected.update(x for x in defected_ids if isinstance(x, str))
    state["defected_echo_ids"] = list(existing_defected)

    if existing_aligned:
        echo_stance_counts["aligned"] = len(existing_aligned)
    if existing_defected:
        echo_stance_counts["defected"] = len(existing_defected)

    # ------------------------------------------------------------------
    # Post-Aggregation: ECSI (Emergent Critical Section Identifier)
    # ------------------------------------------------------------------
    snapshot_data = {
        "tick": current_tick,
        "max_ticks": max_ticks,
        "real_time_minutes": round(real_time_minutes, 2) if math.isfinite(real_time_minutes) else 0.0,
        "runners_reporting": runners_reporting,
        "avg_velocity": round(avg_velocity, 3) if math.isfinite(avg_velocity) else 0.0,
        "avg_water": round(avg_water, 1) if math.isfinite(avg_water) else 0.0,
        "avg_distance": round(avg_distance, 3) if math.isfinite(avg_distance) else 0.0,
        "status_counts": status_counts,
        "persona_status_counts": persona_status_counts,
        "persona_avg_progress": persona_avg_progress,
        "notable_events": list(notable_events),  # L2: copy to prevent post-snapshot mutation
        "finished_runner_ids": finished_ids,
        # Leadership Crucible extensions
        "echoes_reporting": echoes_reporting,
        "echo_stance_counts": echo_stance_counts,
        "echo_sentiments": echo_sentiments_snapshot,
        "echo_actions": echo_actions_snapshot,
        "aligned_echo_ids": aligned_ids,
        "defected_echo_ids": defected_ids,
    }

    quest_config = {"resources": state.get("resources") or {}, "principles": principles}
    alerts = detect_contention(snapshot_data, quest_config, resource_state, state)
    if alerts:
        raw_alert_history = state.get("alert_history")
        alert_history = list(raw_alert_history) if isinstance(raw_alert_history, list) else []
        alert_history.extend([a.to_dict() for a in alerts])
        state["alert_history"] = alert_history

    # ------------------------------------------------------------------
    # Post-ECSI: Coaching micro-feedback and A2UI cards
    # ------------------------------------------------------------------
    micro_feedback = generate_micro_feedback(
        decision_tags=decision_tags,
        resource_state=resource_state,
        alerts=[a.to_dict() for a in alerts],
        tick=current_tick,
        contradiction_alert=contradiction_alert,
    )
    coaching_card = build_coaching_card(
        resource_state=resource_state,
        decision_tags=decision_tags,
        alerts=[a.to_dict() for a in alerts],
        tick=current_tick,
        max_ticks=max_ticks,
        contradiction_alert=contradiction_alert,
        previous_resource_state=previous_resource_state,
        last_coaching_signature=tuple(state["last_coaching_signature"])
        if isinstance(state.get("last_coaching_signature"), (list, tuple))
        else None,
    )

    # ------------------------------------------------------------------
    # Post-ECSI: Inter-Echo Influence Network propagation
    # ------------------------------------------------------------------
    influence_data = state.get("influence_network")
    if not influence_data and current_tick == 0:
        # First tick: initialize influence network from quest config or defaults
        quest_edges = state.get("influence_edges")
        if not quest_edges:
            echo_configs = state.get("echo_configs") or []
            quest_edges = build_default_influence_edges(echo_configs)
        network = InfluenceNetwork.from_quest_config(quest_edges)
        state["influence_network"] = network.to_dict_list()
    elif influence_data:
        network = InfluenceNetwork.from_dict_list(influence_data)
    else:
        network = None

    if network and len(network) > 0:
        if current_echo_sentiments_by_role:
            deltas = network.propagate(current_echo_sentiments_by_role)
            network.apply_decay()
            state["influence_network"] = network.to_dict_list()
            # Store deltas for the next tick's process_decision_round to consume
            state["influence_deltas"] = {
                k: round(delta_val, 4)
                for k, v in deltas.items()
                if math.isfinite(delta_val := _finite_float(v, 0.0))
            }

    # Build final OrganizationalState (deep-copy mutable state so later ticks
    # do not overwrite historical snapshots needed by ECSI dedup & replay)
    try:
        raw_org_completion_pct = (current_tick / max_ticks) * 100 if max_ticks > 0 else 0.0
    except OverflowError:
        raw_org_completion_pct = 0.0
    org_completion_pct = raw_org_completion_pct if math.isfinite(raw_org_completion_pct) else 0.0
    org_state = {
        "resources": copy.deepcopy(resource_state),
        "active_alerts": [a.to_dict() for a in alerts],
        "contradiction_log": list(state.get("contradiction_log") or []),
        "completion_percentage": org_completion_pct,
    }
    snapshot_data["organizational_state"] = org_state

    # ------------------------------------------------------------------
    # Stakeholder Voices: friction-biased thought sampling for live feed
    # ------------------------------------------------------------------
    voice_samples = _sample_friction_thoughts(
        messages,
        current_tick=current_tick,
        max_samples=1,
    )
    if voice_samples:
        snapshot_data["stakeholder_voices"] = voice_samples
        notable_events.extend(voice_samples)
        snapshot_data["notable_events"] = list(notable_events)

    raw_snapshots = state.get("tick_snapshots")
    snapshots_list = list(raw_snapshots) if isinstance(raw_snapshots, list) else []
    snapshots_list.append(snapshot_data)
    if len(snapshots_list) > 15:
        snapshots_list = snapshots_list[-15:]
    state["tick_snapshots"] = snapshots_list

    # Accumulate vitals trend separately — tick_snapshots is truncated for
    # LLM context window size, but compile_results needs the full trend for
    # the debrief report and longitudinal tracking.
    raw_vitals_full = state.get("vitals_trend_full")
    vitals_full = list(raw_vitals_full) if isinstance(raw_vitals_full, list) else []
    vitals_full.append(
        {
            "tick": current_tick,
            "real_time_minutes": round(real_time_minutes, 2) if math.isfinite(real_time_minutes) else 0.0,
            "avg_velocity": round(avg_velocity, 3) if math.isfinite(avg_velocity) else 0.0,
            "avg_water": round(avg_water, 1) if math.isfinite(avg_water) else 0.0,
            "avg_distance": round(avg_distance, 3) if math.isfinite(avg_distance) else 0.0,
        }
    )
    state["vitals_trend_full"] = vitals_full

    # Increment current_tick HERE inside the tool so it advances
    # even if the LLM calls advance_tick multiple times in one turn.
    state["current_tick"] = current_tick + 1

    # ------------------------------------------------------------------
    # Emit narrative pulse
    # ------------------------------------------------------------------
    safe_real_time = round(real_time_minutes, 1) if math.isfinite(real_time_minutes) else 0.0
    narrative = (
        f"Sprint {current_tick}/{max_ticks} ({safe_real_time} min): {runners_reporting} runners reporting"
    )
    if echoes_reporting > 0:
        narrative += f", {echoes_reporting} echoes reporting"
    if notable_events:
        narrative += f" | Events: {', '.join(notable_events)}"

    # ------------------------------------------------------------------
    # Compute traffic conditions (code-level, not LLM-driven)
    # ------------------------------------------------------------------
    traffic: dict | None = None
    traffic_model = state.get("traffic_model")
    if traffic_model:
        try:
            from agents.utils.traffic import compute_tick_traffic

            segment_index = traffic_model.get("segment_index", [])
            ticks_closed = traffic_model.get("ticks_closed", {})
            sweep_distance = _finite_float(avg_distance * 0.6, 0.0)  # back-of-pack estimate

            traffic_result = compute_tick_traffic(
                segment_index=segment_index,
                sweep_distance_mi=sweep_distance,
                current_tick=current_tick,
                ticks_closed=ticks_closed,
            )
            updated_traffic_model = dict(traffic_model)
            updated_traffic_model["ticks_closed"] = traffic_result["ticks_closed"]
            state["traffic_model"] = updated_traffic_model

            # Full per-segment data including coordinates for frontend
            # visualization.  The tick agent uses include_contents='none'
            # so prior tick responses don't accumulate in model context.
            traffic = {
                "sweep_distance_mi": round(sweep_distance, 3) if math.isfinite(sweep_distance) else 0.0,
                "segments": traffic_result["segments"],
                "overall_congestion": traffic_result["overall_congestion"],
                "tev_impact": traffic_result["tev_impact"],
            }
        except Exception as e:
            logger.error("advance_tick: traffic computation failed: %s", e, exc_info=True)

    logger.info("advance_tick: %s", narrative)

    # All data flows through the tool_end event — no custom gateway
    # messages.  The frontend reads tick + traffic from tool_end payload.
    # ------------------------------------------------------------------
    # Compute enriched tick data for frontend narrative
    # ------------------------------------------------------------------
    # Resource deltas: compare current resource state to previous tick
    raw_prev_res = state.get("_prev_tick_resources")
    prev_resources = raw_prev_res if isinstance(raw_prev_res, dict) else {}
    resource_deltas: dict[str, dict] = {}
    resource_display_names = {
        "lau": "Leadership Attention",
        "bt": "Budget",
        "tbu": "Team Bandwidth",
        "owb": "Org Well-Being",
    }
    for key, display_name in resource_display_names.items():
        pool = resource_state.get(key, {})
        current_val = _finite_float(pool.get("current"), 0.0) if isinstance(pool, dict) else 0.0
        prev_val = _finite_float(prev_resources.get(key), current_val)
        raw_delta = current_val - prev_val
        delta = round(raw_delta, 1) if math.isfinite(raw_delta) else 0.0
        critical = _finite_float(pool.get("critical_threshold"), 0.0) if isinstance(pool, dict) else 0.0
        resource_deltas[key] = {
            "name": display_name,
            "current": round(current_val, 1) if math.isfinite(current_val) else 0.0,
            "delta": delta,
            "critical_threshold": critical,
            "is_critical": current_val <= critical,
            "is_warning": current_val <= critical * 1.5,
        }
    # Store current resources for next tick's delta
    state["_prev_tick_resources"] = {
        key: (
            _finite_float(resource_state.get(key, {}).get("current"), 0.0)
            if isinstance(resource_state.get(key), dict)
            else 0.0
        )
        for key in resource_display_names
    }

    # Alignment completion percentage
    raw_completion_pct = (100.0 * avg_distance / race_dist) if race_dist > 0 else 0.0
    completion_pct = round(raw_completion_pct, 1) if math.isfinite(raw_completion_pct) else 0.0

    # Average comfort across runners (only those with explicit comfort data)
    raw_avg_comfort = total_comfort / comfort_count if comfort_count > 0 else 100.0
    avg_comfort = round(raw_avg_comfort, 1) if math.isfinite(raw_avg_comfort) else 0.0

    # Gatekeeper friction summary (wall_severity is on a 0.0-10.0 scale:
    # 0.0 = coalition boost, 5.0 = neutral baseline, >5.0 = active resistance drag)
    friction_entries = [
        {"role": role, "severity": sev_f}
        for role, sev in route_friction_map.items()
        if not isinstance(sev, bool)
        and isinstance(sev, (int, float))
        and math.isfinite(sev_f := _finite_float(sev, float("nan")))
        and sev_f > 5.0
    ]
    friction_entries.sort(key=lambda x: x["severity"], reverse=True)
    max_friction = friction_entries[0]["severity"] if friction_entries else 0.0
    friction_level = (
        "high"
        if max_friction >= 8.0
        else "moderate"
        if max_friction >= 6.5
        else "low"
        if max_friction > 5.0
        else "none"
    )

    # ------------------------------------------------------------------
    # Aggregate notable events into a summary for the frontend feed
    # ------------------------------------------------------------------
    # Categories: "stalled", "deadlocked", "aligned", "echo", "other"
    _TRANSITION_KEYWORDS = {
        "stalled": "stalled",
        "deadlocked": "deadlocked",
        "aligned": "aligned",
    }
    _PERSONA_TYPES = {"pragmatist", "visionary", "diplomat", "disruptor"}
    events_summary: dict[str, dict] = {}
    echo_events: list[str] = []
    other_events: list[str] = []

    for evt in notable_events:
        if not isinstance(evt, str):
            continue
        # Skip voice samples — they're handled separately
        if evt.startswith("💭"):
            continue
        if evt in echo_notable_events:
            echo_events.append(evt)
            continue
        # Check for stakeholder transition events
        matched = False
        for keyword, category in _TRANSITION_KEYWORDS.items():
            if keyword in evt.lower():
                if category not in events_summary:
                    events_summary[category] = {"total": 0, "by_persona": {}}
                events_summary[category]["total"] += 1
                # Extract persona from event string
                for persona in _PERSONA_TYPES:
                    if persona in evt.lower():
                        events_summary[category]["by_persona"][persona] = (
                            events_summary[category]["by_persona"].get(persona, 0) + 1
                        )
                        break
                matched = True
                break
        if not matched:
            other_events.append(evt)

    notable_events_summary = {
        "transitions": events_summary,
        "echoes": echo_events[:3],  # Cap at 3 most recent echo thoughts
        "other": other_events[:3],
    }

    result: dict = {
        "status": "success",
        "simulation_id": simulation_id,
        "tick": current_tick,
        "max_ticks": max_ticks,
        "real_time_minutes": round(real_time_minutes, 2) if math.isfinite(real_time_minutes) else 0.0,
        "runners_reporting": runners_reporting,
        "avg_velocity": round(avg_velocity, 3) if math.isfinite(avg_velocity) else 0.0,
        "avg_water": round(avg_water, 1) if math.isfinite(avg_water) else 0.0,
        "avg_distance": round(avg_distance, 3) if math.isfinite(avg_distance) else 0.0,
        "status_counts": status_counts,
        "finished_runner_ids": list(existing_finished),
        "collapsed_runner_ids": list(existing_collapsed),
        "notable_events": notable_events,
        "notable_events_summary": notable_events_summary,
        "message": narrative,
        # Phase 1: Enriched tick data for frontend narrative
        "completion_pct": completion_pct,
        "resource_deltas": resource_deltas,
        "persona_status_counts": persona_status_counts,
        "infrastructure": {
            "medical_saves_this_tick": total_medical_saves,
            "medical_events": medical_events_this_tick,
            "avg_comfort": avg_comfort,
            "comfort_warning": avg_comfort < 40,
            "active_gatekeepers": len(friction_entries),
            "max_friction": round(max_friction, 2),
            "friction_level": friction_level,
            "friction_details": friction_entries[:3],  # top 3 resistors
        },
        "active_alerts_count": len(alerts),
    }
    if traffic:
        result["traffic"] = traffic
    # Leadership coaching extensions
    result["micro_feedback"] = micro_feedback
    emit_session_id, emit_sim_id, emit_uid = await _resolve_emit_target(tool_context)
    emit_kwargs: dict[str, Any] = {"user_id": emit_uid} if emit_uid is not None else {}
    if coaching_card:
        # Emit the coaching card via A2UI gateway messages. Track step so a
        # Redis blip between surfaceUpdate and beginRendering is reported
        # (orphan-surface case) instead of silently corrupting the frontend.
        _coach_emit_step = "surfaceUpdate"
        try:
            await emit_gateway_message(
                origin={"type": "agent", "id": "executive_coach", "session_id": emit_session_id},
                destination=[emit_session_id],
                status="success",
                msg_type="a2ui",
                event="a2ui",
                data={"surfaceUpdate": coaching_card["surfaceUpdate"]},
                simulation_id=emit_sim_id,
                **emit_kwargs,
            )
            _coach_emit_step = "beginRendering"
            await emit_gateway_message(
                origin={"type": "agent", "id": "executive_coach", "session_id": emit_session_id},
                destination=[emit_session_id],
                status="success",
                msg_type="a2ui",
                event="a2ui",
                data={"beginRendering": coaching_card["beginRendering"]},
                simulation_id=emit_sim_id,
                **emit_kwargs,
            )
            result["coaching_card_emitted"] = True
        except Exception as e:
            # Per-tick coaching emit is best-effort — a failure here must not
            # crash the whole tick. Surface partial-emit case loudly so the
            # orphan surface is investigated.
            if _coach_emit_step == "beginRendering":
                logger.error(
                    "advance_tick: coaching card partial emit (session %s) — "
                    "surfaceUpdate landed but beginRendering failed: %s",
                    emit_session_id,
                    e,
                    exc_info=True,
                )
            else:
                logger.error(
                    "advance_tick: coaching card surfaceUpdate emit failed (session %s): %s",
                    emit_session_id,
                    e,
                    exc_info=True,
                )
            result["coaching_card_emitted"] = False
        # Persist dedup metadata: snapshot the resources the participant
        # just saw and remember the signature so we suppress identical
        # re-emissions on subsequent ticks. Session state must be
        # JSON-serializable in ADK, so store the signature as a list.
        state["last_coaching_signature"] = list(coaching_card["signature"])
        state["last_resource_snapshot"] = copy.deepcopy(resource_state)

    # ------------------------------------------------------------------
    # Emit live dashboard A2UI surface (updates every tick)
    # ------------------------------------------------------------------
    try:
        dashboard_payload = build_live_dashboard(
            tick=current_tick,
            max_ticks=max_ticks,
            completion_pct=completion_pct,
            resource_state=resource_state,
            resource_deltas=resource_deltas,
            status_counts=status_counts,
            persona_status_counts=persona_status_counts,
            infrastructure=result.get("infrastructure", {}),
            active_alerts_count=len(alerts),
            avg_velocity=avg_velocity,
            avg_water=avg_water,
            voices=[e for e in notable_events if isinstance(e, str) and e.startswith("\U0001f4ad")],
        )
        await emit_gateway_message(
            origin={"type": "agent", "id": "simulator", "session_id": emit_session_id},
            destination=[emit_session_id],
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data={"surfaceUpdate": dashboard_payload["surfaceUpdate"]},
            simulation_id=emit_sim_id,
            **emit_kwargs,
        )
        await emit_gateway_message(
            origin={"type": "agent", "id": "simulator", "session_id": emit_session_id},
            destination=[emit_session_id],
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data={"beginRendering": dashboard_payload["beginRendering"]},
            simulation_id=emit_sim_id,
            **emit_kwargs,
        )
    except Exception as e:
        logger.warning("advance_tick: live dashboard emission failed: %s", e)

    return result


async def check_race_complete(tool_context: ToolContext) -> dict:
    """Check whether the race has reached its maximum tick count.

    If current_tick >= max_ticks, sets ``tool_context.actions.escalate = True``
    to signal the LoopAgent to exit.

    Args:
        tool_context: ADK tool context for session state access.

    Returns:
        dict with race status and remaining ticks.
    """
    state = tool_context.state
    current_tick = _finite_int(state.get("current_tick"), 0)
    max_ticks = _finite_int(state.get("max_ticks"), DEFAULT_MAX_TICKS)

    if current_tick >= max_ticks:
        tool_context.actions.escalate = True
        logger.info(
            "check_race_complete: race complete at tick %d/%d",
            current_tick,
            max_ticks,
        )
        return {
            "status": "race_complete",
            "simulation_id": state.get("simulation_id"),
            "current_tick": current_tick,
            "max_ticks": max_ticks,
            "message": f"Race complete after {max_ticks} ticks",
        }

    ticks_remaining = max_ticks - current_tick
    logger.info(
        "check_race_complete: in_progress tick %d/%d (%d remaining)",
        current_tick,
        max_ticks,
        ticks_remaining,
    )
    return {
        "status": "in_progress",
        "simulation_id": state.get("simulation_id"),
        "current_tick": current_tick,
        "max_ticks": max_ticks,
        "ticks_remaining": ticks_remaining,
        "message": f"Race in progress: {ticks_remaining} ticks remaining",
    }
