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

"""Unit tests for translation-diagnostic and stakeholder psychology tools."""

from __future__ import annotations

import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest

_TD_TOOLS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skills"
    / "translation-diagnostic"
    / "tools.py"
)
_SPEC = importlib.util.spec_from_file_location("td_tools", _TD_TOOLS_PATH)
assert _SPEC and _SPEC.loader
_TD_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_TD_MOD)

emit_stakeholder_psychology = _TD_MOD.emit_stakeholder_psychology


def _make_ctx(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_falls_back_to_scenario_stakeholders():
    """When stakeholder_map is empty, emit_stakeholder_psychology uses leadership_scenario.stakeholders."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [],
            "leadership_scenario": {
                "stakeholders": [
                    {
                        "id": "vp_eng",
                        "label": "VP of Engineering",
                        "tier": 1,
                        "persona": "Guards platform reliability and engineering bandwidth.",
                        "domain_principles": ["reliability"],
                    },
                    {
                        "id": "dir_ops",
                        "label": "Director of Operations",
                        "tier": 2,
                        "persona": "Focuses on SLA stability and handoff clarity.",
                        "domain_principles": ["handoff_clarity"],
                    },
                ],
            },
            "echo_voice_log": [
                {"tick": 1, "role": "vp_eng", "sentiment": 0.1, "resistance": 0.4, "stance": "skeptical"},
                {"tick": 2, "role": "vp_eng", "sentiment": 0.5, "resistance": 0.2, "stance": "supportive"},
                {"tick": 1, "role": "dir_ops", "sentiment": -0.2, "resistance": 0.6, "stance": "resistant"},
            ],
            "decision_log": [
                {"tick": 1, "decision_tags": {"principles_applied": ["reliability"]}},
            ],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    profiles = {p["stakeholder_id"]: p for p in result["stakeholder_psychology"]["profiles"]}
    assert set(profiles.keys()) == {"vp_eng", "dir_ops"}

    vp = profiles["vp_eng"]
    assert vp["stakeholder_label"] == "VP of Engineering"
    assert vp["tier"] == 1
    assert vp["decision_style"] == "data-driven"
    assert vp["communication_style"] == "direct"
    assert vp["risk_tolerance"] == "moderate"

    ops = profiles["dir_ops"]
    assert ops["stakeholder_label"] == "Director of Operations"
    assert ops["tier"] == 2
    assert ops["decision_style"] == "consensus-seeking"
    assert ops["communication_style"] == "diplomatic"
    assert ops["risk_tolerance"] == "low"


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_falls_back_to_scenario_echoes_and_resolves_unknown():
    """When echo_voice_log has role='unknown', it resolves against leadership_scenario.echoes."""
    ctx = _make_ctx(
        {
            "leadership_scenario": {
                "echoes": [
                    {
                        "role": "acct_mgr",
                        "label": "Senior Account Manager",
                        "tier": 3,
                        "persona": "Protects customer relationships and quoting autonomy.",
                    },
                ],
            },
            "echo_voice_log": [
                {"tick": 1, "role": "unknown", "sentiment": -0.3, "resistance": 0.7, "stance": "resistant"},
            ],
            "decision_log": [],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    profiles = result["stakeholder_psychology"]["profiles"]
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile["stakeholder_id"] == "acct_mgr"
    assert profile["stakeholder_label"] == "Senior Account Manager"
    assert profile["tier"] == 3
    assert profile["decision_style"] == "pragmatic"
    assert profile["communication_style"] == "structured"
    assert profile["risk_tolerance"] == "moderate"


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_discards_spurious_unknown_when_real_roles_exist():
    """An unconfigured 'unknown' echo entry is ignored when real stakeholder roles are present."""
    ctx = _make_ctx(
        {
            "leadership_scenario": {
                "stakeholders": [
                    {
                        "id": "vp_eng",
                        "label": "VP of Engineering",
                        "tier": 1,
                    },
                ],
            },
            "echo_voice_log": [
                {"tick": 1, "role": "vp_eng", "sentiment": 0.3, "resistance": 0.2, "stance": "receptive"},
                {"tick": 1, "role": "unknown", "sentiment": 0.0, "resistance": 0.5, "stance": "neutral"},
            ],
            "decision_log": [],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    profiles = result["stakeholder_psychology"]["profiles"]
    assert [p["stakeholder_id"] for p in profiles] == ["vp_eng"]


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_handles_serialized_json_scenario_and_multiple_unmapped_roles():
    """Serialized JSON leadership_scenario and multiple unmapped roles resolve cleanly without TIER 0 or unknown styles."""
    import json

    ctx = _make_ctx(
        {
            "leadership_scenario": json.dumps(
                {
                    "stakeholders": [
                        {
                            "id": "cfo",
                            "label": "Chief Financial Officer",
                            "tier": 1,
                            "domain_principles": ["Resource Optimization & Strategic Allocation"],
                        },
                        {
                            "id": "union_rep",
                            "label": "Union Representative",
                            "tier": 3,
                            "domain_principles": ["Empowerment & Cultivating Distributed Leadership"],
                        },
                    ]
                }
            ),
            "echo_voice_log": [
                {"tick": 1, "role": "unmapped_alpha", "sentiment": -0.4, "resistance": 0.8, "stance": "hostile"},
                {"tick": 1, "role": "unmapped_beta", "sentiment": 0.2, "resistance": 0.3, "stance": "neutral"},
            ],
            "decision_log": [
                {
                    "tick": 1,
                    "decision_tags": {
                        "principles_applied": ["Resource Optimization & Strategic Allocation"]
                    },
                }
            ],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    profiles = {p["stakeholder_id"]: p for p in result["stakeholder_psychology"]["profiles"]}
    assert set(profiles.keys()) == {"cfo", "union_rep"}
    assert profiles["cfo"]["stakeholder_label"] == "Chief Financial Officer"
    assert profiles["cfo"]["tier"] == 1
    assert profiles["cfo"]["wtt_breakdown"]["principle_engagement"] == 100
    assert profiles["union_rep"]["stakeholder_label"] == "Union Representative"
    assert profiles["union_rep"]["tier"] == 3
    for p in profiles.values():
        assert p["tier"] > 0
        assert p["decision_style"] != "unknown"
        assert p["communication_style"] != "unknown"
        assert p["risk_tolerance"] != "unknown"


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_never_emits_tier_zero_or_unknown_label_without_scenario():
    """Even when both stakeholder_map and leadership_scenario are absent, tier and styles never fall back to 0/unknown."""
    ctx = _make_ctx(
        {
            "echo_voice_log": [
                {"tick": 1, "role": "unknown", "sentiment": 0.0, "resistance": 0.5, "stance": "neutral"},
            ],
            "decision_log": [],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    profile = result["stakeholder_psychology"]["profiles"][0]
    assert profile["stakeholder_label"] == "Stakeholder Representative"
    assert profile["tier"] == 3
    assert profile["decision_style"] == "pragmatic"
    assert profile["communication_style"] == "structured"
    assert profile["risk_tolerance"] == "moderate"

