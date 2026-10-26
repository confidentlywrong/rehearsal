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

"""Tests for the Artifact-4 stakeholder-interview tool."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory.stakeholder_interview import (
    MAX_ANSWER_CHARS,
    MAX_EXCHANGES,
    MAX_FOLLOW_UP_CHARS,
    MAX_KEY_QUOTES,
    MAX_KEY_QUOTE_CHARS,
    MAX_QUESTION_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TOPIC_CHARS,
    PERSONAS_STATE_KEY,
    STATE_KEY,
    emit_stakeholder_interview,
)


def _personas_state() -> dict:
    return {
        "personas": [
            {"stakeholder_id": "ceo", "archetype": "visionary"},
            {"stakeholder_id": "mid_mgmt", "archetype": "pragmatist"},
            {"stakeholder_id": "legal", "archetype": "guardian"},
        ]
    }


def _ctx(with_personas: bool = True) -> SimpleNamespace:
    state: dict = {}
    if with_personas:
        state[PERSONAS_STATE_KEY] = _personas_state()
    return SimpleNamespace(state=state)


def _valid_payload(**overrides) -> dict:
    payload: dict = {
        "stakeholder_id": "mid_mgmt",
        "topic": "Bandwidth + training gaps surfaced in Sprint 1",
        "tone": "candid",
        "final_stance": "neutral",
        "exchanges": [
            {
                "question": "Why did you push back so hard in the Sprint-1 townhall?",
                "answer": (
                    "Look, my team is already running at 110%. The phased "
                    "rollout looked fine on paper, but nobody was asking who "
                    "trains my people while we keep the quarterly targets."
                ),
                "follow_up": "Was it the training plan or the targets that finally moved you?",
            },
            {
                "question": "What changed your mind by Sprint 3?",
                "answer": (
                    "Two things, in order. The target-relief letter from the "
                    "CFO. And the dedicated training budget. Either one alone "
                    "wouldn't have done it — I needed both to defend the "
                    "change to my own leads."
                ),
            },
        ],
        "key_quotes": [
            "I needed both — the relief AND the budget — to defend the change to my leads.",
            "Nobody was asking who trains my people while we keep the targets.",
        ],
        "summary": (
            "Mid-mgmt was never anti-rollout — they were anti-being-set-up-to-fail. "
            "The Sprint-3 concessions were necessary, not optional."
        ),
        "rationale": "Grounded in the timeline's Sprint-1 friction and Sprint-3 unlock.",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_interview() -> None:
    ctx = _ctx()
    result = await emit_stakeholder_interview(json.dumps(_valid_payload()), ctx)

    assert result["status"] == "success"
    iv = result["stakeholder_interview"]
    assert iv["stakeholder_id"] == "mid_mgmt"
    assert iv["topic"].startswith("Bandwidth")
    assert iv["tone"] == "candid"
    assert iv["final_stance"] == "neutral"
    assert len(iv["exchanges"]) == 2
    assert iv["exchanges"][0]["follow_up"]
    assert "follow_up" not in iv["exchanges"][1]
    assert len(iv["key_quotes"]) == 2
    assert iv["summary"]
    assert iv["rationale"]
    assert iv["inferred"] is True
    assert ctx.state[STATE_KEY] == iv


@pytest.mark.asyncio
async def test_accepts_dict_payload() -> None:
    result = await emit_stakeholder_interview(_valid_payload(), _ctx())
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_optional_sections_omitted_when_absent() -> None:
    ctx = _ctx()
    payload = _valid_payload()
    del payload["key_quotes"]
    del payload["summary"]
    del payload["rationale"]
    result = await emit_stakeholder_interview(payload, ctx)
    assert result["status"] == "success"
    iv = result["stakeholder_interview"]
    assert "key_quotes" not in iv
    assert "summary" not in iv
    assert "rationale" not in iv


@pytest.mark.asyncio
async def test_inferred_defaults_true_and_preserved_when_false() -> None:
    ctx = _ctx()
    payload = _valid_payload()
    payload["inferred"] = False
    result = await emit_stakeholder_interview(payload, ctx)
    assert result["status"] == "success"
    assert result["stakeholder_interview"]["inferred"] is False


@pytest.mark.asyncio
async def test_followup_dropped_when_empty_string() -> None:
    """Empty/whitespace follow_up should be quietly dropped, not rejected."""
    ctx = _ctx()
    payload = _valid_payload()
    payload["exchanges"][0]["follow_up"] = "   "
    result = await emit_stakeholder_interview(payload, ctx)
    assert result["status"] == "success"
    assert "follow_up" not in result["stakeholder_interview"]["exchanges"][0]


# ---------------------------------------------------------------------------
# JSON / top-level shape failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_string() -> None:
    result = await emit_stakeholder_interview("{not json", _ctx())
    assert result["status"] == "error"
    assert "invalid JSON" in result["message"]


@pytest.mark.asyncio
async def test_payload_must_be_object() -> None:
    result = await emit_stakeholder_interview(json.dumps([]), _ctx())
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_stakeholder_id_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    del bad["stakeholder_id"]
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "stakeholder_id"


@pytest.mark.asyncio
async def test_unknown_stakeholder_rejected_when_personas_known() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["stakeholder_id"] = "ghost"
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "stakeholder_id"
    assert "ghost" in result["message"]


@pytest.mark.asyncio
async def test_topic_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["topic"] = ""
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "topic"


@pytest.mark.asyncio
async def test_topic_too_long() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["topic"] = "x" * (MAX_TOPIC_CHARS + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_invalid_tone_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["tone"] = "passive_aggressive"
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "tone"


@pytest.mark.asyncio
async def test_invalid_final_stance_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["final_stance"] = "wavering"
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "final_stance"


# ---------------------------------------------------------------------------
# Exchanges failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchanges_must_be_array() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"] = {"q": "a"}
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_zero_exchanges_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"] = []
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "exchanges"


@pytest.mark.asyncio
async def test_too_many_exchanges_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    one = bad["exchanges"][0]
    bad["exchanges"] = [one] * (MAX_EXCHANGES + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_exchange_question_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"][0]["question"] = ""
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_exchange_question_too_long() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"][0]["question"] = "x" * (MAX_QUESTION_CHARS + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_exchange_answer_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"][0]["answer"] = ""
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_exchange_answer_too_long() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"][0]["answer"] = "x" * (MAX_ANSWER_CHARS + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_exchange_follow_up_too_long() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"][0]["follow_up"] = "x" * (MAX_FOLLOW_UP_CHARS + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_exchange_follow_up_wrong_type_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["exchanges"][0]["follow_up"] = 42
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Key quotes failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_quotes_must_be_array() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_quotes"] = "just one quote"
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_too_many_key_quotes_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_quotes"] = ["q"] * (MAX_KEY_QUOTES + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_key_quote_too_long() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_quotes"][0] = "x" * (MAX_KEY_QUOTE_CHARS + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_key_quote_empty_string_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_quotes"][0] = "  "
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Summary / rationale / inferred
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_too_long_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["summary"] = "x" * (MAX_SUMMARY_CHARS + 1)
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_summary_wrong_type_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["summary"] = ["not", "a", "string"]
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_inferred_non_bool_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["inferred"] = "yes"
    result = await emit_stakeholder_interview(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "inferred"


# ---------------------------------------------------------------------------
# Permissive fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permissive_when_no_personas_in_state() -> None:
    """Without personas in state, any stakeholder_id is accepted."""
    ctx = _ctx(with_personas=False)
    payload = _valid_payload()
    payload["stakeholder_id"] = "anything_goes"
    result = await emit_stakeholder_interview(payload, ctx)
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# State-write resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_write_failure_does_not_crash() -> None:
    """If session state is unwritable, the tool should still return success."""

    class _ReadOnlyState:
        def get(self, k, default=None):
            return None

        def __setitem__(self, k, v):
            raise TypeError("read-only state")

    ctx = SimpleNamespace(state=_ReadOnlyState())
    result = await emit_stakeholder_interview(_valid_payload(), ctx)
    assert result["status"] == "success"



