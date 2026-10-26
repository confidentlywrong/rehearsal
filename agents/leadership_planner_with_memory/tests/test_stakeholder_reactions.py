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

"""Tests for the regime-B stakeholder-reactions extraction tool."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory.stakeholder_reactions import (
    GRAPH_STATE_KEY,
    MAX_REACTIONS,
    MAX_REACTION_TEXT_CHARS,
    STATE_KEY,
    emit_stakeholder_reactions,
)


def _graph_state() -> dict:
    """A minimal valid graph that the reactions tool will reference."""
    return {
        "nodes": [
            {"id": "ceo", "label": "CEO", "kind": "Actor"},
            {"id": "mid_mgmt", "label": "Mid-management", "kind": "Group"},
            {"id": "legal", "label": "Legal", "kind": "Group"},
        ],
        "edges": [],
    }


def _ctx(with_graph: bool = True) -> SimpleNamespace:
    state: dict = {}
    if with_graph:
        state[GRAPH_STATE_KEY] = _graph_state()
    return SimpleNamespace(state=state)


def _valid_reactions_payload(**overrides) -> dict:
    payload: dict = {
        "reactions": [
            {
                "stakeholder_id": "ceo",
                "stance": "supportive",
                "reaction_text": "I back this — let's move fast and own the narrative.",
            },
            {
                "stakeholder_id": "mid_mgmt",
                "stance": "resistant",
                "reaction_text": "We have no bandwidth and no training plan. This will fail in week three.",
                "inferred": True,
            },
            {
                "stakeholder_id": "legal",
                "stance": "neutral",
                "reaction_text": "I need to see the data-handling SOPs before I can sign off.",
            },
        ],
        "rationale": "Plausible first-tick reactions to the AI rollout plan.",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
class TestEmitStakeholderReactions:
    async def test_happy_path_returns_validated_reactions(self):
        ctx = _ctx()
        result = await emit_stakeholder_reactions(json.dumps(_valid_reactions_payload()), ctx)

        assert result["status"] == "success", result
        payload = result["stakeholder_reactions"]
        assert len(payload["reactions"]) == 3
        assert payload["rationale"].startswith("Plausible first-tick")
        # `inferred` defaults to True when omitted.
        assert payload["reactions"][0]["inferred"] is True
        assert payload["reactions"][1]["inferred"] is True
        # State is stashed under the canonical key.
        assert ctx.state[STATE_KEY] == payload

    async def test_accepts_dict_payload_directly(self):
        """A model slip that passes a dict instead of a JSON string still works."""
        ctx = _ctx()
        result = await emit_stakeholder_reactions(_valid_reactions_payload(), ctx)  # type: ignore[arg-type]
        assert result["status"] == "success", result

    async def test_invalid_json_returns_error(self):
        ctx = _ctx()
        result = await emit_stakeholder_reactions("not-json", ctx)
        assert result["status"] == "error"
        assert "invalid JSON" in result["message"]
        assert STATE_KEY not in ctx.state

    async def test_empty_reactions_array_rejected(self):
        ctx = _ctx()
        result = await emit_stakeholder_reactions(json.dumps({"reactions": []}), ctx)
        assert result["status"] == "error"
        assert "non-empty array" in result["message"]
        assert STATE_KEY not in ctx.state

    async def test_unknown_stakeholder_id_rejected(self):
        ctx = _ctx()
        bad = _valid_reactions_payload()
        bad["reactions"][0]["stakeholder_id"] = "nonexistent_node"
        result = await emit_stakeholder_reactions(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "does not match any node id" in result["message"]
        assert STATE_KEY not in ctx.state

    async def test_unknown_id_allowed_when_no_graph_in_state(self):
        """Permissive fallback: no graph yet => accept any id."""
        ctx = _ctx(with_graph=False)
        result = await emit_stakeholder_reactions(json.dumps(_valid_reactions_payload()), ctx)
        assert result["status"] == "success", result
        assert ctx.state[STATE_KEY]["reactions"][0]["stakeholder_id"] == "ceo"

    async def test_invalid_stance_rejected(self):
        ctx = _ctx()
        bad = _valid_reactions_payload()
        bad["reactions"][0]["stance"] = "very supportive"
        result = await emit_stakeholder_reactions(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "stance" in result["message"]

    async def test_numeric_stance_rejected(self):
        """Refuse fabricated numeric sentiment scores in the schema."""
        ctx = _ctx()
        bad = _valid_reactions_payload()
        bad["reactions"][0]["stance"] = 0.42
        result = await emit_stakeholder_reactions(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "stance" in result["message"]

    async def test_reaction_text_length_capped(self):
        ctx = _ctx()
        bad = _valid_reactions_payload()
        bad["reactions"][0]["reaction_text"] = "x" * (MAX_REACTION_TEXT_CHARS + 1)
        result = await emit_stakeholder_reactions(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "tweet-length" in result["message"] or "280" in result["message"]

    async def test_empty_reaction_text_rejected(self):
        ctx = _ctx()
        bad = _valid_reactions_payload()
        bad["reactions"][0]["reaction_text"] = "   "
        result = await emit_stakeholder_reactions(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "reaction_text" in result["message"]

    async def test_duplicate_stakeholder_id_rejected(self):
        ctx = _ctx()
        bad = _valid_reactions_payload()
        bad["reactions"][1]["stakeholder_id"] = "ceo"  # duplicate
        result = await emit_stakeholder_reactions(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "duplicates" in result["message"]

    async def test_too_many_reactions_rejected(self):
        ctx = SimpleNamespace(state={})  # no graph, so any id is allowed
        many = {
            "reactions": [
                {
                    "stakeholder_id": f"node_{i}",
                    "stance": "neutral",
                    "reaction_text": "ok.",
                }
                for i in range(MAX_REACTIONS + 1)
            ]
        }
        result = await emit_stakeholder_reactions(json.dumps(many), ctx)
        assert result["status"] == "error"
        assert "too many reactions" in result["message"]

    async def test_inferred_defaults_to_true(self):
        ctx = _ctx()
        payload = _valid_reactions_payload()
        for r in payload["reactions"]:
            r.pop("inferred", None)
        result = await emit_stakeholder_reactions(json.dumps(payload), ctx)
        assert result["status"] == "success"
        for r in result["stakeholder_reactions"]["reactions"]:
            assert r["inferred"] is True

    async def test_inferred_false_preserved(self):
        """When the user prompt verbatim stated the stance, inferred=false stays."""
        ctx = _ctx()
        payload = _valid_reactions_payload()
        payload["reactions"][0]["inferred"] = False
        result = await emit_stakeholder_reactions(json.dumps(payload), ctx)
        assert result["status"] == "success"
        assert result["stakeholder_reactions"]["reactions"][0]["inferred"] is False

    async def test_non_bool_inferred_rejected(self):
        ctx = _ctx()
        bad = _valid_reactions_payload()
        bad["reactions"][0]["inferred"] = "yes"
        result = await emit_stakeholder_reactions(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "inferred" in result["message"]

    async def test_state_write_failure_does_not_crash(self):
        """If tool_context.state is not a mutable mapping, log+continue."""

        class _NoSetState:
            state = object()  # not a dict; setitem will raise

        result = await emit_stakeholder_reactions(
            json.dumps(_valid_reactions_payload()),
            _NoSetState(),  # type: ignore[arg-type]
        )
        # No graph in state => permissive mode; should still succeed.
        assert result["status"] == "success"
