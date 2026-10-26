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

"""Tests for the live dashboard A2UI voices section."""

from agents.simulator.live_dashboard import build_live_dashboard

# Minimal valid kwargs for build_live_dashboard
_BASE_KWARGS = {
    "tick": 5,
    "max_ticks": 12,
    "completion_pct": 42.0,
    "resource_state": {
        "lau": {"current": 150, "critical_threshold": 30},
        "bt": {"current": 800, "critical_threshold": 100},
        "tbu": {"current": 45, "critical_threshold": 10},
        "owb": {"current": 8, "critical_threshold": 5},
    },
    "resource_deltas": {
        "tbu": {"delta": 1},
        "owb": {"delta": -2},
    },
    "status_counts": {"running": 3, "collapsed": 1},
    "persona_status_counts": {"disruptor": {"collapsed": 1}},
    "infrastructure": {
        "medical_saves_this_tick": 0,
        "avg_comfort": 0,
        "friction_level": "moderate",
    },
    "active_alerts_count": 1,
    "avg_velocity": 3.5,
    "avg_water": 45.0,
}


def _find_component(result: dict, comp_id: str) -> dict | None:
    """Find a component by ID in the surfaceUpdate components list."""
    for comp in result["surfaceUpdate"]["components"]:
        if comp["id"] == comp_id:
            return comp
    return None


def _get_child_ids(result: dict) -> list[str]:
    """Get the child IDs from the root Column component."""
    content = _find_component(result, "dash-content")
    assert content is not None, "dash-content Column missing"
    return content["component"]["Column"]["children"]["explicitList"]


class TestVoicesSection:
    """Verify the optional VOICES section in the live dashboard."""

    def test_voices_section_present_when_voices_provided(self):
        """When voices is non-empty, VOICES section appears between stakeholders and infra."""
        result = build_live_dashboard(
            **_BASE_KWARGS,
            voices=['💭 disruptor: "We need to move faster."'],
        )

        assert _find_component(result, "dash-voices-title") is not None
        assert _find_component(result, "dash-div-voices") is not None
        assert _find_component(result, "dash-voice-0") is not None

        # VOICES title should say "VOICES"
        title = _find_component(result, "dash-voices-title")
        assert title["component"]["Text"]["text"]["literalString"] == "VOICES"

    def test_voices_section_absent_when_no_voices(self):
        """When voices is None, no VOICES section appears."""
        result = build_live_dashboard(**_BASE_KWARGS, voices=None)

        assert _find_component(result, "dash-voices-title") is None
        assert _find_component(result, "dash-div-voices") is None

    def test_voices_section_absent_when_empty_list(self):
        """When voices is an empty list, no VOICES section appears."""
        result = build_live_dashboard(**_BASE_KWARGS, voices=[])

        assert _find_component(result, "dash-voices-title") is None

    def test_voices_stripped_of_thought_emoji(self):
        """The 💭 prefix should be stripped and replaced with 💬."""
        result = build_live_dashboard(
            **_BASE_KWARGS,
            voices=['💭 pragmatist: "Let\'s be realistic."'],
        )

        voice = _find_component(result, "dash-voice-0")
        assert voice is not None
        text = voice["component"]["Text"]["text"]["literalString"]
        assert text.startswith("💬")
        assert "💭" not in text
        assert "pragmatist" in text

    def test_voices_limited_to_three(self):
        """At most 3 voices should appear in the dashboard."""
        result = build_live_dashboard(
            **_BASE_KWARGS,
            voices=[
                "💭 voice 1",
                "💭 voice 2",
                "💭 voice 3",
                "💭 voice 4",
                "💭 voice 5",
            ],
        )

        assert _find_component(result, "dash-voice-0") is not None
        assert _find_component(result, "dash-voice-1") is not None
        assert _find_component(result, "dash-voice-2") is not None
        assert _find_component(result, "dash-voice-3") is None  # 4th should not exist

    def test_voices_use_caption_hint(self):
        """Voice components should use caption usageHint for subtle styling."""
        result = build_live_dashboard(
            **_BASE_KWARGS,
            voices=["💭 test voice"],
        )

        voice = _find_component(result, "dash-voice-0")
        assert voice["component"]["Text"]["usageHint"] == "caption"

    def test_voices_appear_in_correct_order(self):
        """VOICES section should appear after stakeholders, before infrastructure."""
        result = build_live_dashboard(
            **_BASE_KWARGS,
            voices=["💭 some voice"],
        )

        children = _get_child_ids(result)
        stake_idx = children.index("dash-stake-title")
        voices_idx = children.index("dash-voices-title")
        infra_idx = children.index("dash-infra-title")

        assert stake_idx < voices_idx < infra_idx


class TestDashboardBackwardsCompatibility:
    """Verify existing dashboard sections still work with new voices param."""

    def test_default_voices_none(self):
        """build_live_dashboard works without voices parameter (backwards compat)."""
        result = build_live_dashboard(**_BASE_KWARGS)

        assert "surfaceUpdate" in result
        assert "beginRendering" in result
        assert result["surfaceUpdate"]["surfaceId"] == "live-dashboard"
        assert result["beginRendering"]["root"] == "dash-card"

    def test_resources_section_still_present(self):
        """Resources section should be unaffected by voices addition."""
        result = build_live_dashboard(**_BASE_KWARGS, voices=["💭 test"])

        assert _find_component(result, "dash-res-title") is not None
        assert _find_component(result, "dash-res-tbu") is not None

    def test_infra_section_still_present(self):
        """Infrastructure section should be unaffected by voices addition."""
        result = build_live_dashboard(**_BASE_KWARGS, voices=["💭 test"])

        assert _find_component(result, "dash-infra-title") is not None
        # Change-enablement is now rendered as three separate rows (resistors
        # count, psych-safety ScoreBar, org-drag) instead of one joined line.
        assert _find_component(result, "dash-infra-resistors") is not None
        assert _find_component(result, "dash-infra-psych") is not None
        assert _find_component(result, "dash-infra-drag") is not None

    def test_alerts_section_still_present(self):
        """Active alerts section should be unaffected by voices addition."""
        result = build_live_dashboard(**_BASE_KWARGS, voices=["💭 test"])

        assert _find_component(result, "dash-alerts") is not None
