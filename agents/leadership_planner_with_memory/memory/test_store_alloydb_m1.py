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

"""Targeted M1 regression tests for leadership AlloyDB store, tools, and SQL assets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from agents.leadership_planner_with_memory.memory import store_alloydb as ls_db
from agents.leadership_planner_with_memory.memory import tools as ls_tools


_REPO_ROOT = Path(__file__).resolve().parents[3]


class _NoGetRecord:
    """asyncpg.Record stub exposing __getitem__ and keys() without .get()."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = dict(mapping)

    def keys(self):
        return self._mapping.keys()

    def __getitem__(self, key: str):
        return self._mapping[key]


def _make_fake_pool(conn: MagicMock) -> MagicMock:
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


class TestLeadershipStoreAlloyDB:
    def test_row_to_scenario_handles_record_without_get_and_optional_cols(self):
        created = datetime(2026, 3, 1, 12, 0, 0)  # naive datetime
        row = _NoGetRecord(
            {
                "scenario_id": "sc-1",
                "scenario_data": '{"name": "Budget Cut"}',
                "created_at": created,
                "eval_score": 82.0,
                "eval_result": '{"overall_score": 82}',
            }
        )
        sc = ls_db._row_to_scenario(row)  # type: ignore[arg-type]
        assert sc.scenario_id == "sc-1"
        assert sc.scenario_data == {"name": "Budget Cut"}
        assert sc.created_at.tzinfo == timezone.utc
        assert sc.evaluation_result == {"overall_score": 82}
        assert sc.persona is None
        assert sc.owner_id is None

        row_with_opt = _NoGetRecord(
            {
                "scenario_id": "sc-2",
                "scenario_data": {"name": "Reorg"},
                "created_at": created.replace(tzinfo=timezone.utc),
                "eval_score": None,
                "eval_result": "{bad json",
                "persona": "cfo",
                "owner_id": "user-123",
            }
        )
        sc2 = ls_db._row_to_scenario(row_with_opt)  # type: ignore[arg-type]
        assert sc2.persona == "cfo"
        assert sc2.owner_id == "user-123"
        assert sc2.evaluation_result is None

    def test_row_to_scenario_rejects_null_or_corrupt_or_nondict_scenario_data(self):
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        for bad_val in (None, "{not-valid-json", b"{not-valid-json", "null", "[]", b"[]", [], "42"):
            row = _NoGetRecord(
                {
                    "scenario_id": "sc-bad",
                    "scenario_data": bad_val,
                    "created_at": now,
                    "eval_score": None,
                    "eval_result": None,
                }
            )
            with pytest.raises(ValueError):
                ls_db._row_to_scenario(row)  # type: ignore[arg-type]

        bytes_row = _NoGetRecord(
            {
                "scenario_id": "sc-bytes",
                "scenario_data": b'{"name": "Bytes Scenario"}',
                "created_at": now,
                "eval_score": 90.0,
                "eval_result": bytearray(b'{"overall_score": 90}'),
            }
        )
        sc = ls_db._row_to_scenario(bytes_row)  # type: ignore[arg-type]
        assert sc.scenario_data == {"name": "Bytes Scenario"}
        assert sc.evaluation_result == {"overall_score": 90}

    @pytest.mark.asyncio
    async def test_row_to_scenario_ignores_non_dict_eval_result_and_activates_cleanly(self, caplog):
        now = datetime(2026, 4, 1, tzinfo=timezone.utc)
        for bad_eval in ("[1, 2]", '"oops"', "123", "true", b"[1, 2]"):
            caplog.clear()
            row = _NoGetRecord(
                {
                    "scenario_id": "sc-nondict",
                    "scenario_data": '{"name": "Crisis"}',
                    "created_at": now,
                    "eval_score": 85.0,
                    "eval_result": bad_eval,
                }
            )
            sc = ls_db._row_to_scenario(row)  # type: ignore[arg-type]
            assert sc.evaluation_result is None
            assert "Ignoring non-dict eval_result JSONB" in caplog.text

            mock_store = MagicMock()
            mock_store.get_scenario = AsyncMock(return_value=sc)
            ctx = MagicMock()
            ctx.state = {}
            with patch.object(ls_tools, "_store", mock_store):
                res = await ls_tools.get_scenario("sc-nondict", tool_context=ctx, activate_scenario=True)

            assert res["status"] == "success"
            assert res["activated"] is True
            assert ctx.state["leadership_evaluation"] == {"overall_score": 85.0}

    def test_get_dsn_rejects_whitespace_host_and_strips_valid_host(self, monkeypatch):
        monkeypatch.setenv("ALLOYDB_PASSWORD", "pw")
        monkeypatch.setenv("ALLOYDB_HOST", "   ")
        with pytest.raises(ValueError, match="ALLOYDB_HOST"):
            ls_db._get_dsn()

        monkeypatch.setenv("ALLOYDB_HOST", " 127.0.0.1 ")
        assert ls_db._get_dsn() == "postgresql://postgres:pw@127.0.0.1:5432/postgres"

    @pytest.mark.asyncio
    async def test_load_simulations_handles_bytes_and_corrupt_jsonb(self, caplog):
        now = datetime(2026, 4, 1, 12, 0, 0)
        conn = MagicMock()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "simulation_id": "sim-1",
                    "scenario_id": "sc-1",
                    "sim_result": b'{"k": 1}',
                    "simulated_at": now,
                },
                {
                    "simulation_id": "sim-2",
                    "scenario_id": "sc-1",
                    "sim_result": bytearray(b'{"k": 2}'),
                    "simulated_at": now,
                },
                {
                    "simulation_id": "sim-3",
                    "scenario_id": "sc-1",
                    "sim_result": "{corrupt_sim_json",
                    "simulated_at": now,
                },
                {
                    "simulation_id": "sim-4",
                    "scenario_id": "sc-1",
                    "sim_result": b"{corrupt_bytes",
                    "simulated_at": now,
                },
                {
                    "simulation_id": "sim-5",
                    "scenario_id": "sc-1",
                    "sim_result": "[]",
                    "simulated_at": now,
                },
            ]
        )
        records = await ls_db._load_simulations(conn, "sc-1")
        assert [r.simulation_result for r in records] == [{"k": 1}, {"k": 2}, {}, {}, {}]
        assert all(r.simulated_at.tzinfo == timezone.utc for r in records)
        assert "JSONB decode failed" in caplog.text

    def test_validate_schema_name_and_secret_name(self, monkeypatch):
        assert ls_db._validate_schema_name(None) == "public"
        assert ls_db._validate_schema_name("") == "public"
        assert ls_db._validate_schema_name("  analytics_1 ") == "analytics_1"
        with pytest.raises(ValueError, match="Invalid ALLOYDB_SCHEMA"):
            ls_db._validate_schema_name("bad;drop")

        monkeypatch.delenv("ALLOYDB_PASSWORD", raising=False)
        monkeypatch.setenv("ALLOYDB_SECRET_NAME", "custom-db-secret")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")
        monkeypatch.setattr(ls_db, "_cached", None)

        mock_client = MagicMock()
        mock_client.access_secret_version.return_value = SimpleNamespace(
            payload=SimpleNamespace(data=b"s3cr3t\n")
        )
        with patch.object(ls_db, "_get_sm_client", return_value=mock_client):
            pw = ls_db._resolve_password()

        assert pw == "s3cr3t"
        mock_client.access_secret_version.assert_called_once_with(
            name="projects/proj-1/secrets/custom-db-secret/versions/latest"
        )
        monkeypatch.setattr(ls_db, "_cached", None)

    @pytest.mark.asyncio
    async def test_list_scenario_bundles_limit_zero_and_utc_and_six_keys(self):
        store = ls_db.AlloyDBScenarioStore()
        with patch.object(ls_db, "_get_pool", AsyncMock()) as mock_get_pool:
            assert await store.list_scenario_bundles(limit=0) == []
            assert await store.list_scenario_bundles(limit=-5) == []
            mock_get_pool.assert_not_awaited()

        naive = datetime(2026, 4, 2, 10, 0, 0)
        fake_row = {
            "scenario_id": "sc-1",
            "eval_score": 88.0,
            "created_at": naive,
            "stakeholder_graph": '{"nodes": []}',
            "stakeholder_personas": "[]",
            "stakeholder_reactions": None,
            "simulation_timeline": "{bad",
            "simulation_report": '{"summary": "ok"}',
            "stakeholder_interview": None,
        }
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[fake_row])
        pool = _make_fake_pool(conn)

        with patch.object(ls_db, "_get_pool", AsyncMock(return_value=pool)):
            bundles = await store.list_scenario_bundles(limit=10)

        assert len(bundles) == 1
        assert bundles[0]["created_at"].tzinfo == timezone.utc
        assert bundles[0]["stakeholder_graph"] == {"nodes": []}
        assert bundles[0]["stakeholder_personas"] == []
        assert bundles[0]["simulation_timeline"] is None
        assert bundles[0]["simulation_report"] == {"summary": "ok"}
        for key in ls_db.AlloyDBScenarioStore._ARTIFACT_KEYS:
            assert key in bundles[0]

    @pytest.mark.asyncio
    async def test_get_artifacts_corrupt_jsonb_returns_none_not_raw_string(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "stakeholder_graph": "{corrupt",
                "stakeholder_personas": None,
                "stakeholder_reactions": None,
                "simulation_timeline": None,
                "simulation_report": None,
                "stakeholder_interview": None,
            }
        )
        pool = _make_fake_pool(conn)
        store = ls_db.AlloyDBScenarioStore()
        with patch.object(ls_db, "_get_pool", AsyncMock(return_value=pool)):
            res = await store.get_artifacts("sc-1")
        assert res is None

    @pytest.mark.asyncio
    async def test_record_simulation_handles_asyncpg_fk_violation(self):
        conn = MagicMock()
        conn.execute = AsyncMock(
            side_effect=asyncpg.ForeignKeyViolationError("violates foreign key constraint")
        )
        pool = _make_fake_pool(conn)
        store = ls_db.AlloyDBScenarioStore()
        with patch.object(ls_db, "_get_pool", AsyncMock(return_value=pool)):
            sim_id = await store.record_simulation("missing-sc", {"score": 1})
        assert sim_id is None


class TestLeadershipToolsM1:
    @pytest.mark.asyncio
    async def test_get_selected_scenarios_distinguishes_db_error_and_partial_failures(self):
        ctx = MagicMock()
        ctx.state = {"user_id": "u1"}

        mock_store = MagicMock()
        mock_store.get_scenario = AsyncMock(side_effect=RuntimeError("DB down"))
        with patch.object(ls_tools, "_store", mock_store):
            res = await ls_tools.get_selected_scenarios(ctx, scenario_ids=["id-1", "id-2"])
        assert res["status"] == "error"
        assert res["failed_ids"] == ["id-1", "id-2"]
        assert res["scenarios"] == []

        mock_store_missing = MagicMock()
        mock_store_missing.get_scenario = AsyncMock(return_value=None)
        with patch.object(ls_tools, "_store", mock_store_missing):
            res_missing = await ls_tools.get_selected_scenarios(ctx, scenario_ids=["deleted-1"])
        assert res_missing["status"] == "none_selected"

        good_sc = SimpleNamespace(
            scenario_id="good-1",
            scenario_data={"name": "Good"},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            evaluation_score=80,
            evaluation_result={"overall_score": 80},
            persona=None,
            simulations=[],
        )
        mock_store_partial = MagicMock()
        mock_store_partial.get_scenario = AsyncMock(
            side_effect=[good_sc, RuntimeError("timeout")]
        )
        with patch.object(ls_tools, "_store", mock_store_partial):
            res_partial = await ls_tools.get_selected_scenarios(ctx, scenario_ids=["good-1", "bad-1"])
        assert res_partial["status"] == "success"
        assert res_partial["count"] == 1
        assert res_partial["failed_ids"] == ["bad-1"]

    @pytest.mark.asyncio
    async def test_store_scenario_surfaces_artifacts_autoflush_error(self):
        ctx = MagicMock()
        ctx.state = {
            "leadership_scenario": {"name": "Crisis A"},
            "leadership_evaluation": {"overall_score": 82},
            "stakeholder_graph": {"nodes": [], "edges": []},
        }
        mock_store = MagicMock()
        mock_store.store_scenario = AsyncMock(return_value="sc-new-1")
        mock_store.store_artifacts = AsyncMock(return_value=False)

        with patch.object(ls_tools, "_store", mock_store):
            res = await ls_tools.store_scenario(ctx)
        assert res["status"] == "success"
        assert res["scenario_id"] == "sc-new-1"
        assert res["artifacts_autoflushed"] == []
        assert "artifacts_autoflush_error" in res

        mock_store.store_artifacts = AsyncMock(side_effect=RuntimeError("artifact write failed"))
        with patch.object(ls_tools, "_store", mock_store):
            res_exc = await ls_tools.store_scenario(ctx)
        assert res_exc["status"] == "success"
        assert "artifact write failed" in res_exc["artifacts_autoflush_error"]

    @pytest.mark.asyncio
    async def test_get_planned_scenarios_data_clamps_limit(self, monkeypatch):
        monkeypatch.setenv("LEADERSHIP_PER_USER_ISOLATION", "on")
        ctx = MagicMock()
        ctx.session = SimpleNamespace(user_id="u1")
        ctx.state = {}
        mock_store = MagicMock()
        mock_store.recall_scenarios = AsyncMock(return_value=[])
        with patch.object(ls_tools, "_store", mock_store):
            await ls_tools.get_planned_scenarios_data(ctx, limit=999999)
        mock_store.recall_scenarios.assert_awaited_once_with(count=500, sort_by="recent", owner_id="u1")

    def test_short_id_preserves_distinct_seed_ids(self):
        shorts = {ls_tools._short_id(f"seed-leadership-00{i}") for i in range(1, 5)}
        assert len(shorts) == 4
        assert ls_tools._short_id("seed-leadership-001") == "leadership-001"
        assert ls_tools._short_id("seed-leadership-004") == "leadership-004"
        assert ls_tools._short_id("a1b2c3d4-5678-90ab") == "a1b2c3d4"


class TestLeadershipSQLAssetsParity:
    def test_schema_files_parity_and_comments(self):
        alloydb_schema = (_REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/schema.sql").read_text()
        local_schema = (_REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/schema_local.sql").read_text()
        init_schema = (_REPO_ROOT / "scripts/postgres-init/03_leadership_schema.sql").read_text()

        assert "Copyright 2026 Christian Haeussinger" in alloydb_schema
        assert "Google LLC" not in alloydb_schema
        for sql_text in (alloydb_schema, local_schema, init_schema):
            for required in (
                "leadership_scenarios",
                "leadership_simulation_records",
                "leadership_stakeholder_artifacts",
                "idx_leadership_sim_records_scenario_id",
                "idx_leadership_scenarios_owner_created",
                "idx_leadership_artifacts_owner_updated",
                "persona",
                "owner_id",
            ):
                assert required in sql_text
        assert "document_chunks" in alloydb_schema
        assert "-- 3. LEADERSHIP STAKEHOLDER ARTIFACTS TABLE" in local_schema

    def test_seed_files_set_seed_owner_id_and_coalesce(self):
        seed_local = (_REPO_ROOT / "agents/leadership_planner_with_memory/alloydb/seed_local.sql").read_text()
        fixtures = (_REPO_ROOT / "scripts/postgres-init/04_leadership_demo_fixtures.sql").read_text()
        dockerfile = (_REPO_ROOT / "infra/postgres/Dockerfile.local").read_text()

        assert "COALESCE(leadership_scenarios.owner_id, EXCLUDED.owner_id)" in seed_local
        assert "UPDATE leadership_stakeholder_artifacts" in seed_local
        assert "'__seed__'" in fixtures
        assert "COALESCE(leadership_scenarios.owner_id, EXCLUDED.owner_id)" in fixtures
        assert "COALESCE(leadership_stakeholder_artifacts.owner_id, EXCLUDED.owner_id)" in fixtures
        assert "leadership_planner_with_memory scenarios + stakeholder artifacts" in dockerfile
