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

"""Stakeholder-graph extraction tool.

This is **Step 0** of the leadership_planner_with_memory workflow. Before
the agent designs any scenario, it must analyze the user's transformation
prompt and emit a small relationship graph: the actors/groups/structures
the transformation touches and the relationships (influence, friction,
reporting) between them. The graph is rendered live in the right-side
agent chat panel of the keynote demo shell — it is the
"Organizational Memory & Learning" wow-moment.

Design choices (locked 2026-06-27 by the operator):

* **The agent IS the extractor.** No nested LLM call. The agent reasons
  about the prompt and supplies the graph directly. This tool only
  validates the shape and stashes it in session state — same lightweight
  pattern as ``validate_and_emit_a2ui``. Keeps the latency low enough
  for a live keynote demo and avoids a second model round-trip.
* **Tight schema.** Node kinds are restricted to a small enum
  (``Actor | Group | Structure | Friction | Belief``) so the frontend
  renderer can color them deterministically.
* **No data invention.** If the prompt does not warrant a node, the
  agent must not invent one. Empty graphs (zero nodes) are rejected so
  the agent cannot silently no-op the step.

Persistence to the /memory Postgres tables is deliberately deferred —
the operator's analyses workbench (Step 9-12 work) is a separate
multi-session story; wiring this Step-0 graph into it would conflate
the live simulation demo with backstage state.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

# Allowed node kinds. Mirrors a deliberate subset of internal/memory/labels.go
# — the backstage uses a richer Walsh-Ungson taxonomy but the live keynote
# only needs these five to make the visualization legible at a glance.
ALLOWED_KINDS: frozenset[str] = frozenset({"Actor", "Group", "Structure", "Friction", "Belief"})

# Sanity ceilings. The frontend SVG renderer becomes illegible above ~15
# nodes; the agent is instructed to stay in the 5-10 range.
MAX_NODES = 20
MAX_EDGES = 40

# Session-state key. The downstream scenario-design steps may read this
# back to seed the persona payload, but consumption is optional — Step 0
# is allowed to stand alone.
STATE_KEY = "stakeholder_graph"


def _err(message: str, *, field: str = "graph_json") -> dict[str, Any]:
    """Build a structured error response."""
    return {
        "status": "error",
        "field": field,
        "message": message,
    }


def _ok(graph: dict[str, Any]) -> dict[str, Any]:
    """Build a structured success response."""
    return {
        "status": "success",
        "stakeholder_graph": graph,
    }


def _validate_node(idx: int, node: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a single node entry. Returns (normalized_node, error)."""
    if not isinstance(node, dict):
        return None, f"nodes[{idx}] must be an object"
    node_id = node.get("id")
    if isinstance(node_id, str):
        node_id = node_id.lower()
    label = node.get("label")
    kind = node.get("kind")
    if not isinstance(node_id, str) or not node_id.strip():
        return None, f"nodes[{idx}].id must be a non-empty string"
    if not isinstance(label, str) or not label.strip():
        return None, f"nodes[{idx}].label must be a non-empty string"
    if kind not in ALLOWED_KINDS:
        return None, (f"nodes[{idx}].kind={kind!r} not in {sorted(ALLOWED_KINDS)}")
    # Optional fields: note (short rationale), governing (bool flag for
    # Belief nodes only). Anything else is dropped silently.
    normalized: dict[str, Any] = {
        "id": node_id.strip(),
        "label": label.strip(),
        "kind": kind,
    }
    note = node.get("note")
    if isinstance(note, str) and note.strip():
        normalized["note"] = note.strip()
    if kind == "Belief" and node.get("governing") is True:
        normalized["governing"] = True
    return normalized, None


def _validate_edge(idx: int, edge: Any, node_ids: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a single edge entry. Returns (normalized_edge, error)."""
    if not isinstance(edge, dict):
        return None, f"edges[{idx}] must be an object"
    src = edge.get("from")
    if isinstance(src, str):
        src = src.lower()
    dst = edge.get("to")
    if isinstance(dst, str):
        dst = dst.lower()
    label = edge.get("label")
    if not isinstance(src, str) or src not in node_ids:
        return None, f"edges[{idx}].from={src!r} does not reference a known node id"
    if not isinstance(dst, str) or dst not in node_ids:
        return None, f"edges[{idx}].to={dst!r} does not reference a known node id"
    if src == dst:
        return None, f"edges[{idx}] is a self-loop (from==to=={src!r})"
    if not isinstance(label, str) or not label.strip():
        return None, f"edges[{idx}].label must be a non-empty string"
    return {"from": src, "to": dst, "label": label.strip()}, None


async def emit_stakeholder_graph(
    graph_json: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Validate a stakeholder-graph JSON payload and stash it in session state.

    This is the **first** tool the leadership_planner_with_memory agent
    must call. The graph it emits powers the live visualization in the
    right-side agent chat panel of the keynote demo shell.

    Args:
        graph_json: A JSON string with shape
            ``{"nodes": [{"id": "ceo", "label": "CEO", "kind": "Actor", "note"?: "..."},
                          ...],
               "edges": [{"from": "ceo", "to": "cto", "label": "sponsors"},
                          ...],
               "rationale": "one-sentence why this graph"}``.
            ``kind`` must be one of ``Actor | Group | Structure | Friction | Belief``.
            ``rationale`` is optional but recommended — surfaced as
            sub-text under the graph in the chat panel.
        tool_context: ADK tool context. The validated graph is stored at
            ``tool_context.state["stakeholder_graph"]``.

    Returns:
        On success: ``{"status": "success", "stakeholder_graph": {...}}``.
            The chat panel keys off this exact tool_end payload to render
            the graph; do not reshape it.
        On failure: ``{"status": "error", "field": "...", "message": "..."}``.
            The agent should fix the violation and retry; partial graphs
            are not persisted.
    """
    # 1. Parse the JSON envelope. Allow a model slip where the agent
    #    passes a dict-like value instead of a string by being lenient
    #    here (mirrors validate_and_emit_a2ui).
    if isinstance(graph_json, dict):
        data = graph_json
    else:
        try:
            data = json.loads(graph_json)
        except (TypeError, ValueError) as exc:
            return _err(f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        return _err("payload must be a JSON object")

    # 2. Validate top-level shape.
    nodes_raw = data.get("nodes")
    edges_raw = data.get("edges", [])
    if not isinstance(nodes_raw, list) or not nodes_raw:
        return _err("nodes must be a non-empty array")
    if not isinstance(edges_raw, list):
        return _err("edges must be an array")
    if len(nodes_raw) > MAX_NODES:
        return _err(f"too many nodes ({len(nodes_raw)} > {MAX_NODES}); keep the keynote graph in the 5-10 range")
    if len(edges_raw) > MAX_EDGES:
        return _err(f"too many edges ({len(edges_raw)} > {MAX_EDGES})")

    # 3. Validate each node and collect ids. Reject duplicate ids — they
    #    would silently confuse the renderer's force-layout.
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(nodes_raw):
        normalized, err = _validate_node(i, raw)
        if err:
            return _err(err, field="nodes")
        assert normalized is not None  # type narrowing for mypy
        if normalized["id"] in seen_ids:
            return _err(f"nodes[{i}].id={normalized['id']!r} duplicates an earlier node id", field="nodes")
        seen_ids.add(normalized["id"])
        nodes.append(normalized)

    # 4. Validate edges against the collected node ids.
    edges: list[dict[str, Any]] = []
    for i, raw in enumerate(edges_raw):
        normalized, err = _validate_edge(i, raw, seen_ids)
        if err:
            return _err(err, field="edges")
        assert normalized is not None
        edges.append(normalized)

    # 5. Optional rationale (sub-text under the graph).
    rationale = data.get("rationale")
    rationale_clean: str | None = None
    if isinstance(rationale, str) and rationale.strip():
        rationale_clean = rationale.strip()[:500]

    graph: dict[str, Any] = {"nodes": nodes, "edges": edges}
    if rationale_clean:
        graph["rationale"] = rationale_clean

    # 6. Stash for downstream tools (optional consumers).
    try:
        tool_context.state[STATE_KEY] = graph
    except (AttributeError, TypeError):
        # In some test contexts tool_context may not expose a mutable
        # `state` mapping. Log + continue — the live keynote still works
        # because the frontend reads from the tool_end payload, not
        # session state.
        logger.warning("emit_stakeholder_graph: could not write %s to session state", STATE_KEY)

    logger.info(
        "emit_stakeholder_graph: %d nodes, %d edges, rationale=%s",
        len(nodes),
        len(edges),
        "yes" if rationale_clean else "no",
    )
    return _ok(graph)
