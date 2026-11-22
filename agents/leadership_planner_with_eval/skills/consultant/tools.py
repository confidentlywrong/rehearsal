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

"""Consultant tools for Leadership Planner with Eval.

Generates and emits the 90-Day Action Plan playbook via A2UI.
"""

import logging
import math
from typing import Any

from google.adk.tools.tool_context import ToolContext

from agents.utils.pulses import emit_gateway_message

logger = logging.getLogger(__name__)


def _coerce_count_int(raw: Any) -> int | None:
    """Safely coerce numeric or comma-formatted string counts to a positive int."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(round(raw)) if math.isfinite(raw) and raw > 0 else None
    if isinstance(raw, str):
        cleaned = raw.strip().replace(",", "").replace("_", "")
        if not cleaned:
            return None
        try:
            fv = float(cleaned)
        except (ValueError, OverflowError):
            return None
        return int(round(fv)) if math.isfinite(fv) and fv > 0 else None
    return None


def _coerce_status_count(raw_val: Any) -> int:
    """Safely coerce a persona status count entry to a non-negative int."""
    if raw_val is None or isinstance(raw_val, bool):
        return 0
    if isinstance(raw_val, int):
        return max(0, raw_val)
    try:
        fv = float(raw_val)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(fv):
        return 0
    return max(0, int(round(fv)))


def _safe_ratio_pct_int(aligned: int, total: int) -> int:
    """Compute rounded integer percentage in [0, 100] without float OverflowError."""
    if total <= 0 or aligned <= 0:
        return 0
    q, r = divmod(100 * aligned, total)
    twice_r = 2 * r
    if twice_r > total or (twice_r == total and (q & 1)):
        q += 1
    return max(0, min(100, q))


def _safe_ratio_pct_float(aligned: int, total: int) -> float:
    """Compute 100.0 * aligned / total without float OverflowError on huge ints."""
    if total <= 0 or aligned <= 0:
        return 0.0
    if aligned >= total:
        return 100.0
    max_bits = max(aligned.bit_length(), total.bit_length())
    if max_bits > 900:
        shift = max_bits - 900
        a_scaled = aligned >> shift
        t_scaled = total >> shift
        if t_scaled <= 0:
            return 100.0 if aligned >= total else 0.0
        return 100.0 * a_scaled / t_scaled
    return 100.0 * aligned / total


def _normalize_simulation_id(raw_sim_id: Any) -> str | None:
    """Return a clean simulation_id or None (never leaking UI placeholders like '—')."""
    if raw_sim_id is None or isinstance(raw_sim_id, bool):
        return None
    s = str(raw_sim_id).strip()
    if not s or s in ("—", "-", "None", "null"):
        return None
    return s


async def _resolve_emit_target(tool_context: Any) -> tuple[str, str | None]:
    """Resolve (mapped_session_id, user_id) for A2UI gateway emission."""
    from agents.utils import simulation_registry
    from agents.utils.communication import resolve_tool_context_user_id

    raw_session = getattr(tool_context, "session", None)
    raw_sid = getattr(raw_session, "id", None)
    session_id = raw_sid.strip() if isinstance(raw_sid, str) and raw_sid.strip() else "default"

    try:
        uid = resolve_tool_context_user_id(tool_context, require_when_saas=False)
    except ValueError as exc:
        logger.warning("consultant: invalid user_id context for A2UI emit: %s", exc)
        uid = None

    if session_id != "default":
        try:
            mapped = (
                await simulation_registry.get_context_id(session_id, user_id=uid)
                if uid is not None
                else await simulation_registry.get_context_id(session_id)
            )
            if mapped:
                session_id = mapped
        except ValueError as exc:
            logger.debug("consultant: skipped context_id lookup for %s (%s)", session_id, exc)

    return session_id, uid


async def emit_action_plan_a2ui(tool_context: ToolContext, plan_markdown: str = "") -> str:
    """Emits the 90-Day Action Plan playbook as an A2UI card to the frontend.

    This function parses the input markdown text roughly into A2UI components
    so the LLM doesn't have to construct the verbose A2UI JSON payload itself.

    The A2UI card is emitted as two gateway messages (surfaceUpdate then
    beginRendering), following the same protocol used by coaching cards in
    ``advance_tick``.

    Args:
        tool_context: ADK tool context.
        plan_markdown: The markdown content of the 90-day action plan.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if state is None or not callable(getattr(state, "get", None)):
        logger.error("emit_action_plan_a2ui: tool_context or tool_context.state is missing/invalid")
        return "ERROR: tool_context with valid session state is required."
    if not isinstance(plan_markdown, str) or not plan_markdown.strip():
        logger.error("emit_action_plan_a2ui: plan_markdown is required and was empty or invalid")
        return (
            "ERROR: plan_markdown is required and must be a non-empty markdown string "
            "describing the 90-Day Action Plan."
        )
    lines = plan_markdown.strip().split("\n")

    components = []
    child_ids = []

    # Title component
    components.append(
        {
            "id": "playbook-title",
            "component": {
                "Text": {"text": {"literalString": "90-Day Action Plan (Consultant Playbook)"}, "usageHint": "h1"}
            },
        }
    )
    child_ids.append("playbook-title")

    components.append({"id": "playbook-div-1", "component": {"Divider": {}}})
    child_ids.append("playbook-div-1")

    # Simple block parsing
    block_id = 0
    current_text = []

    def _flush_block():
        nonlocal block_id, current_text
        if not current_text:
            return

        text_str = "\n".join(current_text).strip()
        if not text_str:
            return

        c_id = f"pb-block-{block_id}"
        block_id += 1

        # Check if header
        usage_hint = "body"
        if text_str.startswith("#"):
            text_str = text_str.lstrip("#").strip()
            usage_hint = "h2"

        components.append(
            {"id": c_id, "component": {"Text": {"text": {"literalString": text_str}, "usageHint": usage_hint}}}
        )
        child_ids.append(c_id)
        current_text = []

    def _is_bullet(s: str) -> bool:
        s_strip = s.lstrip()
        if not s_strip:
            return False
        if s_strip[:2] in ("- ", "* ", "• "):
            return True
        # Numbered bullet: "1. ", "12. ", etc.
        head = s_strip.split(" ", 1)[0]
        if head.endswith(".") and head[:-1].isdigit():
            return True
        return False

    # Keep consecutive bullets in a single Text body block but separate them
    # with a blank line so the markdown pipe renders them as a *loose* list
    # (each <li> wrapped in <p>) — that produces visible vertical spacing
    # without needing extra components.
    prev_was_bullet = False
    for line in lines:
        if line.startswith("#"):
            _flush_block()
            current_text.append(line)
            _flush_block()
            prev_was_bullet = False
        elif line.strip() == "":
            _flush_block()
            prev_was_bullet = False
        elif _is_bullet(line):
            if prev_was_bullet:
                current_text.append("")  # loose list separator
            current_text.append(line)
            prev_was_bullet = True
        else:
            current_text.append(line)
            prev_was_bullet = False

    _flush_block()

    components.append({"id": "playbook-content", "component": {"Column": {"children": {"explicitList": child_ids}}}})

    components.append({"id": "playbook-card", "component": {"Card": {"child": "playbook-content"}}})

    surface_update = {"surfaceUpdate": {"surfaceId": "action_plan", "components": components}}
    begin_rendering = {"beginRendering": {"surfaceId": "action_plan", "root": "playbook-card"}}

    session_id, uid = await _resolve_emit_target(tool_context)
    simulation_id = _normalize_simulation_id(state.get("simulation_id"))

    origin = {"type": "agent", "id": "consultant", "session_id": session_id}
    destination = [session_id]
    emit_kwargs: dict[str, Any] = {"user_id": uid} if uid is not None else {}

    # Emit the A2UI card as two gateway messages (surfaceUpdate then
    # beginRendering), matching the protocol used by coaching cards
    # in advance_tick (advancing-race-ticks/tools.py L686-L703).
    # Track which step we're on so a Redis blip between the two awaits
    # surfaces as a partial-failure error — the dangerous case is
    # success-then-failure, which leaves the frontend with an orphaned
    # surface registration and no render trigger.
    _emit_step = "surfaceUpdate"
    try:
        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=surface_update,
            simulation_id=simulation_id,
            **emit_kwargs,
        )
        _emit_step = "beginRendering"
        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=begin_rendering,
            simulation_id=simulation_id,
            **emit_kwargs,
        )
    except Exception as e:
        if _emit_step == "beginRendering":
            logger.error(
                "emit_action_plan_a2ui: partial emit for session %s — "
                "surfaceUpdate landed but beginRendering failed (%s). "
                "Frontend will hold an orphaned surface until session reset.",
                session_id,
                e,
                exc_info=True,
            )
        else:
            logger.error(
                "emit_action_plan_a2ui: surfaceUpdate emit failed for session %s: %s",
                session_id,
                e,
                exc_info=True,
            )
        raise

    logger.info("Successfully emitted 90-Day Action Plan A2UI for session %s", session_id)

    return "SUCCESS: The 90-Day Action Plan has been successfully emitted to the UI."


def _derive_coaching_insights(
    *,
    kotter_title: str,
    kotter_desc: str,
    adkar_title: str,
    adkar_desc: str,
    synthesis: str,
    persona_order: list[str],
    persona_pcts: dict[str, int | None],
    norm_counts: dict[str, dict[str, int]],
    simulated_str: str,
    sim_data: dict[str, Any],
    eval_result: dict[str, Any],
    scores_map: dict[str, int],
) -> tuple[str, str, str, str, str]:
    """Fill omitted Kotter/ADKAR/Synthesis insights from measured telemetry and evaluation data.

    Preserves caller-supplied non-empty text and falls back to ``"—"`` when
    neither simulation telemetry nor evaluator findings/suggestions exist.
    """
    ranked = sorted(
        [(p, int(persona_pcts[p])) for p in persona_order if persona_pcts.get(p) is not None],
        key=lambda item: item[1],
    )
    total_collapsed = sum(int(c.get("collapsed", 0)) for c in norm_counts.values())
    total_exhausted = sum(int(c.get("exhausted", 0)) for c in norm_counts.values())
    contradictions = sim_data.get("contradiction_log")
    n_contradictions = len(contradictions) if isinstance(contradictions, list) else 0
    black_swans = sim_data.get("triggered_black_swans")
    n_swans = len(black_swans) if isinstance(black_swans, list) else 0

    raw_findings_val = eval_result.get("findings")
    raw_findings: list[Any] = raw_findings_val if isinstance(raw_findings_val, list) else []
    findings = [f for f in raw_findings if isinstance(f, dict)]
    raw_suggs_val = eval_result.get("suggestions")
    if not isinstance(raw_suggs_val, list):
        raw_suggs_val = eval_result.get("improvement_suggestions")
    raw_suggs: list[Any] = raw_suggs_val if isinstance(raw_suggs_val, list) else []
    suggestions = [s.strip() for s in raw_suggs if isinstance(s, str) and s.strip()]

    has_telemetry = bool(
        ranked or norm_counts or n_contradictions or n_swans or findings or suggestions
    )
    if not has_telemetry:
        return (
            kotter_title or "—",
            kotter_desc or "—",
            adkar_title or "—",
            adkar_desc or "—",
            synthesis or "—",
        )

    criterion_labels = {
        "stakeholder_alignment": "Stakeholder Alignment",
        "decision_quality": "Decision Quality",
        "crisis_readiness": "Crisis Readiness",
        "resource_stewardship": "Resource Stewardship",
        "strategic_coherence": "Strategic Coherence",
        "ethical_alignment": "Ethical Alignment",
    }
    lowest_crit = min(scores_map.items(), key=lambda item: item[1]) if scores_map else ("crisis_readiness", 0)
    lowest_label = criterion_labels.get(lowest_crit[0], lowest_crit[0].replace("_", " ").title())
    crisis_score = scores_map.get("crisis_readiness", 0)

    weak_persona, weak_pct = ranked[0] if ranked else ("", 0)
    top_persona, top_pct = ranked[-1] if ranked else ("", 0)

    if not kotter_title:
        if ranked and weak_pct < 60:
            kotter_title = f"Kotter Step 2-4: Stabilize {weak_persona} Coalition ({weak_pct}%)"
        elif n_contradictions > 0 or crisis_score < 70:
            kotter_title = f"Kotter Step 1-5: Resolve Systemic Friction (Crisis {crisis_score}/100)"
        elif ranked:
            kotter_title = f"Kotter Step 5-8: Scale {top_persona} Alignment ({top_pct}%)"
        elif findings:
            kotter_title = f"Kotter Focus: {str(findings[0].get('title') or lowest_label).strip()}"
        else:
            kotter_title = f"Kotter Focus: Strengthen {lowest_label} ({lowest_crit[1]}/100)"

    if not kotter_desc:
        parts: list[str] = []
        if ranked:
            cohort_bits = (
                f"{weak_persona} reached {weak_pct}% vs. {top_persona} {top_pct}%"
                if len(ranked) > 1
                else f"{top_persona} reached {top_pct}%"
            )
            if simulated_str and simulated_str != "—":
                cohort_bits += f" across {simulated_str} stakeholders"
            weak_counts = norm_counts.get(weak_persona, {})
            weak_loss = int(weak_counts.get("collapsed", 0)) + int(weak_counts.get("exhausted", 0))
            if weak_loss > 0:
                cohort_bits += f" ({weak_loss} {weak_persona} collapsed/exhausted)"
            parts.append(cohort_bits)
        elif findings:
            f0 = findings[0]
            parts.append(
                str(f0.get("description") or f0.get("title") or "").strip()
            )
        if n_contradictions > 0:
            parts.append(f"{n_contradictions} commitment contradiction(s) logged")
        if n_swans > 0:
            parts.append(f"{n_swans} black-swan trigger(s) active")
        parts.append(f"Crisis Readiness {crisis_score}/100")
        kotter_desc = "; ".join(p for p in parts if p) + "."

    if not adkar_title:
        if total_collapsed > 0:
            adkar_title = f"ADKAR Ability & Reinforcement ({total_collapsed} Collapsed)"
        elif total_exhausted > 0:
            adkar_title = f"ADKAR Reinforcement Risk ({total_exhausted} Exhausted)"
        elif ranked:
            adkar_title = f"ADKAR Desire & Ability ({weak_persona} {weak_pct}%)"
        else:
            adkar_title = f"ADKAR Priority: Lift {lowest_label} ({lowest_crit[1]}/100)"

    if not adkar_desc:
        parts = []
        if total_collapsed > 0 or total_exhausted > 0:
            parts.append(f"{total_collapsed} collapsed and {total_exhausted} exhausted across cohorts")
        parts.append(f"weakest dimension is {lowest_label} ({lowest_crit[1]}/100)")
        if suggestions:
            parts.append(f"action: {suggestions[0]}")
        adkar_desc = "; ".join(parts) + "."

    if not synthesis:
        overall_val = eval_result.get("overall_score", "—")
        if ranked and simulated_str != "—":
            synthesis = (
                f"Overall score {overall_val}/100 across {simulated_str} stakeholders; "
                f"prioritize {weak_persona} ({weak_pct}%) enablement and {lowest_label}."
            )
        elif suggestions:
            synthesis = suggestions[0]
        else:
            synthesis = f"Overall evaluation {overall_val}/100 with {lowest_label} at {lowest_crit[1]}/100."

    return (kotter_title, kotter_desc, adkar_title, adkar_desc, synthesis)


async def emit_sim_results_a2ui(
    tool_context: ToolContext,
    scenario_name: str = "",
    overall_score: int = 0,
    stakeholder_alignment: int = 0,
    decision_quality: int = 0,
    crisis_readiness: int = 0,
    coaching_kotter_title: str = "",
    coaching_kotter_desc: str = "",
    coaching_adkar_title: str = "",
    coaching_adkar_desc: str = "",
    coaching_synthesis: str = "",
) -> str:
    """Emits the Simulation Results dashboard as an A2UI card.

    Deterministically builds the three-card layout (Main Results, Strategy
    Overview, Coaching Insights) from structured data so the LLM does not
    need to construct A2UI JSON manually.

    Args:
        tool_context: ADK tool context.
        scenario_name: Title of the simulation scenario.
        overall_score: Overall evaluation score (0-100 integer).
        stakeholder_alignment: Stakeholder Alignment score (0-100).
        decision_quality: Decision Quality score (0-100).
        crisis_readiness: Crisis Readiness score (0-100).
        coaching_kotter_title: Kotter coaching insight title.
        coaching_kotter_desc: Kotter coaching insight description.
        coaching_adkar_title: ADKAR coaching insight title.
        coaching_adkar_desc: ADKAR coaching insight description.
        coaching_synthesis: One-sentence synthesis takeaway.
    """
    state = getattr(tool_context, "state", None) if tool_context is not None else None
    if state is None or not callable(getattr(state, "get", None)):
        logger.error("emit_sim_results_a2ui: tool_context or tool_context.state is missing/invalid")
        return "ERROR: tool_context with valid session state is required."

    # --- Auto-record simulation outcome if active_scenario_id is present ---
    active_sid = state.get("active_scenario_id")
    if active_sid and isinstance(state.get("simulation_result"), dict):
        try:
            from agents.leadership_planner_with_memory.memory.tools import (
                record_scenario_outcome,
            )

            rec_res = await record_scenario_outcome(
                scenario_id=str(active_sid), tool_context=tool_context
            )
            if isinstance(rec_res, dict) and rec_res.get("status") == "error":
                logger.warning(
                    "emit_sim_results_a2ui: record_scenario_outcome returned error for %s: %s",
                    active_sid,
                    rec_res.get("message"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "emit_sim_results_a2ui: auto record_scenario_outcome failed for %s: %s",
                active_sid,
                exc,
                exc_info=True,
            )

    # --- Recover scenario_name from session state when omitted ---
    scenario_state = state.get("leadership_scenario")
    if not isinstance(scenario_name, str) or not scenario_name.strip():
        if isinstance(scenario_state, dict):
            scenario_name = str(
                scenario_state.get("name")
                or scenario_state.get("theme")
                or "—"
            )
        else:
            scenario_name = "—"

    # --- Recover scores from session state (require valid evaluation) ----
    raw_eval = state.get("leadership_evaluation") or state.get("evaluation_result")
    eval_result: dict = raw_eval if isinstance(raw_eval, dict) else {}
    raw_eval_score = eval_result.get("overall_score") if eval_result.get("status") != "error" else None
    has_valid_state_eval = False
    if (
        eval_result.get("status") != "error"
        and not isinstance(raw_eval_score, bool)
        and isinstance(raw_eval_score, (int, float))
    ):
        try:
            eval_score_f = float(raw_eval_score)
            has_valid_state_eval = math.isfinite(eval_score_f) and eval_score_f > 0
        except (TypeError, ValueError, OverflowError):
            has_valid_state_eval = False

    def _parse_score_int(v: Any) -> int:
        if v is None or isinstance(v, bool):
            return -1
        if isinstance(v, int):
            try:
                fv = float(v)
            except OverflowError:
                return -1
            return v if math.isfinite(fv) else -1
        if isinstance(v, float):
            if not math.isfinite(v):
                return -1
            return int(round(v))
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return -1
            try:
                fv = float(s)
            except (TypeError, ValueError, OverflowError):
                return -1
            return int(round(fv)) if math.isfinite(fv) else -1
        try:
            return int(v)
        except (TypeError, ValueError, OverflowError):
            return -1

    parsed_overall = _parse_score_int(overall_score)
    arg_overall = parsed_overall if parsed_overall >= 0 else 0

    if arg_overall <= 0 and not has_valid_state_eval:
        logger.error(
            "emit_sim_results_a2ui: no valid evaluation scores available (overall_score=%r, has_valid_state_eval=%s)",
            overall_score,
            has_valid_state_eval,
        )
        return (
            "ERROR: No valid evaluation scores available. Run 'evaluate_leadership_plan' "
            "or pass valid positive evaluation scores before calling emit_sim_results_a2ui."
        )

    eval_scores = (
        eval_result.get("scores")
        if has_valid_state_eval and isinstance(eval_result.get("scores"), dict)
        else {}
    )

    coaching_kotter_title = coaching_kotter_title.strip() if isinstance(coaching_kotter_title, str) else ""
    coaching_kotter_desc = coaching_kotter_desc.strip() if isinstance(coaching_kotter_desc, str) else ""
    coaching_adkar_title = coaching_adkar_title.strip() if isinstance(coaching_adkar_title, str) else ""
    coaching_adkar_desc = coaching_adkar_desc.strip() if isinstance(coaching_adkar_desc, str) else ""
    if isinstance(coaching_synthesis, str) and coaching_synthesis.strip():
        coaching_synthesis = coaching_synthesis.strip()
    else:
        state_summary = eval_result.get("summary") if has_valid_state_eval else None
        coaching_synthesis = (
            state_summary.strip()
            if isinstance(state_summary, str) and state_summary.strip()
            else ""
        )

    def _coalesce(arg_value, state_value):
        """Use arg if it's a real positive int; otherwise the state value."""
        iv = _parse_score_int(arg_value)
        if iv > 0:
            return iv

        sv = _parse_score_int(state_value)
        if sv >= 0:
            return sv
        return iv if iv >= 0 else 0

    overall_score = _coalesce(overall_score, eval_result.get("overall_score"))
    stakeholder_alignment = _coalesce(stakeholder_alignment, eval_scores.get("stakeholder_alignment"))
    decision_quality = _coalesce(decision_quality, eval_scores.get("decision_quality"))
    crisis_readiness = _coalesce(crisis_readiness, eval_scores.get("crisis_readiness"))

    # Self-heal the 3 additional criteria (not LLM parameters — read from state only)
    resource_stewardship = max(0, min(100, int(_coalesce(None, eval_scores.get("resource_stewardship")))))
    strategic_coherence = max(0, min(100, int(_coalesce(None, eval_scores.get("strategic_coherence")))))
    ethical_alignment = max(0, min(100, int(_coalesce(None, eval_scores.get("ethical_alignment")))))

    logger.info(
        "CONSULTANT_DIAG: sim_results scores after state-heal: overall=%s, sa=%s, dq=%s, cr=%s (eval_state_present=%s)",
        overall_score,
        stakeholder_alignment,
        decision_quality,
        crisis_readiness,
        bool(eval_result),
    )

    # Clamp all score parameters to valid 0-100 range
    overall_score = max(0, min(100, int(overall_score or 0)))
    stakeholder_alignment = max(0, min(100, int(stakeholder_alignment or 0)))
    decision_quality = max(0, min(100, int(decision_quality or 0)))
    crisis_readiness = max(0, min(100, int(crisis_readiness or 0)))

    simulation_id = _normalize_simulation_id(state.get("simulation_id"))
    sim_id_display = simulation_id or "—"
    expected = (
        _coerce_count_int(scenario_state.get("stakeholder_count") if isinstance(scenario_state, dict) else None)
        or _coerce_count_int(state.get("expected_stakeholder_count"))
    )

    # --- Load structured data from Redis side-channel ---
    # compile_results stores persona_status_counts and runner_count to Redis
    # so they survive the A2A text boundary between simulator and planner.
    from agents.utils.communication import resolve_tool_context_user_id
    from agents.utils.simdata import SimDataError, load_simulation_data

    sim_data: dict = {}
    if simulation_id:
        try:
            sim_user_id = resolve_tool_context_user_id(tool_context, require_when_saas=False)
            sim_data = (
                await (
                    load_simulation_data(simulation_id, user_id=sim_user_id)
                    if sim_user_id is not None
                    else load_simulation_data(simulation_id)
                )
            ) or {}
        except (SimDataError, ValueError) as exc:
            logger.error(
                "emit_sim_results_a2ui: failed to load simulation data for %s: %s",
                simulation_id,
                exc,
                exc_info=True,
            )
            return f"ERROR: Failed to load simulation telemetry for {simulation_id}: {exc}"
        logger.info(
            "CONSULTANT_DIAG: load_simulation_data(%s) returned keys=%s, persona_status_counts=%s, runner_count=%s",
            simulation_id,
            list(sim_data.keys()) if sim_data else "EMPTY",
            sim_data.get("persona_status_counts"),
            sim_data.get("runner_count"),
        )
    else:
        logger.warning(
            "CONSULTANT_DIAG: simulation_id is missing or placeholder: %r",
            state.get("simulation_id"),
        )

    # Extract runner_count: Redis side-channel → simulation_config → state
    runner_count = _coerce_count_int(sim_data.get("runner_count"))
    if runner_count is None:
        sim_config = state.get("simulation_config")
        if isinstance(sim_config, dict):
            runner_count = _coerce_count_int(sim_config.get("runner_count"))
    if runner_count is None:
        runner_count = _coerce_count_int(state.get("expected_stakeholder_count"))
    if runner_count is None and isinstance(scenario_state, dict):
        runner_count = _coerce_count_int(scenario_state.get("stakeholder_count"))

    # Format stakeholder counts with comma separators
    expected_str = f"{expected:,}" if expected is not None else "—"
    simulated_str = f"{runner_count:,}" if runner_count else "—"

    # --- Persona progress from Redis side-channel ---
    raw_persona_counts = sim_data.get("persona_status_counts")
    persona_counts: dict[str, Any] = (
        raw_persona_counts if isinstance(raw_persona_counts, dict) else {}
    )
    raw_persona_progress = sim_data.get("persona_avg_progress")
    persona_progress: dict[str, Any] = (
        raw_persona_progress if isinstance(raw_persona_progress, dict) else {}
    )

    # Fallback: try session state (works if called within the simulator session)
    if not persona_counts:
        state_counts = state.get("persona_status_counts")
        persona_counts = state_counts if isinstance(state_counts, dict) else {}
    if not persona_progress:
        state_progress = state.get("persona_avg_progress")
        persona_progress = state_progress if isinstance(state_progress, dict) else {}

    # Calculate persona display values
    persona_order = ["Visionary", "Diplomat", "Pragmatist", "Disruptor"]
    # Normalize keys to title case and sanitize counts to non-negative ints
    norm_counts: dict[str, dict[str, int]] = {}
    for k, v in persona_counts.items():
        if isinstance(k, str) and isinstance(v, dict):
            cleaned_counts = {
                sk: _coerce_status_count(sv)
                for sk, sv in v.items()
                if isinstance(sk, str)
            }
            if cleaned_counts:
                norm_counts[k.title()] = cleaned_counts
    # Preserve None to distinguish "no measurement" from "measured zero".
    # The old behavior coerced None→00.0 then later filtered with `> 0`,
    # which silently dropped a legitimately measured 0% — the exact case
    # the user most needs to see on a leadership dashboard.
    norm_progress: dict[str, float | None] = {}
    for k, v in persona_progress.items():
        if not isinstance(k, str):
            continue
        if v is None or isinstance(v, bool):
            norm_progress[k.title()] = None
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError, OverflowError):
            norm_progress[k.title()] = None
            continue
        norm_progress[k.title()] = fv if math.isfinite(fv) else None

    persona_values: dict[str, str] = {}
    # Parallel numeric percentages used as ScoreBar values. None means "no data"
    # — we omit the ScoreBar instead of emitting a misleading 0.
    persona_pcts: dict[str, int | None] = {}
    for persona in persona_order:
        progress_pct = norm_progress.get(persona)
        counts = norm_counts.get(persona, {})

        if progress_pct is not None:
            # Primary metric: average alignment progress. 0.0 is a valid
            # measurement and must render as a red 0% bar, not be dropped.
            pct = max(0, min(100, int(round(progress_pct))))
            persona_values[persona] = f"{pct}% Progress"
            persona_pcts[persona] = pct
        elif counts:
            # Fallback: binary aligned count
            aligned = counts.get("finished", 0) + counts.get("aligned", 0)
            total = sum(counts.values())
            if total > 0:
                pct = _safe_ratio_pct_int(aligned, total)
                persona_values[persona] = f"{pct}% Aligned" if pct > 0 else "0% Progress"
                persona_pcts[persona] = pct
            else:
                persona_values[persona] = "—"
                persona_pcts[persona] = None
        else:
            persona_values[persona] = "—"
            persona_pcts[persona] = None

    logger.info(
        "CONSULTANT_DIAG: persona_values=%s, norm_progress=%s, norm_counts_keys=%s",
        persona_values,
        norm_progress,
        list(norm_counts.keys()),
    )

    # Determine winning strategy using progress metric (more granular than binary aligned)
    best_persona = "—"
    best_pct = 0.0
    for persona in persona_order:
        progress_pct = norm_progress.get(persona)
        if progress_pct is not None and progress_pct > best_pct:
            best_pct = progress_pct
            best_persona = persona

    # Fallback: if no progress data, try binary aligned count
    if best_persona == "—":
        for persona in persona_order:
            counts = norm_counts.get(persona, {})
            if counts:
                aligned = counts.get("finished", 0) + counts.get("aligned", 0)
                total = sum(counts.values())
                if total > 0:
                    pct = _safe_ratio_pct_float(aligned, total)
                    if pct > best_pct:
                        best_pct = pct
                        best_persona = persona

    winning_desc = f"{best_persona}" if best_persona != "—" else "No clear winner"

    (
        coaching_kotter_title,
        coaching_kotter_desc,
        coaching_adkar_title,
        coaching_adkar_desc,
        coaching_synthesis,
    ) = _derive_coaching_insights(
        kotter_title=coaching_kotter_title,
        kotter_desc=coaching_kotter_desc,
        adkar_title=coaching_adkar_title,
        adkar_desc=coaching_adkar_desc,
        synthesis=coaching_synthesis,
        persona_order=persona_order,
        persona_pcts=persona_pcts,
        norm_counts=norm_counts,
        simulated_str=simulated_str,
        sim_data=sim_data,
        eval_result=eval_result,
        scores_map={
            "stakeholder_alignment": stakeholder_alignment,
            "decision_quality": decision_quality,
            "crisis_readiness": crisis_readiness,
            "resource_stewardship": resource_stewardship,
            "strategic_coherence": strategic_coherence,
            "ethical_alignment": ethical_alignment,
        },
    )

    # ---- Build A2UI components ----
    components: list[dict] = []

    # === Card 1: Main Results ===
    components.extend(
        [
            {"id": "tag-main", "component": {"Text": {"text": {"literalString": "SIMULATED"}, "usageHint": "label"}}},
            {
                "id": "sim-meta-main",
                "component": {"Text": {"text": {"literalString": sim_id_display}, "usageHint": "caption"}},
            },
            {"id": "tag-row-main", "component": {"Row": {"children": {"explicitList": ["tag-main", "sim-meta-main"]}}}},
            {"id": "title-main", "component": {"Text": {"text": {"literalString": scenario_name}, "usageHint": "h2"}}},
            {
                "id": "left-col-main",
                "component": {"Column": {"children": {"explicitList": ["tag-row-main", "title-main"]}}},
            },
            {
                "id": "score-num-main",
                "component": {"Text": {"text": {"literalString": str(overall_score)}, "usageHint": "h1"}},
            },
            {
                "id": "score-lbl-main",
                "component": {"Text": {"text": {"literalString": "Score"}, "usageHint": "caption"}},
            },
            {
                "id": "score-col-main",
                "component": {"Column": {"children": {"explicitList": ["score-num-main", "score-lbl-main"]}}},
            },
            {
                "id": "header-main",
                "component": {"Row": {"children": {"explicitList": ["left-col-main", "score-col-main"]}}},
            },
            {
                "id": "stake-l-main",
                "component": {
                    "Text": {"text": {"literalString": "Stakeholders (expected/simulated)"}, "usageHint": "body"}
                },
            },
            {
                "id": "stake-v-main",
                "component": {
                    "Text": {"text": {"literalString": f"{expected_str}/{simulated_str}"}, "usageHint": "body"}
                },
            },
            {
                "id": "stake-r-main",
                "component": {"Row": {"children": {"explicitList": ["stake-l-main", "stake-v-main"]}}},
            },
            {"id": "d1-main", "component": {"Divider": {}}},
            {
                "id": "sa-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Stakeholder Alignment"},
                        "value": {"literalNumber": int(stakeholder_alignment)},
                    }
                },
            },
            {
                "id": "dq-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Decision Quality"},
                        "value": {"literalNumber": int(decision_quality)},
                    }
                },
            },
            {
                "id": "cr-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Crisis Readiness"},
                        "value": {"literalNumber": int(crisis_readiness)},
                    }
                },
            },
            {
                "id": "rs-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Resource Stewardship"},
                        "value": {"literalNumber": int(resource_stewardship)},
                    }
                },
            },
            {
                "id": "sc-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Strategic Coherence"},
                        "value": {"literalNumber": int(strategic_coherence)},
                    }
                },
            },
            {
                "id": "ea-main",
                "component": {
                    "ScoreBar": {
                        "label": {"literalString": "Ethical Alignment"},
                        "value": {"literalNumber": int(ethical_alignment)},
                    }
                },
            },
            {"id": "d2-main", "component": {"Divider": {}}},
            {"id": "rerun-txt-main", "component": {"Text": {"text": {"literalString": "Re-run Simulation"}}}},
            {
                "id": "rerun-btn-main",
                "component": {
                    "Button": {
                        "child": "rerun-txt-main",
                        "action": {"name": "run_simulation"},
                        "primary": {"literalBoolean": True},
                    }
                },
            },
            {
                "id": "main-content",
                "component": {
                    "Column": {
                        "children": {
                            "explicitList": [
                                "header-main",
                                "stake-r-main",
                                "d1-main",
                                "sa-main",
                                "dq-main",
                                "cr-main",
                                "rs-main",
                                "sc-main",
                                "ea-main",
                                "d2-main",
                                "rerun-btn-main",
                            ]
                        }
                    }
                },
            },
            {"id": "card-main", "component": {"Card": {"child": "main-content"}}},
        ]
    )

    # === Card 2: Strategy Overview ===
    # Build persona breakdown as ScoreBars when we have numeric data,
    # otherwise fall back to a body Text saying "— (no data)" so the row
    # is still informative without faking a 0 score.
    strategy_components: list[dict] = [
        {
            "id": "strategy-title",
            "component": {
                "Text": {"text": {"literalString": "Strategy Overview (Persona Breakdown)"}, "usageHint": "h2"}
            },
        },
    ]
    persona_child_ids: list[str] = ["strategy-title"]
    persona_id_map = {
        "Visionary": "vis",
        "Diplomat": "dip",
        "Pragmatist": "prg",
        "Disruptor": "dis",
    }
    for persona, prefix in persona_id_map.items():
        pct = persona_pcts.get(persona)
        if pct is not None:
            strategy_components.append(
                {
                    "id": f"{prefix}-bar",
                    "component": {
                        "ScoreBar": {
                            "label": {"literalString": persona},
                            "value": {"literalNumber": int(pct)},
                        }
                    },
                }
            )
            persona_child_ids.append(f"{prefix}-bar")
        else:
            strategy_components.extend(
                [
                    {
                        "id": f"{prefix}-lbl",
                        "component": {"Text": {"text": {"literalString": persona}, "usageHint": "body"}},
                    },
                    {
                        "id": f"{prefix}-val",
                        "component": {
                            "Text": {"text": {"literalString": persona_values[persona]}, "usageHint": "body"}
                        },
                    },
                    {
                        "id": f"{prefix}-row",
                        "component": {"Row": {"children": {"explicitList": [f"{prefix}-lbl", f"{prefix}-val"]}}},
                    },
                ]
            )
            persona_child_ids.append(f"{prefix}-row")
    strategy_components.extend(
        [
            {"id": "d-strategy", "component": {"Divider": {}}},
            {
                "id": "winning-lbl",
                "component": {"Text": {"text": {"literalString": "Winning Strategy"}, "usageHint": "caption"}},
            },
            {
                "id": "winning-desc",
                "component": {"Text": {"text": {"literalString": winning_desc}, "usageHint": "body"}},
            },
        ]
    )
    persona_child_ids.extend(["d-strategy", "winning-lbl", "winning-desc"])
    strategy_components.append(
        {"id": "strategy-content", "component": {"Column": {"children": {"explicitList": persona_child_ids}}}}
    )
    strategy_components.append(
        {
            "id": "card-strategy",
            "component": {"Card": {"child": "strategy-content", "expandable": {"literalBoolean": True}}},
        }
    )
    components.extend(strategy_components)

    # === Card 3: Coaching Insights ===
    components.extend(
        [
            {
                "id": "coach-title-card",
                "component": {"Text": {"text": {"literalString": "Coaching Insights"}, "usageHint": "h2"}},
            },
            {
                "id": "c1-title",
                "component": {"Text": {"text": {"literalString": coaching_kotter_title}, "usageHint": "body"}},
            },
            {"id": "c1-fw", "component": {"Text": {"text": {"literalString": "KOTTER"}, "usageHint": "caption"}}},
            {"id": "c1-row", "component": {"Row": {"children": {"explicitList": ["c1-title", "c1-fw"]}}}},
            {
                "id": "c1-desc",
                "component": {"Text": {"text": {"literalString": coaching_kotter_desc}, "usageHint": "body"}},
            },
            {
                "id": "c2-title",
                "component": {"Text": {"text": {"literalString": coaching_adkar_title}, "usageHint": "body"}},
            },
            {"id": "c2-fw", "component": {"Text": {"text": {"literalString": "ADKAR"}, "usageHint": "caption"}}},
            {"id": "c2-row", "component": {"Row": {"children": {"explicitList": ["c2-title", "c2-fw"]}}}},
            {
                "id": "c2-desc",
                "component": {"Text": {"text": {"literalString": coaching_adkar_desc}, "usageHint": "body"}},
            },
            {"id": "c3-title", "component": {"Text": {"text": {"literalString": "Synthesis"}, "usageHint": "body"}}},
            {"id": "c3-fw", "component": {"Text": {"text": {"literalString": "TAKEAWAY"}, "usageHint": "caption"}}},
            {"id": "c3-row", "component": {"Row": {"children": {"explicitList": ["c3-title", "c3-fw"]}}}},
            {
                "id": "c3-desc",
                "component": {"Text": {"text": {"literalString": coaching_synthesis}, "usageHint": "body"}},
            },
            {
                "id": "coach-content",
                "component": {
                    "Column": {
                        "children": {
                            "explicitList": [
                                "coach-title-card",
                                "c1-row",
                                "c1-desc",
                                "c2-row",
                                "c2-desc",
                                "c3-row",
                                "c3-desc",
                            ]
                        }
                    }
                },
            },
            {
                "id": "card-coaching",
                "component": {"Card": {"child": "coach-content", "expandable": {"literalBoolean": True}}},
            },
        ]
    )

    # === Card 4: Transformation Status ===
    # Pull rich outcome data from the Redis side-channel that was already loaded above.
    raw_vitals = sim_data.get("vitals_trend")
    total_ticks = len(raw_vitals) if isinstance(raw_vitals, list) else 0
    raw_alerts = sim_data.get("alert_history")
    alert_history: list = raw_alerts if isinstance(raw_alerts, list) else []
    alert_count = len(alert_history)
    raw_quest_config = sim_data.get("quest_config")
    quest_config: dict = raw_quest_config if isinstance(raw_quest_config, dict) else {}
    raw_black_swans = sim_data.get("triggered_black_swans")
    if not isinstance(raw_black_swans, list):
        raw_black_swans = quest_config.get("triggered_black_swans")
    black_swans: list = raw_black_swans if isinstance(raw_black_swans, list) else []
    black_swan_count = len(black_swans)

    # Capital / resource status from session state (more reliable than redis for planner)
    raw_resources = sim_data.get("resources") or state.get("resources") or {}
    resources: dict = raw_resources if isinstance(raw_resources, dict) else {}
    capital_val = (
        resources.get("leadership_capital")
        if resources.get("leadership_capital") is not None
        else resources.get("capital")
    )
    capital_initial = None
    if capital_val is None:
        # Simulator resource pools are keyed lau/bt/tbu/owb (case varies);
        # leadership capital is the LAU (Leadership Attention Units) pool.
        lau_pool = resources.get("lau") if resources.get("lau") is not None else resources.get("LAU")
        if isinstance(lau_pool, dict):
            capital_val = lau_pool.get("current")
            capital_initial = lau_pool.get("initial")
        elif isinstance(lau_pool, (int, float)) and not isinstance(lau_pool, bool):
            capital_val = lau_pool
    try:
        if capital_val is not None and not isinstance(capital_val, bool):
            cap_f = float(capital_val)
            if not math.isfinite(cap_f):
                raise ValueError("non-finite capital")
            capital_str = f"{int(round(cap_f)):,}"
            if capital_initial is not None and not isinstance(capital_initial, bool):
                init_f = float(capital_initial)
                if math.isfinite(init_f) and init_f > 0:
                    capital_str = f"{capital_str} / {int(round(init_f)):,} LAU"
        else:
            capital_str = "—"
    except (TypeError, ValueError, OverflowError):
        capital_str = "—"

    # Compute overall alignment rate from persona status counts
    total_runners_all = 0
    aligned_all = 0
    for v in (persona_counts or {}).values():
        if isinstance(v, dict):
            total_runners_all += sum(_coerce_status_count(sv) for sv in v.values())
            aligned_all += _coerce_status_count(v.get("finished")) + _coerce_status_count(v.get("aligned"))
    alignment_rate_str = (
        f"{_safe_ratio_pct_int(aligned_all, total_runners_all)}%" if total_runners_all > 0 else "—"
    )

    # Transformation outcome label based on overall score
    if overall_score >= 80:
        outcome_label = "Transformation Successful"
    elif overall_score >= 60:
        outcome_label = "Partial Transformation"
    elif overall_score >= 40:
        outcome_label = "Transformation Stalled"
    else:
        outcome_label = "Transformation Failed"

    status_components: list[dict] = [
        {
            "id": "status-title",
            "component": {"Text": {"text": {"literalString": "Transformation Status"}, "usageHint": "h2"}},
        },
        {
            "id": "status-outcome-val",
            "component": {"Text": {"text": {"literalString": outcome_label}, "usageHint": "h3"}},
        },
        {"id": "status-d1", "component": {"Divider": {}}},
        {
            "id": "status-align-lbl",
            "component": {"Text": {"text": {"literalString": "Overall Alignment Rate"}, "usageHint": "caption"}},
        },
        {
            "id": "status-align-val",
            "component": {"Text": {"text": {"literalString": alignment_rate_str}, "usageHint": "body"}},
        },
        {
            "id": "status-align-row",
            "component": {"Row": {"children": {"explicitList": ["status-align-lbl", "status-align-val"]}}},
        },
        {
            "id": "status-capital-lbl",
            "component": {"Text": {"text": {"literalString": "Leadership Capital Remaining"}, "usageHint": "caption"}},
        },
        {
            "id": "status-capital-val",
            "component": {"Text": {"text": {"literalString": capital_str}, "usageHint": "body"}},
        },
        {
            "id": "status-capital-row",
            "component": {"Row": {"children": {"explicitList": ["status-capital-lbl", "status-capital-val"]}}},
        },
        {
            "id": "status-ticks-lbl",
            "component": {"Text": {"text": {"literalString": "Simulation Cycles Run"}, "usageHint": "caption"}},
        },
        {
            "id": "status-ticks-val",
            "component": {"Text": {"text": {"literalString": str(total_ticks) if total_ticks else "—"}, "usageHint": "body"}},
        },
        {
            "id": "status-ticks-row",
            "component": {"Row": {"children": {"explicitList": ["status-ticks-lbl", "status-ticks-val"]}}},
        },
        {"id": "status-d2", "component": {"Divider": {}}},
        {
            "id": "status-bs-lbl",
            "component": {"Text": {"text": {"literalString": "Black Swan Events Triggered"}, "usageHint": "caption"}},
        },
        {
            "id": "status-bs-val",
            "component": {"Text": {"text": {"literalString": str(black_swan_count)}, "usageHint": "body"}},
        },
        {
            "id": "status-bs-row",
            "component": {"Row": {"children": {"explicitList": ["status-bs-lbl", "status-bs-val"]}}},
        },
        {
            "id": "status-alert-lbl",
            "component": {"Text": {"text": {"literalString": "Critical Alerts Raised"}, "usageHint": "caption"}},
        },
        {
            "id": "status-alert-val",
            "component": {"Text": {"text": {"literalString": str(alert_count)}, "usageHint": "body"}},
        },
        {
            "id": "status-alert-row",
            "component": {"Row": {"children": {"explicitList": ["status-alert-lbl", "status-alert-val"]}}},
        },
        {
            "id": "status-content",
            "component": {
                "Column": {
                    "children": {
                        "explicitList": [
                            "status-title",
                            "status-outcome-val",
                            "status-d1",
                            "status-align-row",
                            "status-capital-row",
                            "status-ticks-row",
                            "status-d2",
                            "status-bs-row",
                            "status-alert-row",
                        ]
                    }
                }
            },
        },
        {
            "id": "card-status",
            "component": {"Card": {"child": "status-content", "expandable": {"literalBoolean": True}}},
        },
    ]
    components.extend(status_components)

    # === Root ===
    components.append(
        {
            "id": "root-col",
            "component": {"Column": {"children": {"explicitList": ["card-main", "card-strategy", "card-status", "card-coaching"]}}},
        }
    )

    surface_update = {
        "surfaceUpdate": {
            "surfaceId": "sim_results",
            "components": components,
        }
    }
    begin_rendering = {
        "beginRendering": {
            "surfaceId": "sim_results",
            "root": "root-col",
        }
    }

    session_id, uid = await _resolve_emit_target(tool_context)
    origin = {"type": "agent", "id": "consultant", "session_id": session_id}
    destination = [session_id]
    emit_kwargs: dict[str, Any] = {"user_id": uid} if uid is not None else {}

    # Track emit step so a mid-pair failure is reported as orphan-surface
    # (the partial-failure case) rather than swallowed.
    _emit_step = "surfaceUpdate"
    try:
        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=surface_update,
            simulation_id=simulation_id,
            **emit_kwargs,
        )
        _emit_step = "beginRendering"
        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=begin_rendering,
            simulation_id=simulation_id,
            **emit_kwargs,
        )
    except Exception as e:
        if _emit_step == "beginRendering":
            logger.error(
                "emit_sim_results_a2ui: partial emit for session %s — "
                "surfaceUpdate landed but beginRendering failed (%s). "
                "Frontend will hold an orphaned surface until session reset.",
                session_id,
                e,
                exc_info=True,
            )
        else:
            logger.error(
                "emit_sim_results_a2ui: surfaceUpdate emit failed for session %s: %s",
                session_id,
                e,
                exc_info=True,
            )
        raise

    logger.info("Emitted Simulation Results A2UI for session %s (score=%s)", session_id, overall_score)

    return "SUCCESS: The Simulation Results dashboard has been emitted to the UI. You MUST now call emit_action_plan_a2ui to complete the 90-day action plan."
