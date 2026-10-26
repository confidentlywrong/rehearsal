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

"""Tests for the ``get_selected_scenarios`` user-directed picker tool.

Uses the in-memory ``ScenarioMemoryStore`` (via the ``in_memory_store``
fixture in conftest.py) as the ``tools._store`` backend so no AlloyDB is
required.
"""

from types import SimpleNamespace

import pytest

from agents.leadership_planner_with_memory.memory import tools


def _make_ctx(state: dict | None = None) -> SimpleNamespace:
    """Build a minimal ToolContext stand-in with a mutable ``state`` dict."""
    return SimpleNamespace(state=state if state is not None else {})


@pytest.fixture(autouse=True)
def _stub_auto_summary(monkeypatch):
    """No-op — _auto_store_summary was removed in Phase 2D."""
    pass


# ---------------------------------------------------------------------------
# get_selected_scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetSelectedScenarios:

    async def test_no_selection_in_state(self, in_memory_store):
        """No selected_context_scenario_ids in state → none_selected."""
        ctx = _make_ctx()
        result = await tools.get_selected_scenarios(tool_context=ctx)
        assert result["status"] == "none_selected"
        assert result["scenarios"] == []

    async def test_empty_selection_list(self, in_memory_store):
        """Empty list in state → none_selected."""
        ctx = _make_ctx(state={"selected_context_scenario_ids": []})
        result = await tools.get_selected_scenarios(tool_context=ctx)
        assert result["status"] == "none_selected"
        assert result["scenarios"] == []

    async def test_happy_path_two_scenarios(self, in_memory_store):
        """Two valid scenario IDs → fetches both successfully."""
        sid1 = await in_memory_store.store_scenario(
            scenario_data={"title": "Budget Crisis"},
            evaluation_score=75.0,
        )
        sid2 = await in_memory_store.store_scenario(
            scenario_data={"title": "Innovation Pivot"},
            evaluation_score=88.0,
        )

        ctx = _make_ctx(state={
            "selected_context_scenario_ids": [sid1, sid2],
        })
        result = await tools.get_selected_scenarios(tool_context=ctx)

        assert result["status"] == "success"
        assert result["count"] == 2
        assert len(result["scenarios"]) == 2

    async def test_missing_scenario_id_skipped(self, in_memory_store):
        """One valid ID + one nonexistent → returns only the valid one."""
        sid1 = await in_memory_store.store_scenario(
            scenario_data={"title": "Real Scenario"},
            evaluation_score=60.0,
        )

        ctx = _make_ctx(state={
            "selected_context_scenario_ids": [sid1, "nonexistent-id-123"],
        })
        result = await tools.get_selected_scenarios(tool_context=ctx)

        assert result["status"] == "success"
        assert result["count"] == 1
        assert len(result["scenarios"]) == 1

    async def test_all_ids_missing(self, in_memory_store):
        """All IDs are invalid → none_selected (all resolved to None)."""
        ctx = _make_ctx(state={
            "selected_context_scenario_ids": ["bad-1", "bad-2"],
        })
        result = await tools.get_selected_scenarios(tool_context=ctx)

        assert result["status"] == "none_selected"
        assert result["scenarios"] == []

    async def test_non_list_value_in_state(self, in_memory_store):
        """Non-list value in state → treated as no selection."""
        ctx = _make_ctx(state={
            "selected_context_scenario_ids": "not-a-list",
        })
        result = await tools.get_selected_scenarios(tool_context=ctx)
        assert result["status"] == "none_selected"

    async def test_blank_ids_filtered(self, in_memory_store):
        """Empty strings and non-string values in the list are skipped."""
        sid = await in_memory_store.store_scenario(
            scenario_data={"title": "Valid"},
            evaluation_score=50.0,
        )
        ctx = _make_ctx(state={
            "selected_context_scenario_ids": ["", "  ", None, sid],
        })
        result = await tools.get_selected_scenarios(tool_context=ctx)

        assert result["status"] == "success"
        assert result["count"] == 1

    async def test_readonly_state_does_not_crash(self):
        """Read-only state (AttributeError on .get) → none_selected."""
        ctx = SimpleNamespace(state=None)
        result = await tools.get_selected_scenarios(tool_context=ctx)
        assert result["status"] == "none_selected"

    async def test_explicit_scenario_ids_override_state(self, in_memory_store):
        """Explicit scenario_ids param takes precedence over state."""
        sid1 = await in_memory_store.store_scenario(
            scenario_data={"title": "From param"},
            evaluation_score=70.0,
        )
        sid_state = await in_memory_store.store_scenario(
            scenario_data={"title": "From state"},
            evaluation_score=80.0,
        )

        # State has sid_state, but explicit param has sid1 — param wins.
        ctx = _make_ctx(state={
            "selected_context_scenario_ids": [sid_state],
        })
        result = await tools.get_selected_scenarios(
            tool_context=ctx, scenario_ids=[sid1]
        )

        assert result["status"] == "success"
        assert result["count"] == 1
        # Should contain the explicitly passed scenario, not the state one.
        assert result["scenarios"][0]["scenario_id"] == sid1

    async def test_explicit_empty_list_falls_through_to_state(self, in_memory_store):
        """Empty explicit list falls back to state."""
        sid = await in_memory_store.store_scenario(
            scenario_data={"title": "State fallback"},
            evaluation_score=65.0,
        )
        ctx = _make_ctx(state={
            "selected_context_scenario_ids": [sid],
        })
        result = await tools.get_selected_scenarios(
            tool_context=ctx, scenario_ids=[]
        )

        assert result["status"] == "success"
        assert result["count"] == 1

