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

"""Tests for the Artifact-2 simulation-timeline tool."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory.simulation_timeline import (
    MAX_EVENT_CHARS,
    MAX_EVENTS_PER_TICK,
    MAX_HEADLINE_CHARS,
    MAX_NOTE_CHARS,
    MAX_TICKS,
    PERSONAS_STATE_KEY,
    STATE_KEY,
    emit_simulation_timeline,
)


def _personas_state() -> dict:
    """Stash three personas in state. Timeline must cover exactly these."""
    return {
        "personas": [
            {
                "stakeholder_id": "ceo",
                "archetype": "visionary",
                "communication_style": "directive",
                "stance": "supportive",
                "sentiment_bias": 0.5,
                "influence_weight": 0.9,
                "response_delay_sprints": 0,
                "primary_concerns": [],
                "inferred": True,
            },
            {
                "stakeholder_id": "mid_mgmt",
                "archetype": "pragmatist",
                "communication_style": "data_driven",
                "stance": "resistant",
                "sentiment_bias": -0.4,
                "influence_weight": 0.6,
                "response_delay_sprints": 2,
                "primary_concerns": [],
                "inferred": True,
            },
            {
                "stakeholder_id": "legal",
                "archetype": "guardian",
                "communication_style": "consensus",
                "stance": "neutral",
                "sentiment_bias": 0.0,
                "influence_weight": 0.5,
                "response_delay_sprints": 1,
                "primary_concerns": [],
                "inferred": True,
            },
        ]
    }


def _ctx(with_personas: bool = True) -> SimpleNamespace:
    state: dict = {}
    if with_personas:
        state[PERSONAS_STATE_KEY] = _personas_state()
    return SimpleNamespace(state=state)


def _tick(tick_num: int, headline: str = "Routine sprint.") -> dict:
    """Build a tick that covers all three default personas."""
    return {
        "tick": tick_num,
        "headline": headline,
        "states": [
            {"stakeholder_id": "ceo", "stance": "supportive", "sentiment": 0.5},
            {"stakeholder_id": "mid_mgmt", "stance": "resistant", "sentiment": -0.4},
            {"stakeholder_id": "legal", "stance": "neutral", "sentiment": 0.0},
        ],
    }


def _valid_payload(**overrides) -> dict:
    payload: dict = {
        "ticks": [_tick(1), _tick(2, "Mid-mgmt friction peaks."), _tick(3, "Stability returns.")],
        "rationale": "Three-sprint projection.",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_three_ticks() -> None:
    ctx = _ctx()
    result = await emit_simulation_timeline(json.dumps(_valid_payload()), ctx)

    assert result["status"] == "success"
    payload = result["simulation_timeline"]
    assert len(payload["ticks"]) == 3
    assert payload["ticks"][0]["tick"] == 1
    assert payload["ticks"][2]["tick"] == 3
    assert payload["rationale"] == "Three-sprint projection."
    assert ctx.state[STATE_KEY] == payload


@pytest.mark.asyncio
async def test_accepts_dict_payload() -> None:
    """Should accept a dict directly (not just a JSON string)."""
    ctx = _ctx()
    result = await emit_simulation_timeline(_valid_payload(), ctx)
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_sprint_label_and_events_preserved() -> None:
    ctx = _ctx()
    tick = _tick(1)
    tick["sprint_label"] = "Sprint 1 (week 2)"
    tick["events"] = ["Town hall held", "Email cascade"]
    result = await emit_simulation_timeline({"ticks": [tick]}, ctx)
    assert result["status"] == "success"
    out = result["simulation_timeline"]["ticks"][0]
    assert out["sprint_label"] == "Sprint 1 (week 2)"
    assert out["events"] == ["Town hall held", "Email cascade"]


@pytest.mark.asyncio
async def test_note_preserved_when_present() -> None:
    ctx = _ctx()
    tick = _tick(1)
    tick["states"][0]["note"] = "Reaffirms timeline at standup."
    result = await emit_simulation_timeline({"ticks": [tick]}, ctx)
    assert result["status"] == "success"
    assert result["simulation_timeline"]["ticks"][0]["states"][0]["note"] == ("Reaffirms timeline at standup.")


@pytest.mark.asyncio
async def test_inferred_defaults_true_and_preserved_when_false() -> None:
    ctx = _ctx()
    tick = _tick(1)
    tick["states"][0]["inferred"] = False
    result = await emit_simulation_timeline({"ticks": [tick]}, ctx)
    assert result["status"] == "success"
    states = result["simulation_timeline"]["ticks"][0]["states"]
    assert states[0]["inferred"] is False
    assert states[1]["inferred"] is True


# ---------------------------------------------------------------------------
# JSON / shape failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_string() -> None:
    result = await emit_simulation_timeline("{not json", _ctx())
    assert result["status"] == "error"
    assert "invalid JSON" in result["message"]


@pytest.mark.asyncio
async def test_payload_must_be_object() -> None:
    result = await emit_simulation_timeline(json.dumps([]), _ctx())
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_empty_ticks_rejected() -> None:
    result = await emit_simulation_timeline({"ticks": []}, _ctx())
    assert result["status"] == "error"
    assert "non-empty" in result["message"]


@pytest.mark.asyncio
async def test_too_many_ticks_rejected() -> None:
    ctx = _ctx()
    payload = {"ticks": [_tick(i + 1) for i in range(MAX_TICKS + 1)]}
    result = await emit_simulation_timeline(payload, ctx)
    assert result["status"] == "error"
    assert "outside" in result["message"]


@pytest.mark.asyncio
async def test_ticks_must_be_sequential() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][1]["tick"] = 5  # gap
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"
    assert "sequential" in result["message"]


@pytest.mark.asyncio
async def test_first_tick_must_be_one() -> None:
    ctx = _ctx()
    bad = {"ticks": [_tick(0)]}
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_tick_must_be_integer() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["tick"] = "1"
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"
    assert "integer" in result["message"]


@pytest.mark.asyncio
async def test_tick_bool_not_accepted_as_int() -> None:
    """True == 1 in Python; the tool must reject bool to avoid silent coercion."""
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["tick"] = True
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Tick-level field failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_headline_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    del bad["ticks"][0]["headline"]
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"
    assert "headline" in result["message"]


@pytest.mark.asyncio
async def test_headline_length_capped() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["headline"] = "x" * (MAX_HEADLINE_CHARS + 1)
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_sprint_label_length_capped() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["sprint_label"] = "x" * 41
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_too_many_events_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["events"] = ["e" + str(i) for i in range(MAX_EVENTS_PER_TICK + 1)]
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_event_too_long_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["events"] = ["x" * (MAX_EVENT_CHARS + 1)]
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_empty_states_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"] = []
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Per-state-row failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_stance_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"][0]["stance"] = "ambivalent"
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_sentiment_out_of_range_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"][0]["sentiment"] = 1.5
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_sentiment_bool_rejected() -> None:
    """True / False would silently coerce to 1.0 / 0.0 — reject."""
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"][0]["sentiment"] = True
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_sentiment_int_accepted_as_float() -> None:
    ctx = _ctx()
    payload = _valid_payload()
    payload["ticks"][0]["states"][0]["sentiment"] = 0
    result = await emit_simulation_timeline(payload, ctx)
    assert result["status"] == "success"
    assert result["simulation_timeline"]["ticks"][0]["states"][0]["sentiment"] == 0.0


@pytest.mark.asyncio
async def test_note_too_long_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"][0]["note"] = "x" * (MAX_NOTE_CHARS + 1)
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_unknown_stakeholder_rejected_when_personas_known() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"][0]["stakeholder_id"] = "ghost"
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"
    assert "ghost" in result["message"]


@pytest.mark.asyncio
async def test_missing_persona_in_tick_rejected() -> None:
    """When personas are known, every tick must cover all of them."""
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"] = bad["ticks"][0]["states"][:2]  # drop legal
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"
    assert "missing personas" in result["message"]


@pytest.mark.asyncio
async def test_duplicate_state_in_tick_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["ticks"][0]["states"].append({"stakeholder_id": "ceo", "stance": "neutral", "sentiment": 0.0})
    result = await emit_simulation_timeline(bad, ctx)
    assert result["status"] == "error"
    assert "duplicates" in result["message"]


# ---------------------------------------------------------------------------
# Permissive fallback (no personas in state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permissive_when_no_personas_in_state() -> None:
    """Without personas in state, any id set is acceptable as long as it is
    internally consistent across ticks."""
    ctx = _ctx(with_personas=False)
    payload = {
        "ticks": [
            {
                "tick": 1,
                "headline": "Day 1.",
                "states": [
                    {"stakeholder_id": "a", "stance": "supportive", "sentiment": 0.5},
                    {"stakeholder_id": "b", "stance": "neutral", "sentiment": 0.0},
                ],
            },
            {
                "tick": 2,
                "headline": "Day 2.",
                "states": [
                    {"stakeholder_id": "a", "stance": "supportive", "sentiment": 0.6},
                    {"stakeholder_id": "b", "stance": "neutral", "sentiment": 0.0},
                ],
            },
        ]
    }
    result = await emit_simulation_timeline(payload, ctx)
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# State-write resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_write_failure_does_not_crash() -> None:
    """If session state is unwritable, the tool should still return success
    (warning is logged). The agent's flow must not be blocked by state-bus
    issues."""

    class _ReadOnlyState:
        def get(self, k, default=None):
            return None

        def __setitem__(self, k, v):
            raise TypeError("read-only state")

    ctx = SimpleNamespace(state=_ReadOnlyState())
    result = await emit_simulation_timeline(_valid_payload(), ctx)
    assert result["status"] == "success"
