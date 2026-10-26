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

"""Tests for the Artifact-3 simulation-report tool."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory.simulation_report import (
    MAX_EXEC_SUMMARY_CHARS,
    MAX_FINDING_BODY_CHARS,
    MAX_FINDING_TITLE_CHARS,
    MAX_FINDINGS,
    MAX_HEADLINE_CHARS,
    MAX_JOURNEY_CHARS,
    MAX_REC_BODY_CHARS,
    MAX_RECOMMENDATIONS,
    MAX_STAKEHOLDER_SUMMARIES,
    MIN_FINDINGS,
    PERSONAS_STATE_KEY,
    STATE_KEY,
    emit_simulation_report,
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
        "headline": "AI rollout consolidated by sprint 3.",
        "outcome": "partial",
        "executive_summary": (
            "The phased rollout retained senior sponsorship throughout and "
            "absorbed a mid-management friction wave in sprint 1. Phase-2 "
            "ethics approval plus a training-plan concession in sprint 3 "
            "flipped mid-management to neutral and frontline to supportive. "
            "Legal cleared after the EU AI Act conformity package landed."
        ),
        "key_findings": [
            {
                "title": "Sponsorship held across all sprints",
                "body": "CEO and CTO remained supportive throughout the simulation.",
                "severity": "low",
            },
            {
                "title": "Mid-management friction was the dominant risk",
                "body": "Bandwidth + training gaps surfaced in sprint 1 and required a target-relief concession to defuse.",
                "severity": "high",
            },
            {
                "title": "Frontline trust hinges on the augment-not-replace charter",
                "body": "Sentiment moved from -0.5 to +0.3 once the charter was published.",
                "severity": "medium",
            },
        ],
        "recommendations": [
            {
                "title": "Lock the augment-not-replace charter on day 1",
                "body": "Surfaces job-fear early; reduces the sprint-3 unlock pressure.",
                "priority": "p0",
            },
            {
                "title": "Allocate dedicated training budget pre-launch",
                "body": "Mid-management resistance fades when targets are unblocked AND a training plan exists.",
                "priority": "p1",
            },
        ],
        "stakeholder_summaries": [
            {
                "stakeholder_id": "ceo",
                "final_stance": "supportive",
                "journey": "Steady support; charter language strengthened the narrative.",
            },
            {
                "stakeholder_id": "mid_mgmt",
                "final_stance": "neutral",
                "journey": "Resistant in sprint 1, defused by target relief by sprint 3.",
            },
        ],
        "rationale": "Distilled from the 3-sprint timeline.",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_report() -> None:
    ctx = _ctx()
    result = await emit_simulation_report(json.dumps(_valid_payload()), ctx)

    assert result["status"] == "success"
    report = result["simulation_report"]
    assert report["headline"].startswith("AI rollout")
    assert report["outcome"] == "partial"
    assert len(report["key_findings"]) == 3
    assert len(report["recommendations"]) == 2
    assert len(report["stakeholder_summaries"]) == 2
    assert report["rationale"]
    assert report["inferred"] is True
    assert ctx.state[STATE_KEY] == report


@pytest.mark.asyncio
async def test_accepts_dict_payload() -> None:
    result = await emit_simulation_report(_valid_payload(), _ctx())
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_optional_sections_omitted_when_absent() -> None:
    ctx = _ctx()
    payload = _valid_payload()
    del payload["recommendations"]
    del payload["stakeholder_summaries"]
    del payload["rationale"]
    result = await emit_simulation_report(payload, ctx)
    assert result["status"] == "success"
    report = result["simulation_report"]
    assert "recommendations" not in report
    assert "stakeholder_summaries" not in report
    assert "rationale" not in report


@pytest.mark.asyncio
async def test_inferred_defaults_true_and_preserved_when_false() -> None:
    ctx = _ctx()
    payload = _valid_payload()
    payload["inferred"] = False
    result = await emit_simulation_report(payload, ctx)
    assert result["status"] == "success"
    assert result["simulation_report"]["inferred"] is False


# ---------------------------------------------------------------------------
# JSON / top-level shape failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_string() -> None:
    result = await emit_simulation_report("{not json", _ctx())
    assert result["status"] == "error"
    assert "invalid JSON" in result["message"]


@pytest.mark.asyncio
async def test_payload_must_be_object() -> None:
    result = await emit_simulation_report(json.dumps([]), _ctx())
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_headline_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    del bad["headline"]
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "headline"


@pytest.mark.asyncio
async def test_headline_too_long_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["headline"] = "x" * (MAX_HEADLINE_CHARS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_invalid_outcome_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["outcome"] = "tbd"
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "outcome"


@pytest.mark.asyncio
async def test_executive_summary_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["executive_summary"] = "  "
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "executive_summary"


@pytest.mark.asyncio
async def test_executive_summary_too_long_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["executive_summary"] = "x" * (MAX_EXEC_SUMMARY_CHARS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Findings failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_few_findings_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_findings"] = bad["key_findings"][: MIN_FINDINGS - 1]
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_too_many_findings_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    one = bad["key_findings"][0]
    bad["key_findings"] = [one] * (MAX_FINDINGS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_finding_title_required() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_findings"][0]["title"] = ""
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_finding_title_too_long() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_findings"][0]["title"] = "x" * (MAX_FINDING_TITLE_CHARS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_finding_body_too_long() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_findings"][0]["body"] = "x" * (MAX_FINDING_BODY_CHARS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_invalid_finding_severity_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["key_findings"][0]["severity"] = "critical"
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Stakeholder summaries failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_stakeholder_summary_rejected_when_personas_known() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["stakeholder_summaries"][0]["stakeholder_id"] = "ghost"
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"
    assert "ghost" in result["message"]


@pytest.mark.asyncio
async def test_duplicate_stakeholder_summary_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["stakeholder_summaries"].append(
        {
            "stakeholder_id": "ceo",
            "final_stance": "supportive",
            "journey": "Duplicate.",
        }
    )
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"
    assert "duplicates" in result["message"]


@pytest.mark.asyncio
async def test_invalid_final_stance_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["stakeholder_summaries"][0]["final_stance"] = "ambivalent"
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_journey_too_long_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["stakeholder_summaries"][0]["journey"] = "x" * (MAX_JOURNEY_CHARS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_too_many_stakeholder_summaries_rejected() -> None:
    ctx = _ctx(with_personas=False)
    bad = _valid_payload()
    one = bad["stakeholder_summaries"][0]
    bad["stakeholder_summaries"] = [{**one, "stakeholder_id": f"s{i}"} for i in range(MAX_STAKEHOLDER_SUMMARIES + 1)]
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Recommendations failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_many_recommendations_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    one = bad["recommendations"][0]
    bad["recommendations"] = [one] * (MAX_RECOMMENDATIONS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_invalid_recommendation_priority_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["recommendations"][0]["priority"] = "urgent"
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_recommendation_body_too_long_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["recommendations"][0]["body"] = "x" * (MAX_REC_BODY_CHARS + 1)
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Recommendation -> interview jump link (interview_with)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendation_interview_with_matching_persona_accepted() -> None:
    ctx = _ctx()
    payload = _valid_payload()
    payload["recommendations"][0]["interview_with"] = "mid_mgmt"
    result = await emit_simulation_report(payload, ctx)
    assert result["status"] == "success"
    assert result["simulation_report"]["recommendations"][0]["interview_with"] == "mid_mgmt"


@pytest.mark.asyncio
async def test_recommendation_interview_with_unknown_persona_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["recommendations"][0]["interview_with"] = "nobody"
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "recommendations"
    assert "interview_with" in result["message"]


@pytest.mark.asyncio
async def test_recommendation_interview_with_empty_string_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["recommendations"][0]["interview_with"] = "   "
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_recommendation_interview_with_non_string_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["recommendations"][0]["interview_with"] = 42
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_recommendation_interview_with_permissive_when_no_personas() -> None:
    """Without personas in state, any interview_with id is accepted."""
    ctx = _ctx(with_personas=False)
    payload = _valid_payload()
    payload["recommendations"][0]["interview_with"] = "anything_goes"
    result = await emit_simulation_report(payload, ctx)
    assert result["status"] == "success"
    assert (
        result["simulation_report"]["recommendations"][0]["interview_with"]
        == "anything_goes"
    )


@pytest.mark.asyncio
async def test_recommendation_without_interview_with_omits_field() -> None:
    """Backward compat: omitted interview_with must not appear in the output."""
    ctx = _ctx()
    payload = _valid_payload()
    # _valid_payload() never sets interview_with — verify it stays absent.
    result = await emit_simulation_report(payload, ctx)
    assert result["status"] == "success"
    for rec in result["simulation_report"]["recommendations"]:
        assert "interview_with" not in rec


# ---------------------------------------------------------------------------
# Permissive fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permissive_when_no_personas_in_state() -> None:
    """Without personas in state, any stakeholder_id is accepted."""
    ctx = _ctx(with_personas=False)
    payload = _valid_payload()
    payload["stakeholder_summaries"][0]["stakeholder_id"] = "anything_goes"
    result = await emit_simulation_report(payload, ctx)
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
    result = await emit_simulation_report(_valid_payload(), ctx)
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_inferred_non_bool_rejected() -> None:
    ctx = _ctx()
    bad = _valid_payload()
    bad["inferred"] = "yes"
    result = await emit_simulation_report(bad, ctx)
    assert result["status"] == "error"
    assert result["field"] == "inferred"


