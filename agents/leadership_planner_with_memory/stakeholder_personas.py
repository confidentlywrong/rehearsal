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

"""Stakeholder-personas extraction tool (Artifact 1, MiroFish-style).

This is the **persona elaboration** layer of the leadership_planner_with_memory
workflow — the missing layer between "node label on a graph" and "synthetic
reaction quote". Modelled after MiroFish's persona-generation stage
(`OasisProfileGenerator.generate_profile_from_entity`) but adapted to
organizational discourse:

* Personas are anchored to Step-0 stakeholder-graph nodes.
* Attributes are domain-appropriate: archetype (visionary, pragmatist,
  guardian, resistor, bridge_builder) instead of MBTI; communication style
  instead of Twitter follower counts; response_delay measured in sprints
  instead of minutes; primary_concerns instead of interest topics.
* `sentiment_bias` (float ∈ [-1.0, 1.0]) is preserved from MiroFish — it
  is the simulation knob that biases each persona's stance shifts across
  ticks. Always tagged `inferred: true` so the UI marks it synthetic.

Design choices (locked 2026-06-28 by the operator):

* **The agent IS the elaborator.** No nested LLM call. The agent reads the
  Step-0 graph + the user's prompt and supplies the personas directly.
  Same lightweight pattern as ``emit_stakeholder_graph`` /
  ``emit_stakeholder_reactions``.
* **No persistence yet.** Personas live in ``tool_context.state`` and
  stream out via the tool_end payload. Postgres tables come later when
  Artifact 2 (multi-tick simulation) lands and persistence starts to
  matter. **Explicit GCP-friendly choice: no Neo4j required.**
* **Invented data is allowed here** — this tool lives in the simulation
  layer. The analytic stakeholder_graph stays untouched.
* **One persona per Actor/Group node.** Beliefs, Frictions, and
  Structures do not get personas (they don't "speak" or "decide").

Downstream consumers:
* Artifact 2's multi-tick loop will read `influence_weight` + `response_delay_sprints`
  + `sentiment_bias` to drive contagion math.
* The reactions tool (Artifact 0.5) will read `stance` + `primary_concerns`
  to ground its synthetic utterances.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# Allowed archetypes. Five-band quantization — covers the full transformation-
# adoption spectrum without exploding the design space. Mirrors common
# change-management literature (visionary vs guardian vs pragmatist axes).
ALLOWED_ARCHETYPES: frozenset[str] = frozenset({"visionary", "pragmatist", "guardian", "resistor", "bridge_builder"})

# Allowed communication styles. Four bands chosen to differentiate persona
# tone at simulation time (the reactions tool will use this to flavor
# `reaction_text`).
ALLOWED_COMM_STYLES: frozenset[str] = frozenset({"data_driven", "narrative", "consensus", "directive"})

# Allowed stances. MUST stay aligned with stakeholder_reactions.ALLOWED_STANCES
# so reactions can be seeded directly from the persona's stance.
ALLOWED_STANCES: frozenset[str] = frozenset({"supportive", "neutral", "resistant"})

# Sanity ceilings.
MAX_PERSONAS = 20
MAX_CONCERNS_PER_PERSONA = 5
MAX_CONCERN_CHARS = 80
MAX_RESPONSE_DELAY_SPRINTS = 20

# Session-state key. Downstream simulation tools read this back.
STATE_KEY = "stakeholder_personas"

# Source-of-truth for stakeholder ids: the previously-emitted graph
# stashed at this state key by ``emit_stakeholder_graph``.
GRAPH_STATE_KEY = "stakeholder_graph"


def _err(message: str, *, field: str = "personas_json") -> dict[str, Any]:
    """Build a structured error response."""
    return {
        "status": "error",
        "field": field,
        "message": message,
    }


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a structured success response."""
    return {
        "status": "success",
        "stakeholder_personas": payload,
    }


def _validate_float_range(idx: int, field: str, value: Any, lo: float, hi: float) -> tuple[float | None, str | None]:
    """Validate a numeric field falls in [lo, hi]. Accept int as float."""
    if isinstance(value, str):
        try:
            value = float(value)
        except (ValueError, TypeError):
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"personas[{idx}].{field} must be a number in [{lo}, {hi}]"
    v = float(value)
    if v < lo or v > hi:
        return None, f"personas[{idx}].{field}={v} outside [{lo}, {hi}]"
    return v, None


def _validate_concerns(idx: int, raw: Any) -> tuple[list[str] | None, str | None]:
    """Validate a primary_concerns list. Returns (normalized, error)."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, f"personas[{idx}].primary_concerns must be a list"
    if len(raw) > MAX_CONCERNS_PER_PERSONA:
        return None, (f"personas[{idx}].primary_concerns has {len(raw)} entries (max {MAX_CONCERNS_PER_PERSONA})")
    out: list[str] = []
    for j, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            return None, (f"personas[{idx}].primary_concerns[{j}] must be a non-empty string")
        s = item.strip()
        if len(s) > MAX_CONCERN_CHARS:
            return None, (f"personas[{idx}].primary_concerns[{j}] length {len(s)} > {MAX_CONCERN_CHARS} chars")
        out.append(s)
    return out, None


def _validate_persona(idx: int, persona: Any, known_ids: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a single persona entry. Returns (normalized, error)."""
    if not isinstance(persona, dict):
        return None, f"personas[{idx}] must be an object"

    sid = persona.get("stakeholder_id")
    archetype = persona.get("archetype")
    comm_style = persona.get("communication_style")
    stance = persona.get("stance")
    sentiment_bias = persona.get("sentiment_bias")
    influence_weight = persona.get("influence_weight")
    response_delay = persona.get("response_delay_sprints", 0)
    inferred = persona.get("inferred", True)

    if not isinstance(sid, str) or not sid.strip():
        return None, f"personas[{idx}].stakeholder_id must be a non-empty string"
    sid_clean = sid.strip()
    if known_ids and sid_clean not in known_ids:
        return None, (
            f"personas[{idx}].stakeholder_id={sid_clean!r} does not match any "
            f"node id from the Step-0 stakeholder_graph; call emit_stakeholder_graph "
            "first or fix the id"
        )

    if archetype not in ALLOWED_ARCHETYPES:
        return None, (f"personas[{idx}].archetype={archetype!r} not in {sorted(ALLOWED_ARCHETYPES)}")
    if comm_style not in ALLOWED_COMM_STYLES:
        return None, (f"personas[{idx}].communication_style={comm_style!r} not in {sorted(ALLOWED_COMM_STYLES)}")
    if stance not in ALLOWED_STANCES:
        return None, (f"personas[{idx}].stance={stance!r} not in {sorted(ALLOWED_STANCES)}")

    sb, err = _validate_float_range(idx, "sentiment_bias", sentiment_bias, -1.0, 1.0)
    if err:
        return None, err
    iw, err = _validate_float_range(idx, "influence_weight", influence_weight, 0.0, 1.0)
    if err:
        return None, err

    if isinstance(response_delay, bool) or not isinstance(response_delay, int):
        return None, (f"personas[{idx}].response_delay_sprints must be a non-negative integer")
    if response_delay < 0 or response_delay > MAX_RESPONSE_DELAY_SPRINTS:
        return None, (
            f"personas[{idx}].response_delay_sprints={response_delay} outside [0, {MAX_RESPONSE_DELAY_SPRINTS}]"
        )

    concerns, err = _validate_concerns(idx, persona.get("primary_concerns"))
    if err:
        return None, err
    assert concerns is not None

    if not isinstance(inferred, bool):
        return None, f"personas[{idx}].inferred must be a boolean"

    return {
        "stakeholder_id": sid_clean,
        "archetype": archetype,
        "communication_style": comm_style,
        "stance": stance,
        "sentiment_bias": sb,
        "influence_weight": iw,
        "response_delay_sprints": response_delay,
        "primary_concerns": concerns,
        "inferred": inferred,
    }, None


async def emit_stakeholder_personas(
    personas_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate a stakeholder-personas JSON payload and stash it in session state.

    This is **Step 0.25** of the workflow, sitting between the Step-0
    stakeholder graph and the Step-0.5 stakeholder reactions. It produces
    one persona per Actor/Group node — the simulation-layer model that
    downstream multi-tick simulation (Artifact 2) will use to drive
    contagion math.

    Args:
        personas_json: A JSON string with shape
            ``{"personas": [
                {"stakeholder_id": "ceo",
                 "archetype": "visionary",
                 "communication_style": "directive",
                 "stance": "supportive",
                 "sentiment_bias": 0.5,
                 "influence_weight": 0.9,
                 "response_delay_sprints": 0,
                 "primary_concerns": ["board optics", "Q3 revenue"],
                 "inferred"?: bool (default true)},
                ...],
               "rationale"?: "one-sentence why these personas"}``.
            Personas are invented synthetic models belonging to the
            simulation layer.
        tool_context: ADK tool context. The validated payload is stored
            at ``tool_context.state["stakeholder_personas"]``.

    Returns:
        On success: ``{"status": "success", "stakeholder_personas": {...}}``.
        On failure: ``{"status": "error", "field": "...", "message": "..."}``.
    """
    # 1. Parse the JSON envelope (lenient: accept a dict directly).
    if isinstance(personas_json, dict):
        data = personas_json
    else:
        try:
            data = json.loads(personas_json)
        except (TypeError, ValueError) as exc:
            return _err(f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        return _err("payload must be a JSON object")

    # 2. Validate top-level shape.
    personas_raw = data.get("personas")
    if not isinstance(personas_raw, list) or not personas_raw:
        return _err("personas must be a non-empty array")
    if len(personas_raw) > MAX_PERSONAS:
        return _err(f"too many personas ({len(personas_raw)} > {MAX_PERSONAS}); one persona per Actor/Group node")

    # 3. Resolve known stakeholder ids from the previously-emitted graph.
    #    Permissive fallback: if no graph in state, accept any id (lets
    #    tests run standalone, mirrors the reactions-tool behavior).
    known_ids: set[str] = set()
    try:
        graph = tool_context.state.get(GRAPH_STATE_KEY)
    except (AttributeError, TypeError):
        graph = None
    if isinstance(graph, dict):
        nodes = graph.get("nodes")
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict):
                    nid = n.get("id")
                    if isinstance(nid, str) and nid.strip():
                        known_ids.add(nid.strip())

    # 4. Validate each persona; reject duplicate stakeholder_ids
    #    (one persona per stakeholder, full stop).
    personas: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    for i, raw in enumerate(personas_raw):
        normalized, err = _validate_persona(i, raw, known_ids)
        if err:
            return _err(err, field="personas")
        assert normalized is not None
        sid = normalized["stakeholder_id"]
        if sid in seen_sids:
            return _err(
                f"personas[{i}].stakeholder_id={sid!r} duplicates an earlier persona (one persona per stakeholder)",
                field="personas",
            )
        seen_sids.add(sid)
        personas.append(normalized)

    # 5. Optional rationale.
    rationale = data.get("rationale")
    rationale_clean: str | None = None
    if isinstance(rationale, str) and rationale.strip():
        rationale_clean = rationale.strip()[:500]

    payload: dict[str, Any] = {"personas": personas}
    if rationale_clean:
        payload["rationale"] = rationale_clean

    # 6. Stash for downstream simulation tools.
    try:
        tool_context.state[STATE_KEY] = payload
    except (AttributeError, TypeError):
        logger.warning(
            "emit_stakeholder_personas: could not write %s to session state",
            STATE_KEY,
        )

    logger.info(
        "emit_stakeholder_personas: %d personas, rationale=%s, graph_ids_known=%d",
        len(personas),
        "yes" if rationale_clean else "no",
        len(known_ids),
    )
    return _ok(payload)
