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

"""Tests for :mod:`agents.utils.tenant_paths` (Phase 3)."""

from __future__ import annotations


import pytest

from agents.utils import tenant_paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear every Phase-3 env var so each test starts from a known baseline."""
    for var in ("SAAS_MODE", "RACE_RUNS_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# resolve_tenant_id
# ---------------------------------------------------------------------------


class TestResolveTenantID:
    """Since DISPATCHER_ORG_ID was removed for user_id multi-tenant,
    the fallback is always returned verbatim.
    """

    def test_returns_fallback(self):
        assert tenant_paths.resolve_tenant_id(fallback="user-alice") == "user-alice"

    def test_none_fallback_returns_empty(self):
        assert tenant_paths.resolve_tenant_id(fallback=None) == ""


# ---------------------------------------------------------------------------
# runs_base_dir
# ---------------------------------------------------------------------------


class TestRunsBaseDir:
    def test_default_points_at_repo_data_dir(self):
        base = tenant_paths.runs_base_dir()
        assert base.name == "simulation_runs"
        assert base.parent.name == "data"

    def test_env_override_takes_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
        assert tenant_paths.runs_base_dir() == tmp_path

    def test_env_override_blank_is_ignored(self, monkeypatch):
        monkeypatch.setenv("RACE_RUNS_DIR", "   ")
        # Stripped to empty → falls back to default.
        assert tenant_paths.runs_base_dir().name == "simulation_runs"


# ---------------------------------------------------------------------------
# path constructors
# ---------------------------------------------------------------------------


class TestPathConstructors:
    def test_tenant_runs_dir_appends_tenant(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
        assert tenant_paths.tenant_runs_dir("acme") == tmp_path / "acme"

    def test_tenant_runs_dir_rejects_empty(self):
        with pytest.raises(ValueError):
            tenant_paths.tenant_runs_dir("")

    def test_run_path_format(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
        assert tenant_paths.run_path("acme", "sim-123") == tmp_path / "acme" / "sim-123.json"

    def test_run_path_rejects_empty_sim_id(self):
        with pytest.raises(ValueError):
            tenant_paths.run_path("acme", "")

    def test_summary_path_format(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
        assert tenant_paths.summary_path("acme") == tmp_path / "acme" / "runs.ndjson"


# ---------------------------------------------------------------------------
# Cross-language byte-identity canary (with internal/tenant in Go)
# ---------------------------------------------------------------------------


class TestLegacyByteIdentical:
    """Pins the exact filesystem layout under SAAS_MODE=off.

    A drift here would silently start writing today's single-tenant
    runs into a different directory and break the existing
    longitudinal store. Hard-coded paths are intentional.
    """

    def test_off_anonymous_subdir_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
        tenant = tenant_paths.resolve_tenant_id(fallback="anonymous")
        assert tenant == "anonymous"
        assert tenant_paths.run_path(tenant, "sim-xyz") == tmp_path / "anonymous" / "sim-xyz.json"

    def test_off_per_participant_subdir_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RACE_RUNS_DIR", str(tmp_path))
        tenant = tenant_paths.resolve_tenant_id(fallback="user-alice")
        assert tenant == "user-alice"
        assert tenant_paths.summary_path(tenant) == tmp_path / "user-alice" / "runs.ndjson"
