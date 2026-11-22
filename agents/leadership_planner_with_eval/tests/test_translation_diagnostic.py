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
    """When session state has no telemetry, tools return clean error without mutating state or fabricating data."""
    emit_translation_diagnostic = _TD_MOD.emit_translation_diagnostic

    ctx = _make_ctx({})

    td_res = await emit_translation_diagnostic(ctx)
    assert td_res["status"] == "error"
    assert td_res["error"] == "no_telemetry"
    assert "translation_diagnostic" in td_res
    assert td_res["translation_diagnostic"].get("gaps") == []
    assert "decision_log" not in ctx.state
    assert "echo_voice_log" not in ctx.state

    sp_res = await emit_stakeholder_psychology(ctx)
    assert sp_res["status"] == "error"
    assert sp_res["error"] == "no_telemetry"
    assert "stakeholder_psychology" in sp_res
    assert sp_res["stakeholder_psychology"].get("profiles") == []
    assert "decision_log" not in ctx.state
    assert "echo_voice_log" not in ctx.state


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
async def test_deterministic_habermas_diagnostic_contains_no_unwanted_strings():
    """_generate_habermas_diagnostic produces clean, professional Habermas translations without '(LLM unavailable)'."""
    import json

    generate_habermas_diagnostic = _TD_MOD._generate_habermas_diagnostic

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

    res = generate_habermas_diagnostic(inflections, decision_log)
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
async def test_emit_stakeholder_psychology_with_stakeholder_map_establishes_baseline_profiles_without_fake_telemetry():
    """Verify that when telemetry is absent but stakeholder_map is present, emit_stakeholder_psychology establishes baseline profiles preserving behavioral patterns with zero fake telemetry."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "tier": 1,
                    "persona": "Chief Financial Officer safeguarding fiscal targets.",
                    "behavioral_patterns": {
                        "decision_style": "strategic",
                        "communication": "top-down directive",
                        "risk_tolerance": "high",
                    },
                },
            ],
            "echo_voice_log": [],
            "decision_log": [],
        }
    )

    result = await emit_stakeholder_psychology(ctx)
    assert result["status"] == "success"
    assert "stakeholder_psychology" in result
    psych = result["stakeholder_psychology"]
    profiles = psych["profiles"]
    assert len(profiles) == 1
    p = profiles[0]
    assert p["stakeholder_id"] == "cfo"
    assert p["decision_style"] == "strategic"
    assert p["communication_style"] == "top-down directive"

    # Strictly verify zero fake telemetry
    assert p["sentiment_trajectory"]["trend"] == "flat"
    assert p["sentiment_trajectory"]["initial"] == 0.0
    assert p["sentiment_trajectory"]["final"] == 0.0
    assert p["sentiment_trajectory"]["min"] == 0.0
    assert p["sentiment_trajectory"]["max"] == 0.0
    assert p["sentiment_trajectory"]["max_delta_tick"] == 0
    assert p["terminal_state"] is None
    assert p["advocacy_actions"] == []
    assert p["escalation_events"] == []
    assert p["malicious_compliance_signals"]["displayed_stance"] == "neutral"
    assert p["malicious_compliance_signals"]["wall_severity"] == 0.0
    assert p["malicious_compliance_signals"]["inner_sentiment_avg"] == 0.0

    # Verify baseline state persistence
    assert "stakeholder_psychology" in ctx.state


@pytest.mark.asyncio
async def test_telemetry_tools_safely_handle_malformed_non_dict_items():
    """Verify emit_translation_diagnostic and _generate_habermas_diagnostic safely handle non-dict items in decision_log."""
    emit_translation_diagnostic = _TD_MOD.emit_translation_diagnostic
    _generate_habermas_diagnostic = _TD_MOD._generate_habermas_diagnostic

    # Telemetry in state containing None or non-dict items
    ctx = _make_ctx(
        {
            "echo_voice_log": [None, {"tick": 1, "role": "cfo", "sentiment": -0.3, "stance": "skeptical"}],
            "decision_log": [None, "invalid_string", 42],
        }
    )

    # When decision_log contains only malformed items, it safely returns error: no_telemetry
    res = await emit_translation_diagnostic(ctx)
    assert res["status"] == "error"
    assert res["error"] == "no_telemetry"

    # Direct call to _generate_habermas_diagnostic with mixed valid/invalid entries does not crash
    diag = _generate_habermas_diagnostic(
        inflections={"cfo": [{"tick": 1, "sentiment": -0.3, "stance": "skeptical"}]},
        decision_log=[None, {"tick": 1, "decision_text": "Valid text"}],
        stakeholder_map=[None, {"id": "cfo", "role": "cfo", "tier": 1}],
    )
    assert "gaps" in diag
    assert len(diag["gaps"]) == 1


@pytest.mark.asyncio
async def test_generate_habermas_diagnostic_does_not_synthesize_pitch_phrases():
    """Verify _generate_habermas_diagnostic uses '(No decision text recorded)' instead of synthetic governance pitch phrases."""
    _generate_habermas_diagnostic = _TD_MOD._generate_habermas_diagnostic

    diag = _generate_habermas_diagnostic(
        inflections={"cfo": [{"tick": 1, "sentiment": -0.3, "stance": "skeptical"}]},
        decision_log=[{"tick": 1}],
        stakeholder_map=[{"id": "cfo", "role": "cfo", "tier": 1}],
    )
    gap = diag["gaps"][0]
    phrase = gap["trigger_phrases"][0]["phrase"]
    assert phrase == "(No decision text recorded)"
    assert "Establish core governance framework" not in phrase
    assert "Roll out phased operational workflows" not in phrase
    assert "Align operational delivery standards" not in phrase


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


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_handles_state_attribute_error_without_unbound_local():
    """Regression: accessing a failing tool_context.state property must not raise UnboundLocalError on state_obj."""

    class _ExplodingStateCtx:
        @property
        def state(self):
            raise AttributeError("broken state")

    res = await emit_stakeholder_psychology(_ExplodingStateCtx())  # type: ignore[arg-type]
    assert res["status"] == "error"
    assert res["error"] == "no_telemetry"


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_cloud_llm_failure_returns_error(monkeypatch):
    """Cloud mode must return status=='error' (error=='llm_failed') instead of silent heuristic fallback."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: False)
    fake_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(side_effect=RuntimeError("Gemini 503"))
            )
        )
    )
    monkeypatch.setattr(_TD_MOD, "_get_client", lambda: fake_client)

    ctx = _make_ctx(
        {
            "echo_voice_log": [{"tick": 1, "role": "cfo", "sentiment": -0.5, "stance": "skeptical"}],
            "decision_log": [{"tick": 1, "decision_text": "Accelerate rollout"}],
        }
    )
    res = await _TD_MOD.emit_translation_diagnostic(ctx)
    assert res["status"] == "error"
    assert res["error"] == "llm_failed"
    assert res["translation_diagnostic"]["gaps"] == []


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_no_inflections_returns_error():
    """Entries lacking valid stakeholder roles must return error=='no_inflections' without synthetic inflections."""
    ctx = _make_ctx(
        {
            "echo_voice_log": [{"tick": 1, "sentiment": -0.3}],
            "decision_log": [{"tick": 1, "decision_text": "Ship phase 1"}],
        }
    )
    res = await _TD_MOD.emit_translation_diagnostic(ctx)
    assert res["status"] == "error"
    assert res["error"] == "no_inflections"
    assert res["translation_diagnostic"]["gaps"] == []


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_empty_stakeholder_map_no_synthetic_personas(monkeypatch):
    """Empty stakeholder_map must not inject synthetic executive_sponsor/operations_director/team_representative."""
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)
    ctx = _make_ctx(
        {
            "echo_voice_log": [{"tick": 1, "role": "cfo", "sentiment": -0.4, "stance": "skeptical"}],
            "decision_log": [{"tick": 1, "decision_text": "Enforce budget freeze"}],
        }
    )
    res = await _TD_MOD.emit_translation_diagnostic(ctx)
    assert res["status"] == "success"
    gap_ids = {g["stakeholder_id"] for g in res["translation_diagnostic"]["gaps"]}
    assert gap_ids == {"cfo"}
    assert not ({"executive_sponsor", "operations_director", "team_representative"} & gap_ids)


def test_translation_diagnostic_model_uses_canonical_default(monkeypatch):
    """_MODEL resolves DEFAULT_FLASH_LITE_MODEL and ignores blank TRANSLATION_DIAGNOSTIC_MODEL."""
    from agents.utils.models import DEFAULT_FLASH_LITE_MODEL, get_model

    monkeypatch.setenv("TRANSLATION_DIAGNOSTIC_MODEL", "   ")
    monkeypatch.delenv("EVALUATOR_MODEL", raising=False)
    assert (
        get_model(
            "TRANSLATION_DIAGNOSTIC_MODEL",
            DEFAULT_FLASH_LITE_MODEL,
            fallback_env_vars=("EVALUATOR_MODEL",),
        )
        == DEFAULT_FLASH_LITE_MODEL
    )


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_local_gemma_handles_malformed_tier_and_none_tick(monkeypatch):
    """Regression: non-int tier and tick=None in local_gemma mode must not raise ValueError/TypeError."""
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {"id": "cto", "role": "cto", "label": "CTO", "tier": "not-an-int"},
            ],
            "echo_voice_log": [
                {"tick": None, "role": "cto", "sentiment": 0.5, "stance": "neutral"},
            ],
            "decision_log": [
                {"tick": 1, "decision_text": "Align architecture milestones."},
            ],
        }
    )

    res = await _TD_MOD.emit_translation_diagnostic(ctx)
    assert res["status"] == "success"
    diag = res["translation_diagnostic"]
    assert diag["eval_method"] == "deterministic_habermas"
    assert len(diag["gaps"]) == 1
    assert diag["gaps"][0]["stakeholder_id"] == "cto"
    assert diag["gaps"][0]["trigger_phrases"][0]["tick"] == 1


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_handles_none_domain_principles_and_overflow_float(monkeypatch):
    """Regression: domain_principles=None (with empty principles_applied) and huge int sentiment must not crash."""
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)
    assert _TD_MOD._safe_finite_float(10**400, 0.0) == 0.0

    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "label": "CFO",
                    "tier": 1,
                    "domain_principles": None,
                }
            ],
            "echo_voice_log": [
                {"tick": 1, "role": "cfo", "sentiment": 10**400, "stance": "neutral"},
                {"tick": 2, "role": "cfo", "sentiment": -0.3, "stance": "skeptical"},
            ],
            "decision_log": [
                {"tick": 2, "decision_text": "Review capital allocation."},
            ],
        }
    )

    res = await _TD_MOD.emit_translation_diagnostic(ctx)
    assert res["status"] == "success"
    diag = res["translation_diagnostic"]
    assert len(diag["gaps"]) == 1
    assert diag["gaps"][0]["stakeholder_id"] == "cfo"


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_handles_none_and_non_list_fields():
    """Regression: emit_stakeholder_psychology must handle None and scalar/string list fields without TypeError or char-splitting."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "label": "CFO",
                    "tier": 1,
                    "persona": None,
                    "domain_principles": None,
                    "alignment_drivers": None,
                    "resistance_triggers": None,
                },
                {
                    "id": "cto",
                    "role": "cto",
                    "label": "CTO",
                    "tier": 2,
                    "persona": "Platform architect",
                    "domain_principles": "invalid",
                    "alignment_drivers": "resilience",
                    "resistance_triggers": 42,
                },
                {
                    "id": "coo",
                    "role": "coo",
                    "label": "COO",
                    "tier": 2,
                    "persona": "Operations lead",
                    "domain_principles": ["delivery_cadence"],
                },
            ],
            "echo_voice_log": [
                {"tick": 1, "role": "cfo", "sentiment": -0.2, "stance": "skeptical"},
                {"tick": 1, "role": "cto", "sentiment": 0.1, "stance": "neutral"},
                {"tick": 1, "role": "coo", "sentiment": 0.3, "stance": "receptive"},
            ],
            "decision_log": [
                {
                    "tick": 1,
                    "decision_text": "Phase rollout across teams.",
                    "decision_tags": {"principles_applied": None},
                },
                {
                    "tick": 2,
                    "decision_text": "Protect delivery cadence.",
                    "decision_tags": {"principles_applied": ["delivery_cadence"]},
                },
            ],
            "contradiction_log": [
                {"tick": 1, "violated_principle": "delivery_cadence"},
                {"tick": 2, "violated_principle": None},
            ],
        }
    )

    res = await _TD_MOD.emit_stakeholder_psychology(ctx)
    assert res["status"] == "success"
    profiles = {p["stakeholder_id"]: p for p in res["stakeholder_psychology"]["profiles"]}
    assert set(profiles.keys()) == {"cfo", "cto", "coo"}

    for sid in ("cfo", "cto", "coo"):
        p = profiles[sid]
        assert isinstance(p["domain_principles"], list)
        assert isinstance(p["alignment_drivers"], list)
        assert isinstance(p["resistance_triggers"], list)
        assert isinstance(p["persona_summary"], str)

    assert profiles["cfo"]["persona_summary"] == ""
    assert profiles["cfo"]["domain_principles"] == []
    assert profiles["cfo"]["alignment_drivers"] == []
    assert profiles["cfo"]["resistance_triggers"] == []

    # String "invalid" must NOT split into ['i', 'n', 'v', ...]
    assert profiles["cto"]["domain_principles"] == []
    assert profiles["cto"]["alignment_drivers"] == []
    assert profiles["cto"]["resistance_triggers"] == []

    assert profiles["coo"]["domain_principles"] == ["delivery_cadence"]
    assert profiles["coo"]["trust_breaches"] == 1
    assert profiles["coo"]["wtt_breakdown"]["principle_engagement"] == 50


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_baseline_handles_non_list_fields():
    """Regression: baseline branch (no echo_voice_log) must coerce non-list domain_principles without char-splitting."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "label": "CFO",
                    "tier": 1,
                    "persona": None,
                    "domain_principles": "invalid",
                    "alignment_drivers": None,
                    "resistance_triggers": 123,
                }
            ],
        }
    )

    res = await _TD_MOD.emit_stakeholder_psychology(ctx)
    assert res["status"] == "success"
    profiles = res["stakeholder_psychology"]["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["domain_principles"] == []
    assert profiles[0]["alignment_drivers"] == []
    assert profiles[0]["resistance_triggers"] == []
    assert profiles[0]["persona_summary"] == ""


@pytest.mark.asyncio
async def test_emit_translation_diagnostic_and_prompt_handle_malformed_principles_roles_and_stakeholders(monkeypatch):
    """Regression: malformed principles_applied, non-str roles, and non-dict/unhashable stakeholder_map must not crash."""
    import agents.utils.local_ai as local_ai_mod

    monkeypatch.setattr(local_ai_mod, "is_local_gemma_active", lambda: True)

    ctx_none_list = _make_ctx(
        {
            "stakeholder_map": [
                "not_a_dict",
                {"role": ["cfo"], "persona": "x"},
                {"id": "cfo", "role": "cfo", "tier": 1, "persona": "Finance chief"},
            ],
            "echo_voice_log": [
                {"tick": 1, "role": 123, "sentiment": 0.5, "stance": "neutral"},
                {"tick": 1, "role": ["cfo"], "sentiment": 0.1, "stance": "neutral"},
                {"tick": 1, "role": "cfo", "sentiment": 0.2, "stance": "neutral"},
                {"tick": 2, "role": "cfo", "sentiment": -0.4, "stance": "resistant"},
            ],
            "decision_log": [
                {
                    "tick": 2,
                    "decision_text": "Cut budget.",
                    "decision_tags": {"principles_applied": [None, "", "roi"]},
                }
            ],
        }
    )
    res_none = await _TD_MOD.emit_translation_diagnostic(ctx_none_list)
    assert res_none["status"] == "success"
    assert [g["stakeholder_id"] for g in res_none["translation_diagnostic"]["gaps"]] == ["cfo"]

    ctx_int_list = _make_ctx(
        {
            "stakeholder_map": [{"id": "cfo", "role": "cfo", "tier": 1}],
            "echo_voice_log": [
                {"tick": 1, "role": "cfo", "sentiment": 0.2, "stance": "neutral"},
                {"tick": 2, "role": "cfo", "sentiment": -0.4, "stance": "resistant"},
            ],
            "decision_log": [
                {
                    "tick": 2,
                    "decision_text": "Cut budget.",
                    "decision_tags": {"principles_applied": [123]},
                }
            ],
        }
    )
    res_int = await _TD_MOD.emit_translation_diagnostic(ctx_int_list)
    assert res_int["status"] == "success"
    assert len(res_int["translation_diagnostic"]["gaps"]) == 1

    inflections = _TD_MOD._find_inflection_points(
        [
            {"tick": 1, "role": 123, "sentiment": 0.5},
            {"tick": 1, "role": ["cfo"], "sentiment": 0.1},
            {"tick": 1, "role": "   ", "sentiment": 0.1},
            {"tick": 1, "role": "cfo", "sentiment": -0.3, "stance": "skeptical"},
        ]
    )
    assert list(inflections.keys()) == ["cfo"]

    prompt = _TD_MOD._build_analysis_prompt(
        inflections=inflections,
        decision_log=[{"tick": 1, "decision_text": "Clarify governance."}],
        stakeholder_map=[
            "not_a_dict",
            {"role": ["cfo"], "persona": "bad"},
            {"role": "cfo", "persona": "Guards capital discipline."},
        ],
    )
    assert "## Stakeholder: cfo" in prompt
    assert "Guards capital discipline." in prompt


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_handles_unhashable_stance_whitespace_role_and_baseline_int_styles():
    """Regression: unhashable stance, whitespace-only role, and int baseline styles must not crash or emit non-str styles."""
    ctx_telemetry = _make_ctx(
        {
            "stakeholder_map": [{"id": "cfo", "role": "cfo", "tier": 1}],
            "echo_voice_log": [
                {"tick": 1, "role": "   ", "sentiment": 0.0, "stance": "neutral"},
                {"tick": 1, "role": "cfo", "sentiment": -0.2, "stance": ["neutral"]},
                {"tick": 2, "role": "cfo", "sentiment": -0.3, "stance": {"a": 1}},
            ],
            "decision_log": [{"tick": 1, "decision_text": "Review plan."}],
        }
    )
    res_tel = await emit_stakeholder_psychology(ctx_telemetry)
    assert res_tel["status"] == "success"
    assert [p["stakeholder_id"] for p in res_tel["stakeholder_psychology"]["profiles"]] == ["cfo"]

    ctx_baseline = _make_ctx(
        {
            "stakeholder_map": [
                {
                    "id": "cfo",
                    "role": "cfo",
                    "tier": 1,
                    "decision_style": 123,
                    "communication_style": 456,
                    "risk_tolerance": 789,
                }
            ],
            "echo_voice_log": [],
            "decision_log": [],
        }
    )
    res_base = await emit_stakeholder_psychology(ctx_baseline)
    assert res_base["status"] == "success"
    profile = res_base["stakeholder_psychology"]["profiles"][0]
    assert profile["decision_style"] == "123"
    assert profile["communication_style"] == "456"
    assert profile["risk_tolerance"] == "789"
    assert isinstance(profile["decision_style"], str)
    assert isinstance(profile["communication_style"], str)
    assert isinstance(profile["risk_tolerance"], str)


def test_classify_habermas_gap_handles_non_iterable_and_bare_string_principles():
    """Regression: direct _classify_habermas_gap calls must not crash on int/None or char-split bare str principles."""
    classify = _TD_MOD._classify_habermas_gap

    # Non-iterable principles and non-str/None args must not raise TypeError
    gap_int = classify(123, "cfo", "roi", -0.3)
    assert isinstance(gap_int, str) and " → " in gap_int

    gap_none = classify(None, None, None, "not-a-float")
    assert gap_none == "system language → lifeworld friction"

    gap_bool = classify(True, 123, 456, None)
    assert gap_bool == "system language → lifeworld friction"

    # Bare string principle must match identically to single-element list (no char-splitting into 'c o s t')
    assert classify("cost", "general_lead", "minor note", -0.1) == classify(
        ["cost"], "general_lead", "minor note", -0.1
    )
    assert classify("cost", "general_lead", "minor note", -0.1) == (
        "strategic optimization → workforce dispensability"
    )


def test_find_inflection_points_and_prompt_and_diagnostic_handle_non_collection_args():
    """Regression: direct helper calls must guard non-list/non-dict inputs without TypeError or AttributeError."""
    find_inflections = _TD_MOD._find_inflection_points
    build_prompt = _TD_MOD._build_analysis_prompt
    gen_diag = _TD_MOD._generate_habermas_diagnostic

    assert find_inflections(None) == {}
    assert find_inflections(123) == {}
    assert find_inflections("not_a_list") == {}
    assert find_inflections({"cfo": []}) == {}

    assert build_prompt(None, [], []) == ""
    assert build_prompt(123, [], []) == ""
    assert build_prompt(None, None, None) == ""

    diag_none = gen_diag(None, None, None)
    assert diag_none["gaps"] == []
    assert isinstance(diag_none["summary"], str)

    diag_int = gen_diag(123, 123, 123)
    assert diag_int["gaps"] == []
    assert isinstance(diag_int["summary"], str)

    inf = {"cfo": [{"tick": 1, "sentiment": -0.3, "stance": "skeptical"}]}
    diag_mixed = gen_diag(inf, [], 123)
    assert len(diag_mixed["gaps"]) == 1
    assert diag_mixed["gaps"][0]["stakeholder_id"] == "cfo"

    diag_bad_dlog = gen_diag(inf, 123, None)
    assert len(diag_bad_dlog["gaps"]) == 1
    assert diag_bad_dlog["gaps"][0]["stakeholder_id"] == "cfo"


def test_sentiment_trajectory_and_avg_inner_sentiment_handle_non_list_inputs():
    """Regression: _compute_sentiment_trajectory and _avg_inner_sentiment_for_role handle non-list logs and bad roles."""
    compute_traj = _TD_MOD._compute_sentiment_trajectory
    avg_inner = _TD_MOD._avg_inner_sentiment_for_role

    expected_empty_traj = {
        "initial": 0.0,
        "final": 0.0,
        "min": 0.0,
        "max": 0.0,
        "max_delta_tick": 0,
        "trend": "flat",
    }

    assert compute_traj(None, "cfo") == expected_empty_traj
    assert compute_traj(123, "cfo") == expected_empty_traj
    assert compute_traj([None, "bad", {"role": None, "sentiment": -0.5}], None) == expected_empty_traj
    assert compute_traj([{"role": "cfo", "sentiment": 0.5}], 123) == expected_empty_traj

    assert avg_inner(None, "cfo") == 0.0
    assert avg_inner(123, "cfo") == 0.0
    assert avg_inner([None, "bad", {"role": None, "stance": "hostile"}], None) == 0.0
    assert avg_inner([{"role": "cfo", "stance": "hostile"}], 123) == 0.0


@pytest.mark.asyncio
async def test_emit_stakeholder_psychology_handles_backslash_stakeholder_labels():
    """Regression: backslash sequences in stakeholder_label must not trigger re.PatternError."""
    ctx = _make_ctx(
        {
            "stakeholder_map": [
                {"id": "cfo", "role": "cfo", "label": "CFO\\1", "tier": 1},
                {"id": "ops_team", "role": "ops_team", "label": "Ops\\Team", "tier": 2},
                {"id": "lead", "role": "lead", "label": "Lead\\", "tier": 2},
            ],
            "echo_voice_log": [
                {
                    "tick": 1,
                    "role": "cfo",
                    "sentiment": -0.3,
                    "stance": "hostile",
                    "notable_event": "cfo shifts from neutral to hostile",
                },
                {
                    "tick": 1,
                    "role": "ops_team",
                    "sentiment": -0.2,
                    "stance": "resistant",
                    "notable_event": "ops_team shifts from neutral to resistant",
                },
                {
                    "tick": 1,
                    "role": "lead",
                    "sentiment": 0.4,
                    "stance": "aligned",
                    "autonomous_action": "lead aligned with rollout",
                },
            ],
            "decision_log": [],
        }
    )

    res = await emit_stakeholder_psychology(ctx)
    assert res["status"] == "success"
    profiles = {p["stakeholder_id"]: p for p in res["stakeholder_psychology"]["profiles"]}
    assert profiles["cfo"]["escalation_events"] == ["CFO\\1 shifts from neutral to hostile"]
    assert profiles["ops_team"]["escalation_events"] == ["Ops\\Team shifts from neutral to resistant"]
    assert profiles["lead"]["advocacy_actions"] == ["Lead\\ aligned with rollout"]


def test_find_inflection_points_returns_empty_for_non_positive_top_n():
    """Regression: top_n <= 0 must consistently return {} even when deltas are non-empty."""
    find_inflections = _TD_MOD._find_inflection_points
    log = [
        {"role": "cfo", "tick": 1, "sentiment": 0.1},
        {"role": "cfo", "tick": 2, "sentiment": 0.9},
    ]
    assert find_inflections(log, top_n=0) == {}
    assert find_inflections(log, top_n=-1) == {}
    assert find_inflections([{"role": "cfo", "tick": 1, "sentiment": 0.1}], top_n=0) == {}
    assert find_inflections([{"role": "cfo", "tick": 1, "sentiment": 0.1}], top_n=-1) == {}



