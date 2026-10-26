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

"""Test fixtures for leadership_planner_with_memory tests.

The root ``agents/conftest.py`` already pins GOOGLE_CLOUD_PROJECT,
GOOGLE_CLOUD_AGENT_ENGINE_ID, ALLOYDB_HOST, etc. before any agent import,
so this conftest only needs to add the ALLOYDB_PASSWORD env var (so
store_alloydb._resolve_password() does not reach Secret Manager) and a
fixture that swaps the module-level _store singleton for an in-memory one.

Mirrors agents/planner_with_memory/tests/conftest.py.
"""

import os

# Required by store_alloydb._resolve_password() so it short-circuits before
# reaching the Secret Manager client.
os.environ.setdefault("ALLOYDB_PASSWORD", "test")

import pytest  # noqa: E402

from agents.leadership_planner_with_memory.memory.store import (  # noqa: E402
    ScenarioMemoryStore,
)


@pytest.fixture
def in_memory_store(monkeypatch):
    """Swap ``tools._store`` for an in-memory ``ScenarioMemoryStore``."""
    from agents.leadership_planner_with_memory.memory import tools as _tools

    store = ScenarioMemoryStore()
    monkeypatch.setattr(_tools, "_store", store)
    return store

