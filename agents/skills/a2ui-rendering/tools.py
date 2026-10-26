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

"""A2UI v0.8.0 validation tool for the ADK agent framework.

Validates A2UI payloads against the v0.8.0 specification: component types,
required properties, typed value wrappers, ID uniqueness, and reference
resolution.
"""

import json
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# A2UI v0.8.0 catalog constants
# ---------------------------------------------------------------------------

VALID_TYPES: set[str] = {
    "Text",
    "Image",
    "Icon",
    "Video",
    "AudioPlayer",
    "Row",
    "Column",
    "List",
    "Card",
    "Tabs",
    "Modal",
    "Divider",
    "Button",
    "TextField",
    "CheckBox",
    "Slider",
    "MultipleChoice",
    "DateTimeInput",
    "ScoreBar",
    "StakeholderGraph",
}

REQUIRED_PROPS: dict[str, list[str]] = {
    "Text": ["text"],
    "Image": ["url"],
    "Icon": ["name"],
    "Video": ["url"],
    "AudioPlayer": ["url"],
    "Button": ["child", "action"],
    "TextField": ["label"],
    "CheckBox": ["label"],
    "Slider": ["value"],
    "ScoreBar": ["value"],
    "MultipleChoice": ["selections"],
    "Card": ["child"],
    "Modal": ["entryPointChild", "contentChild"],
    "Tabs": ["tabItems"],
    "Row": ["children"],
    "Column": ["children"],
    "List": ["children"],
    "StakeholderGraph": ["payload"],
}

# Properties that must use typed string wrappers (literalString or path)
STRING_PROPS: set[str] = {"text", "url", "name", "label", "description"}

# Properties that must use typed boolean wrappers (literalBoolean or path)
BOOLEAN_PROPS: set[str] = {"autoplay", "primary", "expandable"}

# Properties that must use typed numeric wrappers (literalNumber or path),
# scoped per component type because `value` is overloaded across primitives
# (boolean on CheckBox, string on TextField, numeric on Slider/ScoreBar).
NUMERIC_PROPS_BY_TYPE: dict[str, set[str]] = {
    "Slider": {"value", "minValue", "maxValue"},
    "ScoreBar": {"value", "minValue", "maxValue"},
}

# Container types that require children with explicitList format
CONTAINER_TYPES: set[str] = {"Row", "Column", "List"}

# Valid top-level A2UI message type keys
VALID_MESSAGE_TYPES: set[str] = {
    "beginRendering",
    "surfaceUpdate",
    "dataModelUpdate",
    "deleteSurface",
}


# ---------------------------------------------------------------------------
# Violation helper
# ---------------------------------------------------------------------------


def _violation(
    component_id: str,
    field: str,
    message: str,
) -> dict[str, str]:
    """Create a structured violation dict."""
    return {
        "component_id": component_id,
        "field": field,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_typed_wrapper(
    component_id: str,
    prop_name: str,
    prop_value: Any,
    violations: list[dict[str, str]],
    comp_type: str = "",
) -> None:
    """Validate that a property uses the correct typed value wrapper."""
    if prop_name in STRING_PROPS:
        if not isinstance(prop_value, dict) or ("literalString" not in prop_value and "path" not in prop_value):
            violations.append(
                _violation(
                    component_id,
                    prop_name,
                    f"Property '{prop_name}' must use a typed wrapper "
                    f'({{"literalString": "..."}} or {{"path": "..."}}) '
                    f"instead of a raw value.",
                )
            )
    elif prop_name in BOOLEAN_PROPS:
        if not isinstance(prop_value, dict) or ("literalBoolean" not in prop_value and "path" not in prop_value):
            violations.append(
                _violation(
                    component_id,
                    prop_name,
                    f"Property '{prop_name}' must use a typed wrapper "
                    f'({{"literalBoolean": ...}} or {{"path": "..."}}) '
                    f"instead of a raw value.",
                )
            )
    elif prop_name in NUMERIC_PROPS_BY_TYPE.get(comp_type, set()):
        # Per-type numeric enforcement: catches a raw `{"value": 50}` on a
        # ScoreBar/Slider that previously slipped through because the
        # frontend's resolveValue() passes primitives unchanged. Without
        # this, the model gets no feedback when it forgets the wrapper.
        if not isinstance(prop_value, dict) or ("literalNumber" not in prop_value and "path" not in prop_value):
            violations.append(
                _violation(
                    component_id,
                    prop_name,
                    f"Property '{prop_name}' on {comp_type} must use a typed wrapper "
                    f'({{"literalNumber": <int>}} or {{"path": "..."}}) '
                    f"instead of a raw value.",
                )
            )


def _validate_children_format(
    component_id: str,
    comp_type: str,
    props: dict[str, Any],
    violations: list[dict[str, str]],
) -> None:
    """Validate that container children use explicitList format."""
    if comp_type not in CONTAINER_TYPES:
        return
    children = props.get("children")
    if children is None:
        return
    if isinstance(children, list):
        violations.append(
            _violation(
                component_id,
                "children",
                f"Container '{comp_type}' must use "
                f'{{"children": {{"explicitList": [...]}}}} '
                f"instead of a raw array. Wrap with explicitList.",
            )
        )
    elif isinstance(children, dict):
        if "explicitList" not in children:
            violations.append(
                _violation(
                    component_id,
                    "children",
                    f"Container '{comp_type}' children dict must contain an 'explicitList' key.",
                )
            )


def _collect_referenced_ids(
    component_id: str,
    comp_type: str,
    props: dict[str, Any],
    refs: list[tuple[str, str, str]],
) -> None:
    """Collect all component ID references for resolution checking.

    Each entry in *refs* is (component_id, field_name, referenced_id).
    """
    # child (Card, Modal entryPointChild/contentChild)
    for field in ("child", "entryPointChild", "contentChild"):
        val = props.get(field)
        if isinstance(val, str):
            refs.append((component_id, field, val))

    # children.explicitList
    children = props.get("children")
    if isinstance(children, dict):
        explicit = children.get("explicitList")
        if isinstance(explicit, list):
            for ref_id in explicit:
                if isinstance(ref_id, str):
                    refs.append((component_id, "children.explicitList", ref_id))

    # tabItems[].child
    tab_items = props.get("tabItems")
    if isinstance(tab_items, list):
        for i, item in enumerate(tab_items):
            if isinstance(item, dict):
                child_ref = item.get("child")
                if isinstance(child_ref, str):
                    refs.append((component_id, f"tabItems[{i}].child", child_ref))


def _validate_component(
    entry: Any,
    all_ids: set[str],
    refs: list[tuple[str, str, str]],
    violations: list[dict[str, str]],
) -> None:
    """Validate one component entry; append violations and collect references."""
    if not isinstance(entry, dict):
        violations.append(_violation("", "component", "Each component entry must be an object."))
        return

    comp_id = entry.get("id", "")
    component_def = entry.get("component", {})

    if not isinstance(component_def, dict) or not component_def:
        violations.append(_violation(comp_id, "component", "Missing or empty component definition."))
        return

    if comp_id in all_ids:
        violations.append(
            _violation(
                comp_id,
                "id",
                f"Duplicate component ID '{comp_id}'. IDs must be unique within a surfaceUpdate.",
            )
        )
    all_ids.add(comp_id)

    comp_type = next(iter(component_def), None)
    if comp_type not in VALID_TYPES:
        violations.append(
            _violation(
                comp_id,
                "component",
                f"Unknown component type '{comp_type}'. Valid types: {sorted(VALID_TYPES)}.",
            )
        )
        return

    props = component_def.get(comp_type, {})
    if not isinstance(props, dict):
        props = {}

    for req_prop in REQUIRED_PROPS.get(comp_type, []):
        if req_prop not in props:
            violations.append(
                _violation(
                    comp_id,
                    req_prop,
                    f"Component '{comp_type}' requires property '{req_prop}'.",
                )
            )

    for prop_name, prop_value in props.items():
        _validate_typed_wrapper(comp_id, prop_name, prop_value, violations, comp_type)

    _validate_children_format(comp_id, comp_type, props, violations)
    _collect_referenced_ids(comp_id, comp_type, props, refs)


# [START a2ui_validation_passes]
def _validate_surface_update(data: dict[str, Any]) -> list[dict[str, str]]:
    """Validate a surfaceUpdate message's components array in two passes."""
    violations: list[dict[str, str]] = []

    surface_update = data.get("surfaceUpdate", {})
    components = surface_update.get("components", [])

    if not isinstance(components, list):
        violations.append(_violation("", "surfaceUpdate.components", "components must be an array."))
        return violations

    all_ids: set[str] = set()
    refs: list[tuple[str, str, str]] = []

    # Pass 1: per-component checks; collect ids and refs.
    for entry in components:
        _validate_component(entry, all_ids, refs, violations)

    # Pass 2: every collected ref must resolve to a known id.
    for source_id, field, ref_id in refs:
        if ref_id not in all_ids:
            violations.append(
                _violation(
                    source_id,
                    field,
                    f"Reference '{ref_id}' does not resolve to any component ID in this surfaceUpdate.",
                )
            )

    return violations


# [END a2ui_validation_passes]


# ---------------------------------------------------------------------------
# Deterministic Dashboard Builder (from session state)
# ---------------------------------------------------------------------------


def _has_leadership_state(tool_context: Any) -> bool:
    """Return True when tool_context.state holds real dict scenario/evaluation data."""
    state = getattr(tool_context, "state", None)
    if state is None or not hasattr(state, "get"):
        return False
    try:
        scenario = state.get("leadership_scenario")
        evaluation = state.get("leadership_evaluation") or state.get("evaluation_result")
    except Exception:
        return False
    return isinstance(scenario, dict) or isinstance(evaluation, dict)


def _clamp_score(val: Any, default: int = 75) -> int:
    try:
        n = int(round(float(val)))
        return max(0, min(100, n))
    except (TypeError, ValueError):
        return default


def _build_dashboard_from_state(tool_context: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Construct a valid A2UI v0.8.0 dashboard surfaceUpdate + beginRendering from state."""
    if not _has_leadership_state(tool_context):
        return None

    state = tool_context.state
    raw_scenario = state.get("leadership_scenario")
    scenario: dict[str, Any] = raw_scenario if isinstance(raw_scenario, dict) else {}
    raw_eval = state.get("leadership_evaluation") or state.get("evaluation_result")
    evaluation: dict[str, Any] = raw_eval if isinstance(raw_eval, dict) else {}

    active_id = state.get("active_scenario_id")
    plan_meta = f"#{str(active_id)[:6]}" if isinstance(active_id, str) and active_id.strip() else "#PLAN"
    overall_score = _clamp_score(evaluation.get("overall_score"), default=75)
    title_str = str(
        scenario.get("name")
        or scenario.get("title")
        or "Enterprise AI Transformation Scenario"
    )

    raw_scores = evaluation.get("scores")
    scores: dict[str, Any] = raw_scores if isinstance(raw_scores, dict) else {}
    sa_val = _clamp_score(scores.get("stakeholder_alignment"), default=overall_score)
    dq_val = _clamp_score(scores.get("decision_quality"), default=overall_score)
    rs_val = _clamp_score(scores.get("resource_stewardship"), default=overall_score)
    cr_val = _clamp_score(scores.get("crisis_readiness"), default=overall_score)
    sc_val = _clamp_score(scores.get("strategic_coherence"), default=overall_score)
    ea_val = _clamp_score(scores.get("ethical_alignment"), default=overall_score)

    raw_findings = evaluation.get("findings")
    findings_list = [f for f in raw_findings if isinstance(f, dict)] if isinstance(raw_findings, list) else []
    if not findings_list:
        fallback_desc = str(
            evaluation.get("summary")
            or scenario.get("summary")
            or "Consensus pathway and stakeholder alignment evaluated across 6 leadership pillars."
        )
        findings_list = [
            {
                "title": "Scenario Evaluation Summary",
                "severity": "MINOR",
                "description": fallback_desc,
            }
        ]

    components: list[dict[str, Any]] = [
        {"id": "tag", "component": {"Text": {"text": {"literalString": "PLAN"}, "usageHint": "label"}}},
        {"id": "plan-meta", "component": {"Text": {"text": {"literalString": plan_meta}, "usageHint": "caption"}}},
        {"id": "tag-row", "component": {"Row": {"children": {"explicitList": ["tag", "plan-meta"]}}}},
        {"id": "score-num", "component": {"Text": {"text": {"literalString": str(overall_score)}, "usageHint": "h1"}}},
        {"id": "score-lbl", "component": {"Text": {"text": {"literalString": "Score"}, "usageHint": "caption"}}},
        {"id": "score-col", "component": {"Column": {"children": {"explicitList": ["score-num", "score-lbl"]}}}},
        {"id": "title", "component": {"Text": {"text": {"literalString": title_str}, "usageHint": "h2"}}},
        {"id": "left-col", "component": {"Column": {"children": {"explicitList": ["tag-row", "title"]}}}},
        {"id": "header", "component": {"Row": {"children": {"explicitList": ["left-col", "score-col"]}}}},
        {"id": "sa", "component": {"ScoreBar": {"label": {"literalString": "Stakeholder Alignment"}, "value": {"literalNumber": sa_val}}}},
        {"id": "dq", "component": {"ScoreBar": {"label": {"literalString": "Decision Quality"}, "value": {"literalNumber": dq_val}}}},
        {"id": "rs", "component": {"ScoreBar": {"label": {"literalString": "Resource Stewardship"}, "value": {"literalNumber": rs_val}}}},
        {"id": "grid-left", "component": {"Column": {"children": {"explicitList": ["sa", "dq", "rs"]}}}},
        {"id": "cr", "component": {"ScoreBar": {"label": {"literalString": "Crisis Readiness"}, "value": {"literalNumber": cr_val}}}},
        {"id": "sc", "component": {"ScoreBar": {"label": {"literalString": "Strategic Coherence"}, "value": {"literalNumber": sc_val}}}},
        {"id": "ea", "component": {"ScoreBar": {"label": {"literalString": "Ethical Alignment"}, "value": {"literalNumber": ea_val}}}},
        {"id": "grid-right", "component": {"Column": {"children": {"explicitList": ["cr", "sc", "ea"]}}}},
        {"id": "grid", "component": {"Row": {"children": {"explicitList": ["grid-left", "grid-right"]}}}},
        {"id": "d1", "component": {"Divider": {}}},
        {"id": "findings-hdr", "component": {"Text": {"text": {"literalString": "Findings"}, "usageHint": "caption"}}},
    ]

    finding_child_ids: list[str] = []
    for idx, finding in enumerate(findings_list[:4], start=1):
        f_title = str(finding.get("title") or f"Finding {idx}")
        f_sev = str(finding.get("severity") or "MINOR").strip().upper()
        if f_sev not in ("MAJOR", "MODERATE", "MINOR"):
            f_sev = "MINOR"
        sev_hint = f"severity-{f_sev.lower()}"
        f_desc = str(finding.get("description") or "\u2014")
        t_id = f"f{idx}-title"
        s_id = f"f{idx}-sev"
        r_id = f"f{idx}-row"
        d_id = f"f{idx}-desc"
        components.extend(
            [
                {"id": t_id, "component": {"Text": {"text": {"literalString": f_title}, "usageHint": "body"}}},
                {"id": s_id, "component": {"Text": {"text": {"literalString": f_sev}, "usageHint": sev_hint}}},
                {"id": r_id, "component": {"Row": {"children": {"explicitList": [t_id, s_id]}}}},
                {"id": d_id, "component": {"Text": {"text": {"literalString": f_desc}, "usageHint": "body"}}},
            ]
        )
        finding_child_ids.extend([r_id, d_id])

    components.extend(
        [
            {"id": "d2", "component": {"Divider": {}}},
            {"id": "btn-text", "component": {"Text": {"text": {"literalString": "Run Simulation"}}}},
            {
                "id": "btn1",
                "component": {
                    "Button": {
                        "child": "btn-text",
                        "action": {"name": "run_simulation"},
                        "primary": {"literalBoolean": True},
                    }
                },
            },
            {"id": "btn-row", "component": {"Row": {"children": {"explicitList": ["btn1"]}}}},
            {
                "id": "content",
                "component": {
                    "Column": {
                        "children": {
                            "explicitList": [
                                "header",
                                "grid",
                                "d1",
                                "findings-hdr",
                                *finding_child_ids,
                                "d2",
                                "btn-row",
                            ]
                        }
                    }
                },
            },
            {"id": "card1", "component": {"Card": {"child": "content"}}},
        ]
    )

    surface_update = {
        "surfaceUpdate": {
            "surfaceId": "dashboard",
            "components": components,
        }
    }
    begin_rendering = {
        "beginRendering": {
            "surfaceId": "dashboard",
            "root": "card1",
        }
    }
    return surface_update, begin_rendering


async def _emit_dashboard_from_state(tool_context: Any) -> dict[str, Any] | None:
    """Auto-persist scenario if needed, build dashboard A2UI from state, and emit it."""
    if not _has_leadership_state(tool_context):
        return None

    state = tool_context.state
    if not isinstance(state.get("active_scenario_id"), str) and isinstance(state.get("leadership_scenario"), dict):
        try:
            from agents.leadership_planner_with_memory.memory.tools import store_scenario

            await store_scenario(tool_context)
        except Exception:
            logger.debug("validate_and_emit_a2ui: auto store_scenario skipped/failed", exc_info=True)

    built = _build_dashboard_from_state(tool_context)
    if built is None:
        return None
    surface_update, begin_rendering = built

    emitted_directly = False
    try:
        from agents.utils.pulses import emit_gateway_message

        session_id = tool_context.session.id
        simulation_id = state.get("simulation_id")
        origin = {"type": "agent", "id": "planner", "session_id": session_id}
        destination = [session_id]

        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=surface_update,
            simulation_id=simulation_id,
        )
        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=begin_rendering,
            simulation_id=simulation_id,
        )
        emitted_directly = True
    except Exception:
        logger.debug(
            "validate_and_emit_a2ui: direct gateway emit for state dashboard unavailable; using tool result fallback",
            exc_info=True,
        )

    if emitted_directly:
        return {
            "status": "success",
            "message": "A2UI dashboard emitted successfully. Planning workflow complete — write your concise executive summary for the user now.",
        }
    return {
        "status": "success",
        "a2ui": surface_update,
        "message": "A2UI dashboard emitted successfully. Planning workflow complete — write your concise executive summary for the user now.",
    }


# ---------------------------------------------------------------------------
# Main tool function
# ---------------------------------------------------------------------------


async def validate_and_emit_a2ui(
    payload: str = "dashboard",
    tool_context: ToolContext = None,
) -> dict:
    """Validate an A2UI v0.8.0 JSON payload, emit it via Redis, and return it.

    Args:
        payload: A JSON string containing a valid A2UI v0.8.0 message
            (surfaceUpdate, beginRendering, dataModelUpdate, or deleteSurface),
            or "dashboard" to deterministically construct and emit the leadership
            evaluation dashboard from session state.
        tool_context: ADK tool context for session/state access.

    Returns:
        On success: {"status": "success", "a2ui": <validated_payload>}
        On failure: {"status": "error", "violations": [...], "suggestion": "..."}
            Each violation is a dict with "component_id", "field", and "message" keys.
    """
    if isinstance(payload, dict):
        payload = json.dumps(payload)

    stripped_input = payload.strip() if isinstance(payload, str) else ""
    if stripped_input.lower() in ("dashboard", "auto", "{}") and _has_leadership_state(tool_context):
        state_res = await _emit_dashboard_from_state(tool_context)
        if state_res is not None:
            return state_res

    # 1. JSON parsing — detect concatenated objects and unwrap {"a2ui": ...}
    try:
        decoder = json.JSONDecoder()
        stripped = payload.strip()
        data, end_idx = decoder.raw_decode(stripped)
        trailing = stripped[end_idx:].strip()
        if trailing:
            # Distinguish genuine concatenated JSON ({...}{...}) from a
            # mechanical model emission slip (extra brace, dangling text).
            # Only the former is a real "two messages in one call" bug we
            # want to surface to the model. The latter just causes the model
            # to retry identically (planner_with_memory's "9-card storm") or
            # give up and emit text only (planner_with_eval). See
            # docs/plans/2026-04-19-a2ui-validator-lenient-trailing-garbage-design.md.
            try:
                decoder.raw_decode(trailing)
            except (json.JSONDecodeError, ValueError):
                # Trailing content is not a parseable JSON object — drop it
                # silently and proceed with the first valid object.
                logger.warning(
                    "A2UI validator: ignoring %d bytes of trailing garbage after valid JSON object: %r",
                    len(trailing),
                    trailing[:80],
                )
            else:
                return {
                    "status": "error",
                    "violations": [
                        _violation(
                            "",
                            "payload",
                            "Multiple JSON objects detected. Combine all routes "
                            "into a single surfaceUpdate with one components array. "
                            "Call validate_and_emit_a2ui once per message type.",
                        ),
                    ],
                    "suggestion": (
                        "Build ONE surfaceUpdate containing ALL route cards in a "
                        "single flat components array. Do NOT concatenate separate "
                        "JSON objects."
                    ),
                }
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        if _has_leadership_state(tool_context):
            state_res = await _emit_dashboard_from_state(tool_context)
            if state_res is not None:
                logger.info("validate_and_emit_a2ui: recovered from invalid JSON using session state dashboard")
                return state_res
        return {
            "status": "error",
            "violations": [
                _violation("", "payload", f"Invalid JSON: {exc}"),
            ],
            "suggestion": "Provide a valid JSON string.",
        }

    if not isinstance(data, dict):
        if _has_leadership_state(tool_context):
            state_res = await _emit_dashboard_from_state(tool_context)
            if state_res is not None:
                return state_res
        return {
            "status": "error",
            "violations": [
                _violation("", "payload", "Payload must be a JSON object."),
            ],
            "suggestion": "Wrap your payload in a JSON object.",
        }

    # Unwrap {"a2ui": {...}} wrapper if present (common LLM mistake —
    # the wrapper is the tool's *output* format, not its input format).
    if "a2ui" in data and isinstance(data["a2ui"], dict) and len(data) == 1:
        data = data["a2ui"]

    # 2. Message type dispatch
    detected_type = None
    for msg_type in VALID_MESSAGE_TYPES:
        if msg_type in data:
            detected_type = msg_type
            break

    if detected_type is None:
        if _has_leadership_state(tool_context):
            state_res = await _emit_dashboard_from_state(tool_context)
            if state_res is not None:
                logger.info("validate_and_emit_a2ui: recovered from missing message_type using session state dashboard")
                return state_res
        return {
            "status": "error",
            "violations": [
                _violation(
                    "",
                    "message_type",
                    f"No recognized A2UI message type found. Expected one of: {sorted(VALID_MESSAGE_TYPES)}.",
                ),
            ],
            "suggestion": ("Include one of: beginRendering, surfaceUpdate, dataModelUpdate, or deleteSurface."),
        }

    # 3. For surfaceUpdate, run full component validation
    violations: list[dict[str, str]] = []
    if detected_type == "surfaceUpdate":
        surface_obj = data.get("surfaceUpdate")
        if not isinstance(surface_obj, dict) or not surface_obj.get("components"):
            violations.append(
                _violation(
                    "",
                    "surfaceUpdate.components",
                    "surfaceUpdate must contain a non-empty 'components' array.",
                )
            )
        else:
            violations = _validate_surface_update(data)

    # [START a2ui_validation_result]
    if violations:
        if _has_leadership_state(tool_context):
            state_res = await _emit_dashboard_from_state(tool_context)
            if state_res is not None:
                logger.info(
                    "validate_and_emit_a2ui: repaired %d A2UI violation(s) using deterministic dashboard from session state",
                    len(violations),
                )
                return state_res
        return {
            "status": "error",
            "violations": violations,
            "suggestion": ("Fix the listed violations to produce a valid A2UI v0.8.0 payload."),
        }

    logger.debug("A2UI payload validated successfully (type=%s)", detected_type)

    # 4. Emit validated payload to the frontend via Redis pub/sub.
    # This ensures reliable delivery instead of depending on the ADK
    # response pipeline to incidentally forward the tool result.
    emitted_directly = False
    try:
        from agents.utils.pulses import emit_gateway_message

        session_id = tool_context.session.id
        simulation_id = tool_context.state.get("simulation_id")

        origin = {"type": "agent", "id": "planner", "session_id": session_id}
        destination = [session_id]

        await emit_gateway_message(
            origin=origin,
            destination=destination,
            status="success",
            msg_type="a2ui",
            event="a2ui",
            data=data,
            simulation_id=simulation_id,
        )
        emitted_directly = True
    except Exception:
        # Redis emit failed. We still return success because the plugin
        # pipeline (_emit_narrative) will re-extract the embedded "a2ui"
        # key from this tool's result and deliver it via the indirect path.
        # Logged at ERROR (not WARNING) because if BOTH paths fail the
        # frontend never sees the update and we'd otherwise miss it.
        logger.error(
            "validate_and_emit_a2ui: Redis emission failed; falling back to "
            "plugin-based delivery via tool result. If frontend renders nothing, "
            "the plugin pipeline also failed \u2014 check _emit_narrative logs.",
            exc_info=True,
        )

    if emitted_directly:
        # Omit "a2ui" key to prevent double-delivery — the plugin's
        # _emit_narrative() would otherwise re-extract and re-emit it.
        return {"status": "success", "message": "A2UI emitted successfully"}
    else:
        # Fallback: include "a2ui" so the plugin's _emit_narrative()
        # picks it up and delivers via the indirect path.
        return {"status": "success", "a2ui": data}
    # [END a2ui_validation_result]
