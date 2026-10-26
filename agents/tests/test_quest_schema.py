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

"""Validate all quest YAML files against the Pydantic schema.

Ensures every YAML in configs/quests/ loads cleanly through the
quest_loader schema and passes structural invariants.
"""

import pytest

from agents.simulator.quest_loader import load_quest_from_yaml, DEFAULT_QUEST_DIR


QUEST_DIR = DEFAULT_QUEST_DIR
QUEST_FILES = sorted(QUEST_DIR.glob("*.yaml"))


@pytest.fixture(params=QUEST_FILES, ids=[f.stem for f in QUEST_FILES])
def quest_file(request):
    return request.param


class TestAllQuestSchemas:
    """Every YAML in configs/quests/ must parse through the schema."""

    def test_quest_loads_and_validates(self, quest_file):
        """Quest file must load without errors."""
        quest = load_quest_from_yaml(quest_file.stem)
        assert quest.id is not None
        assert len(quest.id) > 0

    def test_quest_has_minimum_echoes(self, quest_file):
        """Each quest must have at least 4 echoes."""
        quest = load_quest_from_yaml(quest_file.stem)
        assert len(quest.echoes) >= 4, f"{quest.id} has only {len(quest.echoes)} echoes"

    def test_quest_has_all_tiers(self, quest_file):
        """Each quest must have echoes across all 3 tiers."""
        quest = load_quest_from_yaml(quest_file.stem)
        tiers = {e.tier for e in quest.echoes}
        assert 1 in tiers, f"{quest.id} missing Tier 1 echoes"
        assert 2 in tiers, f"{quest.id} missing Tier 2 echoes"
        assert 3 in tiers, f"{quest.id} missing Tier 3 echoes"

    def test_quest_has_7_principles(self, quest_file):
        """Each quest must have exactly 7 principles."""
        quest = load_quest_from_yaml(quest_file.stem)
        assert len(quest.principles) == 7, f"{quest.id} has {len(quest.principles)} principles"

    def test_quest_has_4_resources(self, quest_file):
        """Each quest must define LAU, BT, TBU, OWB."""
        quest = load_quest_from_yaml(quest_file.stem)
        expected = {"LAU", "BT", "TBU", "OWB"}
        assert set(quest.resources.keys()) == expected, (
            f"{quest.id} resources: {set(quest.resources.keys())} != {expected}"
        )

    def test_quest_echo_roles_unique(self, quest_file):
        """Echo roles must be unique within a quest."""
        quest = load_quest_from_yaml(quest_file.stem)
        roles = [e.role for e in quest.echoes]
        assert len(roles) == len(set(roles)), f"{quest.id} has duplicate roles: {roles}"

    def test_quest_has_executive_coach(self, quest_file):
        """Every quest must include the always-on executive_coach echo."""
        quest = load_quest_from_yaml(quest_file.stem)
        roles = {e.role for e in quest.echoes}
        assert "executive_coach" in roles, f"{quest.id} is missing executive_coach echo"

    def test_executive_coach_is_tier_1(self, quest_file):
        """The executive_coach must be Tier 1 (full LLM)."""
        quest = load_quest_from_yaml(quest_file.stem)
        coaches = [e for e in quest.echoes if e.role == "executive_coach"]
        assert len(coaches) == 1
        assert coaches[0].tier == 1, f"executive_coach must be Tier 1, got {coaches[0].tier}"


# ---------------------------------------------------------------------------
# Step 7: Phase 3 → Phase 4 citation passthrough on EchoConfig
# ---------------------------------------------------------------------------


class TestEchoConfigCitationFields:
    """EchoConfig must accept (but not require) Phase 3 citation fields
    so the leadership_planner can attach `cited_node_ids` and
    `source_doc_ids` to retrieved personas while existing template
    quests continue to validate unchanged."""

    def _minimal_echo(self, **overrides):
        """Build a minimal valid EchoConfig dict for these tests."""
        base = {
            "role": "vp_procurement",
            "tier": 2,
            "persona": "Sam VP",
            "domain_principles": [],
            "affinity": [0.5] * 7,
        }
        base.update(overrides)
        return base

    def test_defaults_are_empty_lists(self):
        from agents.simulator.quest_loader import EchoConfig

        echo = EchoConfig(**self._minimal_echo())
        assert echo.cited_node_ids == []
        assert echo.source_doc_ids == []

    def test_accepts_node_id_and_doc_id_lists(self):
        from agents.simulator.quest_loader import EchoConfig

        echo = EchoConfig(
            **self._minimal_echo(
                cited_node_ids=["n-actor-1", "n-belief-1"],
                source_doc_ids=["doc-1", "doc-2"],
            )
        )
        assert echo.cited_node_ids == ["n-actor-1", "n-belief-1"]
        assert echo.source_doc_ids == ["doc-1", "doc-2"]

    def test_existing_quests_validate_without_citation_fields(self):
        """The four canonical-shape fields stay optional; all checked-in
        YAML quests already passed the schema tests above without
        them — this is a direct guard against accidental future
        promotion of citation fields to required."""
        from agents.simulator.quest_loader import EchoConfig

        # No citation fields supplied at all.
        echo = EchoConfig(**self._minimal_echo())
        dumped = echo.model_dump()
        # model_dump still emits the keys (with empty defaults) so the
        # tick passthrough has a stable shape to read.
        assert dumped["cited_node_ids"] == []
        assert dumped["source_doc_ids"] == []
