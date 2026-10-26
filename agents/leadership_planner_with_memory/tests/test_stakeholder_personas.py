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

"""Tests for the Artifact-1 stakeholder-personas extraction tool."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory.stakeholder_personas import (
    GRAPH_STATE_KEY,
    MAX_CONCERN_CHARS,
    MAX_CONCERNS_PER_PERSONA,
    MAX_PERSONAS,
    MAX_RESPONSE_DELAY_SPRINTS,
    STATE_KEY,
    emit_stakeholder_personas,
)


def _graph_state() -> dict:
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


def _valid_personas_payload(**overrides) -> dict:
    payload: dict = {
        "personas": [
            {
                "stakeholder_id": "ceo",
                "archetype": "visionary",
                "communication_style": "directive",
                "stance": "supportive",
                "sentiment_bias": 0.5,
                "influence_weight": 0.9,
                "response_delay_sprints": 0,
                "primary_concerns": ["board optics", "Q3 revenue"],
            },
            {
                "stakeholder_id": "mid_mgmt",
                "archetype": "pragmatist",
                "communication_style": "data_driven",
                "stance": "resistant",
                "sentiment_bias": -0.4,
                "influence_weight": 0.6,
                "response_delay_sprints": 2,
                "primary_concerns": ["bandwidth", "training", "career impact"],
                "inferred": True,
            },
            {
                "stakeholder_id": "legal",
                "archetype": "guardian",
                "communication_style": "consensus",
                "stance": "neutral",
                "sentiment_bias": -0.1,
                "influence_weight": 0.4,
                "response_delay_sprints": 1,
                "primary_concerns": ["regulatory exposure"],
            },
        ],
        "rationale": "Three personas anchoring the AI-rollout stress test.",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
class TestEmitStakeholderPersonas:
    async def test_happy_path_returns_validated_personas(self):
        ctx = _ctx()
        result = await emit_stakeholder_personas(json.dumps(_valid_personas_payload()), ctx)

        assert result["status"] == "success", result
        payload = result["stakeholder_personas"]
        assert len(payload["personas"]) == 3
        assert payload["rationale"].startswith("Three personas")
        # `inferred` defaults to True when omitted.
        assert payload["personas"][0]["inferred"] is True
        assert payload["personas"][1]["inferred"] is True
        # State is stashed under the canonical key.
        assert ctx.state[STATE_KEY] == payload

    async def test_accepts_dict_payload_directly(self):
        ctx = _ctx()
        result = await emit_stakeholder_personas(_valid_personas_payload(), ctx)  # type: ignore[arg-type]
        assert result["status"] == "success", result

    async def test_invalid_json_returns_error(self):
        ctx = _ctx()
        result = await emit_stakeholder_personas("not-json", ctx)
        assert result["status"] == "error"
        assert "invalid JSON" in result["message"]
        assert STATE_KEY not in ctx.state

    async def test_empty_personas_array_rejected(self):
        ctx = _ctx()
        result = await emit_stakeholder_personas(json.dumps({"personas": []}), ctx)
        assert result["status"] == "error"
        assert "non-empty array" in result["message"]

    async def test_unknown_stakeholder_id_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["stakeholder_id"] = "nonexistent_node"
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "does not match any node id" in result["message"]

    async def test_unknown_id_allowed_when_no_graph_in_state(self):
        ctx = _ctx(with_graph=False)
        result = await emit_stakeholder_personas(json.dumps(_valid_personas_payload()), ctx)
        assert result["status"] == "success", result

    async def test_invalid_archetype_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["archetype"] = "saboteur"
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "archetype" in result["message"]

    async def test_invalid_communication_style_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["communication_style"] = "screaming"
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "communication_style" in result["message"]

    async def test_invalid_stance_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["stance"] = "very supportive"
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "stance" in result["message"]

    async def test_sentiment_bias_out_of_range_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["sentiment_bias"] = 1.5
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "sentiment_bias" in result["message"]

    async def test_sentiment_bias_non_numeric_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["sentiment_bias"] = "high"
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "sentiment_bias" in result["message"]

    async def test_influence_weight_out_of_range_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["influence_weight"] = -0.1
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "influence_weight" in result["message"]

    async def test_response_delay_negative_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["response_delay_sprints"] = -1
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "response_delay_sprints" in result["message"]

    async def test_response_delay_too_large_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["response_delay_sprints"] = MAX_RESPONSE_DELAY_SPRINTS + 1
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "response_delay_sprints" in result["message"]

    async def test_response_delay_non_int_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["response_delay_sprints"] = 1.5
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "response_delay_sprints" in result["message"]

    async def test_response_delay_defaults_to_zero(self):
        ctx = _ctx()
        payload = _valid_personas_payload()
        for p in payload["personas"]:
            p.pop("response_delay_sprints", None)
        result = await emit_stakeholder_personas(json.dumps(payload), ctx)
        assert result["status"] == "success"
        for p in result["stakeholder_personas"]["personas"]:
            assert p["response_delay_sprints"] == 0

    async def test_primary_concerns_optional(self):
        ctx = _ctx()
        payload = _valid_personas_payload()
        for p in payload["personas"]:
            p.pop("primary_concerns", None)
        result = await emit_stakeholder_personas(json.dumps(payload), ctx)
        assert result["status"] == "success"
        for p in result["stakeholder_personas"]["personas"]:
            assert p["primary_concerns"] == []

    async def test_too_many_concerns_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["primary_concerns"] = ["c"] * (MAX_CONCERNS_PER_PERSONA + 1)
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "primary_concerns" in result["message"]

    async def test_concern_too_long_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["primary_concerns"] = ["x" * (MAX_CONCERN_CHARS + 1)]
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "primary_concerns" in result["message"]

    async def test_duplicate_stakeholder_id_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][1]["stakeholder_id"] = "ceo"
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "duplicates" in result["message"]

    async def test_too_many_personas_rejected(self):
        ctx = SimpleNamespace(state={})
        many = {
            "personas": [
                {
                    "stakeholder_id": f"node_{i}",
                    "archetype": "pragmatist",
                    "communication_style": "data_driven",
                    "stance": "neutral",
                    "sentiment_bias": 0.0,
                    "influence_weight": 0.3,
                }
                for i in range(MAX_PERSONAS + 1)
            ]
        }
        result = await emit_stakeholder_personas(json.dumps(many), ctx)
        assert result["status"] == "error"
        assert "too many personas" in result["message"]

    async def test_inferred_false_preserved(self):
        ctx = _ctx()
        payload = _valid_personas_payload()
        payload["personas"][0]["inferred"] = False
        result = await emit_stakeholder_personas(json.dumps(payload), ctx)
        assert result["status"] == "success"
        assert result["stakeholder_personas"]["personas"][0]["inferred"] is False

    async def test_non_bool_inferred_rejected(self):
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["inferred"] = "yes"
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "inferred" in result["message"]

    async def test_state_write_failure_does_not_crash(self):
        class _NoSetState:
            state = object()

        result = await emit_stakeholder_personas(
            json.dumps(_valid_personas_payload()),
            _NoSetState(),  # type: ignore[arg-type]
        )
        assert result["status"] == "success"

    async def test_int_sentiment_bias_accepted(self):
        """`0` and `1` as ints should be accepted for sentiment_bias and influence_weight."""
        ctx = _ctx()
        payload = _valid_personas_payload()
        payload["personas"][0]["sentiment_bias"] = 0
        payload["personas"][0]["influence_weight"] = 1
        result = await emit_stakeholder_personas(json.dumps(payload), ctx)
        assert result["status"] == "success"
        assert result["stakeholder_personas"]["personas"][0]["sentiment_bias"] == 0.0
        assert result["stakeholder_personas"]["personas"][0]["influence_weight"] == 1.0

    async def test_bool_rejected_as_sentiment_bias(self):
        """A bare `True` should NOT be silently coerced to 1.0."""
        ctx = _ctx()
        bad = _valid_personas_payload()
        bad["personas"][0]["sentiment_bias"] = True
        result = await emit_stakeholder_personas(json.dumps(bad), ctx)
        assert result["status"] == "error"
        assert "sentiment_bias" in result["message"]
