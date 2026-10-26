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

"""Tests for the Step-0 stakeholder-graph extraction tool."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory import stakeholder_graph as sg
from agents.leadership_planner_with_memory.stakeholder_graph import (
    STATE_KEY,
    emit_stakeholder_graph,
)


def _ctx(state: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=state if state is not None else {})


def _valid_graph_payload(**overrides) -> dict:
    payload: dict = {
        "nodes": [
            {"id": "ceo", "label": "CEO", "kind": "Actor"},
            {"id": "mid_mgmt", "label": "Mid-management", "kind": "Group"},
            {"id": "skills_gap", "label": "Skills gap", "kind": "Friction"},
        ],
        "edges": [
            {"from": "ceo", "to": "mid_mgmt", "label": "directs"},
            {"from": "mid_mgmt", "to": "skills_gap", "label": "blocked by"},
        ],
        "rationale": "Compact graph for a vanilla transformation prompt.",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
class TestEmitStakeholderGraph:
    async def test_happy_path_returns_validated_graph(self):
        ctx = _ctx()
        result = await emit_stakeholder_graph(json.dumps(_valid_graph_payload()), ctx)

        assert result["status"] == "success"
        graph = result["stakeholder_graph"]
        assert len(graph["nodes"]) == 3
        assert len(graph["edges"]) == 2
        assert graph["rationale"].startswith("Compact graph")
        assert ctx.state[STATE_KEY] == graph

    async def test_accepts_dict_payload_directly(self):
        """A model slip that passes a dict instead of a JSON string still works."""
        ctx = _ctx()
        result = await emit_stakeholder_graph(_valid_graph_payload(), ctx)  # type: ignore[arg-type]
        assert result["status"] == "success"

    async def test_invalid_json_returns_error(self):
        ctx = _ctx()
        result = await emit_stakeholder_graph("not-json", ctx)
        assert result["status"] == "error"
        assert "invalid JSON" in result["message"]
        assert STATE_KEY not in ctx.state

    async def test_empty_nodes_rejected(self):
        ctx = _ctx()
        result = await emit_stakeholder_graph(json.dumps({"nodes": [], "edges": []}), ctx)
        assert result["status"] == "error"
        assert "non-empty" in result["message"]

    async def test_invalid_node_kind_rejected(self):
        ctx = _ctx()
        payload = _valid_graph_payload()
        payload["nodes"][0]["kind"] = "Wizard"
        result = await emit_stakeholder_graph(json.dumps(payload), ctx)
        assert result["status"] == "error"
        assert "Wizard" in result["message"]

    async def test_missing_node_label_rejected(self):
        ctx = _ctx()
        payload = _valid_graph_payload()
        payload["nodes"][0]["label"] = ""
        result = await emit_stakeholder_graph(json.dumps(payload), ctx)
        assert result["status"] == "error"
        assert "label" in result["message"]

    async def test_duplicate_node_id_rejected(self):
        ctx = _ctx()
        payload = _valid_graph_payload()
        payload["nodes"].append({"id": "ceo", "label": "CEO duplicate", "kind": "Actor"})
        result = await emit_stakeholder_graph(json.dumps(payload), ctx)
        assert result["status"] == "error"
        assert "duplicates" in result["message"]

    async def test_edge_referencing_unknown_node_rejected(self):
        ctx = _ctx()
        payload = _valid_graph_payload()
        payload["edges"].append({"from": "ceo", "to": "ghost", "label": "haunts"})
        result = await emit_stakeholder_graph(json.dumps(payload), ctx)
        assert result["status"] == "error"
        assert "ghost" in result["message"]

    async def test_self_loop_rejected(self):
        ctx = _ctx()
        payload = _valid_graph_payload()
        payload["edges"].append({"from": "ceo", "to": "ceo", "label": "self-talks"})
        result = await emit_stakeholder_graph(json.dumps(payload), ctx)
        assert result["status"] == "error"
        assert "self-loop" in result["message"]

    async def test_too_many_nodes_rejected(self):
        ctx = _ctx()
        nodes = [{"id": f"n{i}", "label": f"Node {i}", "kind": "Actor"} for i in range(sg.MAX_NODES + 1)]
        result = await emit_stakeholder_graph(json.dumps({"nodes": nodes, "edges": []}), ctx)
        assert result["status"] == "error"
        assert "too many nodes" in result["message"]

    async def test_optional_note_and_governing_belief_preserved(self):
        ctx = _ctx()
        payload = {
            "nodes": [
                {
                    "id": "ai_fear",
                    "label": "Job-loss fear",
                    "kind": "Belief",
                    "note": "Surveys show 62% concerned",
                    "governing": True,
                },
                {"id": "workforce", "label": "Workforce", "kind": "Group"},
            ],
            "edges": [{"from": "ai_fear", "to": "workforce", "label": "destabilizes"}],
        }
        result = await emit_stakeholder_graph(json.dumps(payload), ctx)
        assert result["status"] == "success"
        belief = result["stakeholder_graph"]["nodes"][0]
        assert belief["note"] == "Surveys show 62% concerned"
        assert belief["governing"] is True

    async def test_governing_flag_dropped_for_non_belief_nodes(self):
        ctx = _ctx()
        payload = _valid_graph_payload()
        payload["nodes"][0]["governing"] = True  # Actor, not Belief
        result = await emit_stakeholder_graph(json.dumps(payload), ctx)
        assert result["status"] == "success"
        assert "governing" not in result["stakeholder_graph"]["nodes"][0]

    async def test_survives_missing_state_attribute(self):
        """If tool_context has no usable state mapping, succeed anyway."""
        ctx = SimpleNamespace()  # no .state attribute at all
        result = await emit_stakeholder_graph(json.dumps(_valid_graph_payload()), ctx)
        assert result["status"] == "success"
