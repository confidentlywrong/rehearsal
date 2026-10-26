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

"""Stakeholder-reactions extraction tool (regime B — single tick).

This is the **synthetic-utterance** layer of the leadership_planner_with_memory
workflow, modelled after MiroFish's "tweet stream → report" pattern but
adapted to organizational discourse: each stakeholder node from the
Step-0 graph emits ONE short reaction-statement to the proposed plan.

Design choices (locked 2026-06-27 by the operator):

* **Single tick (regime B).** No contagion, no propagation across the
  graph yet — that's regime C. One reaction per stakeholder, period.
* **No persistence.** Reactions live in ``tool_context.state`` and
  stream out via the tool_end payload. There is **no** Neo4j /
  Postgres / file write. This is the explicit GCP-friendly choice:
  no graph database required at any deployment tier.
* **Invented data is allowed here.** This tool belongs to the
  ``scenario`` / simulation layer, not the analytic graph. Each
  reaction carries an ``inferred`` flag (defaults to True) so the UI
  can clearly mark synthetic utterances. This honors the refined rule:
  no invention in the analytic graph, invention is the *product* of
  the simulation layer.
* **The agent IS the simulator.** No nested LLM call. The agent reads
  the prompt + the previously-emitted graph + any draft plan and
  supplies the reactions directly. Same lightweight pattern as
  ``emit_stakeholder_graph``.
* **Tight schema.** Stance is a 3-value enum
  (``supportive | neutral | resistant``); no free-text stances and
  no fabricated numeric scores. Each reaction_text is tweet-length
  (≤ 280 chars), in the stakeholder's voice, citing concrete concerns.

Downstream consumers (Step C / report) will aggregate these into a
"who held, who flipped, what arguments won" report — that work is
deferred until this slice is validated end-to-end.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# Allowed stance values. Mirrors the 3-band quantization used in the
# UI legend; we deliberately refuse floats and free-text to keep the
# "no fabricated numeric scores" hard-rule intact in the analytic layer.
ALLOWED_STANCES: frozenset[str] = frozenset({"supportive", "neutral", "resistant"})

# Sanity ceilings. The graph caps at 20 nodes, so one reaction per
# stakeholder lands at the same ceiling.
MAX_REACTIONS = 20

# Tweet-length cap — MiroFish lineage, also keeps the UI cards legible.
MAX_REACTION_TEXT_CHARS = 280

# Session-state key. Downstream report tools will read this back; the
# frontend reads the tool_end payload directly.
STATE_KEY = "stakeholder_reactions"

# Source-of-truth for stakeholder ids: the previously-emitted graph
# stashed at this state key by ``emit_stakeholder_graph``.
GRAPH_STATE_KEY = "stakeholder_graph"


def _err(message: str, *, field: str = "reactions_json") -> dict[str, Any]:
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
        "stakeholder_reactions": payload,
    }


def _validate_reaction(idx: int, reaction: Any, known_ids: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a single reaction entry. Returns (normalized, error)."""
    if not isinstance(reaction, dict):
        return None, f"reactions[{idx}] must be an object"

    sid = reaction.get("stakeholder_id")
    stance = reaction.get("stance")
    text = reaction.get("reaction_text")
    inferred = reaction.get("inferred", True)

    if not isinstance(sid, str) or not sid.strip():
        return None, f"reactions[{idx}].stakeholder_id must be a non-empty string"
    sid_clean = sid.strip()
    if known_ids and sid_clean not in known_ids:
        return None, (
            f"reactions[{idx}].stakeholder_id={sid_clean!r} does not match any "
            f"node id from the Step-0 stakeholder_graph; call emit_stakeholder_graph "
            "first or fix the id"
        )
    if stance not in ALLOWED_STANCES:
        return None, (f"reactions[{idx}].stance={stance!r} not in {sorted(ALLOWED_STANCES)}")
    if not isinstance(text, str) or not text.strip():
        return None, f"reactions[{idx}].reaction_text must be a non-empty string"
    text_clean = text.strip()
    if len(text_clean) > MAX_REACTION_TEXT_CHARS:
        return None, (
            f"reactions[{idx}].reaction_text length {len(text_clean)} > "
            f"{MAX_REACTION_TEXT_CHARS} chars (keep it tweet-length)"
        )
    if not isinstance(inferred, bool):
        return None, f"reactions[{idx}].inferred must be a boolean"

    return {
        "stakeholder_id": sid_clean,
        "stance": stance,
        "reaction_text": text_clean,
        "inferred": inferred,
    }, None


async def emit_stakeholder_reactions(
    reactions_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate a stakeholder-reactions JSON payload and stash it in session state.

    This is the **regime-B simulation tick**: each stakeholder from the
    Step-0 graph emits ONE short reaction-statement to the proposed
    plan, in their own voice, tagged with a stance. The agent must
    call ``emit_stakeholder_graph`` first so the stakeholder ids resolve.

    Args:
        reactions_json: A JSON string with shape
            ``{"reactions": [
                {"stakeholder_id": "ceo",
                 "stance": "supportive" | "neutral" | "resistant",
                 "reaction_text": "Quote, <=280 chars, in stakeholder voice",
                 "inferred"?: bool (default true)},
                ...],
               "rationale"?: "one-sentence why these reactions"}``.
            Reactions are invented synthetic utterances — that is the
            point of the simulation layer. The ``inferred`` flag should
            be ``true`` for all reactions UNLESS the user prompt
            explicitly described the stakeholder's stance verbatim.
        tool_context: ADK tool context. The validated payload is stored
            at ``tool_context.state["stakeholder_reactions"]``.

    Returns:
        On success: ``{"status": "success", "stakeholder_reactions": {...}}``.
        On failure: ``{"status": "error", "field": "...", "message": "..."}``.
    """
    # 1. Parse the JSON envelope (lenient: also accept a dict directly).
    if isinstance(reactions_json, dict):
        data = reactions_json
    else:
        try:
            data = json.loads(reactions_json)
        except (TypeError, ValueError) as exc:
            return _err(f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        return _err("payload must be a JSON object")

    # 2. Validate top-level shape.
    reactions_raw = data.get("reactions")
    if not isinstance(reactions_raw, list) or not reactions_raw:
        return _err("reactions must be a non-empty array")
    if len(reactions_raw) > MAX_REACTIONS:
        return _err(f"too many reactions ({len(reactions_raw)} > {MAX_REACTIONS}); one reaction per stakeholder")

    # 3. Resolve known stakeholder ids from the previously-emitted graph.
    #    If the graph is not yet in state, fall back to a permissive
    #    mode (empty known_ids set => skip the id-match check). The
    #    agent prompt enforces the ordering; this fallback keeps tests
    #    and edge cases from hard-failing.
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

    # 4. Validate each reaction; reject duplicate stakeholder_ids
    #    (regime B = one reaction per stakeholder).
    reactions: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    for i, raw in enumerate(reactions_raw):
        normalized, err = _validate_reaction(i, raw, known_ids)
        if err:
            return _err(err, field="reactions")
        assert normalized is not None
        sid = normalized["stakeholder_id"]
        if sid in seen_sids:
            return _err(
                f"reactions[{i}].stakeholder_id={sid!r} duplicates an "
                "earlier reaction (regime B = one reaction per stakeholder)",
                field="reactions",
            )
        seen_sids.add(sid)
        reactions.append(normalized)

    # 5. Optional rationale.
    rationale = data.get("rationale")
    rationale_clean: str | None = None
    if isinstance(rationale, str) and rationale.strip():
        rationale_clean = rationale.strip()[:500]

    payload: dict[str, Any] = {"reactions": reactions}
    if rationale_clean:
        payload["rationale"] = rationale_clean

    # 6. Stash for downstream report tools (optional consumers).
    try:
        tool_context.state[STATE_KEY] = payload
    except (AttributeError, TypeError):
        logger.warning(
            "emit_stakeholder_reactions: could not write %s to session state",
            STATE_KEY,
        )

    logger.info(
        "emit_stakeholder_reactions: %d reactions, rationale=%s, graph_ids_known=%d",
        len(reactions),
        "yes" if rationale_clean else "no",
        len(known_ids),
    )
    return _ok(payload)
