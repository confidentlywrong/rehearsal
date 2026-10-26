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


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_and_psychology_with_empty_state_generate_fallbacks():
    """When session state has no telemetry, realistic fallback data is generated and tools succeed."""
    emit_translation_diagnostic = _TD_MOD.emit_translation_diagnostic

    ctx = _make_ctx({})

    td_res = await emit_translation_diagnostic(ctx)
    assert td_res["status"] == "success"
    assert "translation_diagnostic" in td_res
    gaps = td_res["translation_diagnostic"].get("gaps", [])
    assert len(gaps) > 0
    assert td_res["translation_diagnostic"].get("summary")

    sp_res = await emit_stakeholder_psychology(ctx)
    assert sp_res["status"] == "success"
    assert "stakeholder_psychology" in sp_res
    profiles = sp_res["stakeholder_psychology"].get("profiles", [])
    assert len(profiles) > 0
    assert sp_res["stakeholder_psychology"].get("summary")
    for p in profiles:
        assert p["tier"] > 0
        assert p["wtt_score"] >= 0
        assert p["decision_style"] != "unknown"


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_local_gemma_deterministic_fast_and_clean(monkeypatch):
    """When is_local_gemma_active() is True, emit_translation_diagnostic runs in <50ms without LLM calls and with zero placeholder strings."""
    import json
    import time

    emit_translation_diagnostic = _TD_MOD.emit_translation_diagnostic

    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)

    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "label": "Chief Financial Officer",
                    "tier": 1,
                    "persona": "Fiduciary guardian prioritizing capital efficiency and margin expansion.",
                    "domain_principles": ["Capital Discipline", "Strategic Value Creation"],
                },
                {
                    "id": "eng_lead",
                    "role": "eng_lead",
                    "label": "Lead Systems Architect",
                    "tier": 2,
                    "persona": "Guards system architecture and sprint engineering bandwidth.",
                    "domain_principles": ["Architectural Integrity", "Operational Reliability"],
                },
            ],
            "echo_voice_log": [
                {
                    "tick": 1,
                    "role": "cfo",
                    "sentiment": -0.35,
                    "resistance": 0.6,
                    "stance": "resistant",
                    "inner_thought": "Concerned about unchecked capital expenditure and lack of ROI metrics.",
                },
                {
                    "tick": 2,
                    "role": "cfo",
                    "sentiment": 0.3,
                    "resistance": 0.2,
                    "stance": "supportive",
                    "inner_thought": "Capital safeguards addressed our primary margin risk.",
                },
                {
                    "tick": 1,
                    "role": "eng_lead",
                    "sentiment": -0.4,
                    "resistance": 0.7,
                    "stance": "resistant",
                    "inner_thought": "Delivery acceleration is causing severe cognitive overload and technical debt.",
                },
            ],
            "decision_log": [
                {
                    "tick": 1,
                    "decision_text": "Accelerate platform delivery benchmarks and establish mandatory sprint output quotas.",
                    "decision_tags": {
                        "principles_applied": ["Strategic Value Creation", "Delivery Cadence"]
                    },
                },
                {
                    "tick": 2,
                    "decision_text": "Introduce protected architectural buffer windows and capital governance gates.",
                    "decision_tags": {
                        "principles_applied": ["Capital Discipline", "Operational Reliability"]
                    },
                },
            ],
        }
    )

    t0 = time.perf_counter()
    result = await emit_translation_diagnostic(ctx)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 50.0, f"Expected < 50ms, took {elapsed_ms:.2f}ms"
    assert result["status"] == "success"
    diag = result["translation_diagnostic"]
    assert diag["eval_method"] == "deterministic_habermas"
    assert diag["inferred"] is True
    assert "gaps" in diag
    assert len(diag["gaps"]) == 2

    # Verify no ugly fallback strings anywhere in the serialized output
    raw_json = json.dumps(diag)
    assert "LLM unavailable" not in raw_json
    assert "heuristic — LLM" not in raw_json

    # Verify gap contents
    cfo_gap = next(g for g in diag["gaps"] if g["stakeholder_id"] == "cfo")
    assert cfo_gap["stakeholder_label"] == "Chief Financial Officer"
    assert cfo_gap["overall_gap_severity"] in ("high", "medium", "low")
    assert len(cfo_gap["trigger_phrases"]) >= 1
    assert "Capital Discipline" in cfo_gap["trigger_phrases"][0]["leader_intent"]
    assert " → " in cfo_gap["trigger_phrases"][0]["gap_type"]
    assert len(cfo_gap["suggested_reframing"]) > 20

    eng_gap = next(g for g in diag["gaps"] if g["stakeholder_id"] == "eng_lead")
    assert eng_gap["stakeholder_label"] == "Lead Systems Architect"
    assert "overload" in eng_gap["trigger_phrases"][0]["gap_type"] or "burnout" in eng_gap["trigger_phrases"][0]["gap_type"]


@pytest.mark.asyncio
async def test_heuristic_fallback_contains_no_unwanted_strings():
    """_heuristic_fallback produces clean, professional Habermas translations without '(LLM unavailable)'."""
    import json

    heuristic_fallback = _TD_MOD._heuristic_fallback

    inflections = {
        "ops_lead": [
            {
                "tick": 1,
                "sentiment": -0.3,
                "stance": "skeptical",
                "inner_thought": "Concerned about bandwidth strain across cross-functional handoffs.",
            }
        ]
    }
    decision_log = [
        {
            "tick": 1,
            "decision_text": "Establish core governance framework and clarify resource commitments.",
            "decision_tags": {"principles_applied": ["Operational Reliability"]},
        }
    ]

    res = heuristic_fallback(inflections, decision_log)
    raw_str = json.dumps(res)
    assert "LLM unavailable" not in raw_str
    assert "manual analysis" not in raw_str
    assert "approximate" not in raw_str

    gap = res["gaps"][0]
    assert gap["stakeholder_id"] == "ops_lead"
    assert gap["stakeholder_label"] == "Ops Lead"
    tp = gap["trigger_phrases"][0]
    assert "Strategic" in tp["leader_intent"] or "Operational" in tp["leader_intent"]
    assert "bandwidth strain" in tp["stakeholder_decoded_as"]
    assert " → " in tp["gap_type"]
    assert len(gap["suggested_reframing"]) > 10


@pytest.mark.asyncio
async def test_translation_diagnostic_handles_serialized_tags_and_string_sentiments(monkeypatch):
    """Verify diagnostic and psychology tools safely parse JSON string decision_tags and string sentiments."""
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)

    emit_translation_diagnostic = _TD_MOD.emit_translation_diagnostic
    emit_stakeholder_psychology = _TD_MOD.emit_stakeholder_psychology

    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "label": "Chief Financial Officer",
                    "tier": 1,
                    "persona": "Capital allocation and margin discipline.",
                    "domain_principles": ["Capital Discipline"],
                }
            ],
            "echo_voice_log": [
                {
                    "tick": "1",
                    "role": "cfo",
                    "sentiment": "-0.45",
                    "stance": "resistant",
                    "inner_thought": "Concerned about unchecked capital expenditure and lack of ROI metrics.",
                },
                {
                    "tick": 2,
                    "role": "cfo",
                    "sentiment": "0.25",
                    "stance": "supportive",
                    "inner_thought": "Capital safeguards addressed our primary margin risk.",
                },
            ],
            "decision_log": [
                {
                    "tick": 1,
                    "decision_text": "Accelerate delivery benchmarks.",
                    # Serialized JSON string as sometimes stored in Redis or db
                    "decision_tags": '{"principles_applied": ["Strategic Value Creation"]}',
                },
                {
                    "tick": 2,
                    "decision_text": "Introduce protected architectural buffer windows.",
                    "decision_tags": '{"principles_applied": ["Capital Discipline"]}',
                },
            ],
        }
    )

    td_res = await emit_translation_diagnostic(ctx)
    assert td_res["status"] == "success"
    gaps = td_res["translation_diagnostic"]["gaps"]
    assert len(gaps) == 1
    assert gaps[0]["stakeholder_id"] == "cfo"
    assert len(gaps[0]["trigger_phrases"]) >= 1
    assert "Capital Discipline" in gaps[0]["trigger_phrases"][0]["leader_intent"]
    assert gaps[0]["trigger_phrases"][0]["tick"] == 2

    sp_res = await emit_stakeholder_psychology(ctx)
    assert sp_res["status"] == "success"
    profiles = sp_res["stakeholder_psychology"]["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["stakeholder_id"] == "cfo"


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_advocacy_and_escalation_categorization_and_deduplication():
    """Verify advocacy_actions and escalation_events are properly separated, deduplicated, and allies formatted."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "vp_operations",
                    "role": "vp_operations",
                    "tier": 1,
                    "coalition_affinity": ["cfo (0.8)", "data_scientist_lead (0.5)"],
                },
                {
                    "id": "data_scientist_lead",
                    "role": "data_scientist_lead",
                    "tier": 2,
                    "coalition_affinity": ["vp_operations"],
                },
            ],
            "echo_voice_log": [
                # VP Operations: advocacy actions + duplicate + stance shift
                {
                    "tick": 1,
                    "role": "vp_operations",
                    "sentiment": 0.6,
                    "stance": "supportive",
                    "autonomous_action": "vp_operations champions the initiative internally",
                },
                {
                    "tick": 2,
                    "role": "vp_operations",
                    "sentiment": 0.7,
                    "stance": "supportive",
                    "autonomous_action": "vp_operations champions the initiative internally",  # duplicate
                },
                {
                    "tick": 3,
                    "role": "vp_operations",
                    "sentiment": 0.9,
                    "stance": "aligned",
                    "notable_event": "vp_operations shifts from supportive to aligned",
                },
                # Data Scientist Lead: escalation events + duplicate + negative stance shift
                {
                    "tick": 1,
                    "role": "data_scientist_lead",
                    "sentiment": -0.3,
                    "stance": "resistant",
                    "autonomous_action": "data_scientist_lead escalates concerns to external stakeholders",
                },
                {
                    "tick": 2,
                    "role": "data_scientist_lead",
                    "sentiment": -0.3,
                    "stance": "resistant",
                    "autonomous_action": "data_scientist_lead escalates concerns to external stakeholders",  # duplicate
                },
                {
                    "tick": 3,
                    "role": "data_scientist_lead",
                    "sentiment": -0.6,
                    "stance": "hostile",
                    "notable_event": "data_scientist_lead shifts from resistant to hostile",
                },
            ],
            "decision_log": [],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    profiles = {p["stakeholder_id"]: p for p in result["stakeholder_psychology"]["profiles"]}

    vp = profiles["vp_operations"]
    assert vp["stakeholder_label"] == "VP Operations"
    assert vp["coalition_allies"] == ["CFO", "Data Scientist Lead"]
    # Advocacy events should contain the champion action (formatted) and the positive shift
    assert len(vp["advocacy_actions"]) == 2
    assert "VP Operations champions the initiative internally" in vp["advocacy_actions"]
    assert "VP Operations shifts from supportive to aligned" in vp["advocacy_actions"]
    assert vp["escalation_events"] == []

    dsl = profiles["data_scientist_lead"]
    assert dsl["stakeholder_label"] == "Data Scientist Lead"
    assert dsl["coalition_allies"] == ["VP Operations"]
    # Escalation events should contain the escalates action (formatted) and the negative shift
    assert len(dsl["escalation_events"]) == 2
    assert "Data Scientist Lead escalates concerns to external stakeholders" in dsl["escalation_events"]
    assert "Data Scientist Lead shifts from resistant to hostile" in dsl["escalation_events"]
    assert dsl["advocacy_actions"] == []


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_fallback_matches_frontend_schema():
    """Verify emergency fallback profile matches frontend TypeScript schema completely."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "tier": 1,
                    "persona": "Chief Financial Officer safeguarding fiscal targets.",
                },
            ],
            "echo_voice_log": [],
            "decision_log": [],
        }
    )

    # Monkeypatch _ensure_telemetry_in_state on _TD_MOD to return empty lists to force the fallback branch
    original_ensure = _TD_MOD._ensure_telemetry_in_state
    try:
        _TD_MOD._ensure_telemetry_in_state = lambda c: ([], [])
        result = await emit_stakeholder_psychology(ctx)
        assert result["status"] == "success"
        profiles = result["stakeholder_psychology"]["profiles"]
        assert len(profiles) == 1
        p = profiles[0]
        assert p["stakeholder_id"] == "cfo"
        assert p["stakeholder_label"] == "CFO"
        # Validate WTT breakdown schema
        assert set(p["wtt_breakdown"].keys()) == {
            "trust_level",
            "resistance_absence",
            "principle_engagement",
            "trust_integrity",
        }
        # Validate sentiment trajectory schema
        assert set(p["sentiment_trajectory"].keys()) == {
            "initial",
            "final",
            "min",
            "max",
            "max_delta_tick",
            "trend",
        }
        assert p["sentiment_trajectory"]["trend"] == "rising"
        # Validate events lists
        assert p["advocacy_actions"] == []
        assert p["escalation_events"] == []
    finally:
        _TD_MOD._ensure_telemetry_in_state = original_ensure


@pytest.mark.asyncio
async def test_build_analysis_prompt_safely_handles_non_integer_ticks():
    """Verify _build_analysis_prompt does not raise ValueError when entry has non-integer tick."""
    _build_analysis_prompt = _TD_MOD._build_analysis_prompt

    inflections = {
        "cfo": [
            {
                "tick": "invalid_tick_string",
                "sentiment": 0.5,
                "stance": "supportive",
                "inner_thought": "Milestones align well.",
            }
        ]
    }
    decision_log = [{"tick": 1, "decision_text": "Clarified milestones."}]

    prompt = _build_analysis_prompt(inflections, decision_log, None)
    assert "## Stakeholder: cfo" in prompt
    assert "Tick invalid_tick_string" in prompt
    assert "(decision text not available)" in prompt


@pytest.mark.asyncio
async def test_sentiment_trajectory_and_inner_sentiment_support_alias_sets():
    """Verify trajectory and inner sentiment compute correctly when given alias sets."""
    _compute_sentiment_trajectory = _TD_MOD._compute_sentiment_trajectory
    _avg_inner_sentiment_for_role = _TD_MOD._avg_inner_sentiment_for_role

    echo_voice_log = [
        {"tick": 1, "role": "vp_operations", "sentiment": 0.2, "stance": "neutral"},
        {"tick": 2, "role": "VP Operations", "sentiment": 0.6, "stance": "supportive"},
    ]

    aliases = {"vp_operations", "vp operations", "director_ops"}
    traj = _compute_sentiment_trajectory(echo_voice_log, aliases)
    assert traj["initial"] == 0.2
    assert traj["final"] == 0.6
    assert traj["trend"] == "rising"

    avg_inner = _avg_inner_sentiment_for_role(echo_voice_log, aliases)
    assert avg_inner > 0.1


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_cleans_embedded_snake_case_slugs():
    """Verify embedded snake_case slugs in notable_event or autonomous_action are converted to title-case with acronym preservation."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "label": "Chief Financial Officer",
                    "tier": 1,
                }
            ],
            "echo_voice_log": [
                {
                    "tick": 1,
                    "role": "cfo",
                    "sentiment": 0.7,
                    "stance": "champion",
                    "autonomous_action": "cfo champions the initiative internally with vp_operations and head_of_ai",
                }
            ],
            "decision_log": [],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    profile = result["stakeholder_psychology"]["profiles"][0]
    assert len(profile["advocacy_actions"]) == 1
    action = profile["advocacy_actions"][0]
    assert "Chief Financial Officer champions the initiative internally with VP Operations and Head of AI" == action
    assert "vp_operations" not in action
    assert "head_of_ai" not in action





