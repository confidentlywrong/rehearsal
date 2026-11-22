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

"""Round 3 regression tests for scripts/ops, scripts/e2e, scripts/bench, and scripts/deploy."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestEmergencyFlushR3:
    """Regression checks for scripts/ops/emergency_flush.py."""

    def test_normalize_async_db_url(self) -> None:
        from scripts.ops import emergency_flush

        assert (
            emergency_flush._normalize_async_db_url("postgres://u:p@h:5432/db")
            == "postgresql+asyncpg://u:p@h:5432/db"
        )
        assert (
            emergency_flush._normalize_async_db_url("postgresql://u:p@h:5432/db")
            == "postgresql+asyncpg://u:p@h:5432/db"
        )
        assert (
            emergency_flush._normalize_async_db_url("postgresql+asyncpg://u:p@h:5432/db")
            == "postgresql+asyncpg://u:p@h:5432/db"
        )

    def test_flush_db_uses_savepoints_on_missing_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.ops import emergency_flush

        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:9213/testdb")

        executed_sql: list[str] = []
        created_urls: list[str] = []

        class _FakeResult:
            def scalar(self) -> int:
                return 3

        class _FakeConn:
            @asynccontextmanager
            async def begin_nested(self):
                yield

            async def execute(self, stmt):
                sql = str(stmt)
                executed_sql.append(sql)
                if "FROM events" in sql:
                    raise RuntimeError("relation events does not exist")
                return _FakeResult()

        class _FakeEngine:
            @asynccontextmanager
            async def begin(self):
                yield _FakeConn()

            async def dispose(self) -> None:
                return None

        def _fake_create_engine(url: str):
            created_urls.append(url)
            return _FakeEngine()

        with patch("sqlalchemy.ext.asyncio.create_async_engine", side_effect=_fake_create_engine):
            ok = asyncio.run(emergency_flush.flush_db())

        assert ok is True
        assert created_urls == ["postgresql+asyncpg://u:p@127.0.0.1:9213/testdb"]
        truncate_stmts = [s for s in executed_sql if s.startswith("TRUNCATE TABLE")]
        assert truncate_stmts == ["TRUNCATE TABLE sessions, app_states, user_states CASCADE"]

    def test_require_project_rejects_placeholder_and_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.ops import emergency_flush

        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("PROJECT_ID", raising=False)
        monkeypatch.delenv("PUBSUB_PROJECT_ID", raising=False)

        with pytest.raises(ValueError):
            emergency_flush._require_project()

        assert emergency_flush.flush_pubsub() is False
        with pytest.raises(ValueError):
            emergency_flush.kill_instances()
        with pytest.raises(ValueError):
            emergency_flush.revive_instances()


class TestDeployCleanupR3:
    """Verify staging_dir is cleaned up even if deploy fails before chdir."""

    def test_deploy_agent_engine_cleans_staging_dir_on_early_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.deploy import deploy

        staging = tmp_path / "staging"
        staging.mkdir()
        monkeypatch.setattr(sys, "modules", {**sys.modules, "vertexai": MagicMock()})

        with (
            patch.object(deploy, "_read_requirements", return_value=[]),
            patch.object(deploy, "_pin_all_requirements", return_value=[]),
            patch.object(deploy, "_stage_extra_packages", return_value=(str(staging), ["agents"])),
            patch.object(deploy, "_compute_staging_fingerprint", side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                deploy.deploy_agent_engine(
                    "planner",
                    {"module": "agents.planner.agent", "attr": "a2a_agent", "path": "agents/planner"},
                    tf={"project_id": "proj-1", "region": "us-central1"},
                )

        assert not staging.exists()


class TestScriptsPortsAndSafetyR3:
    """Regression checks for E2E, bench, diagnostics, and shell scripts."""

    def test_e2e_dispatch_script_syntax_and_gateway_cmd(self) -> None:
        script = _REPO_ROOT / "scripts" / "e2e" / "e2e_dispatch_test.sh"
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

        content = script.read_text()
        assert "go run ./cmd/gateway" in content
        assert "go run cmd/gateway/main.go" not in content
        assert "GATEWAY_PORT:-9101" in content
        assert "REDIS_PORT:-9102" in content
        assert "REDIS_PASSWORD" in content

    def test_test_runner_handles_non_json_error_and_empty_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.e2e import test_runner

        bad_resp = httpx.Response(
            status_code=502,
            content=b"<html>Bad Gateway</html>",
            request=httpx.Request("POST", "http://127.0.0.1:9104/a2a/runner/"),
        )
        res = test_runner._event_result_from_a2a_response(bad_resp, "tick", 12.5)
        assert res.success is False
        assert res.error is not None and res.error.startswith("HTTP 502:")

        async def _empty_gateway(*args, **kwargs):
            return []

        monkeypatch.setattr(test_runner, "run_gateway", _empty_gateway)
        monkeypatch.setattr(
            sys,
            "argv",
            ["test_runner.py", "--mode", "gateway", "--target", "http://127.0.0.1:9101"],
        )
        with pytest.raises(SystemExit) as exc_info:
            asyncio.run(test_runner.main())
        assert exc_info.value.code == 1

    def test_default_ports_match_local_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("GATEWAY_URL", "PLANNER_URL", "DATABASE_URL"):
            monkeypatch.delenv(var, raising=False)

        for mod_name, attr, expected in (
            ("scripts.e2e.e2e_fanout_test", "GATEWAY_HTTP_URL", "http://127.0.0.1:9101"),
            ("scripts.e2e.e2e_scale_test", "GATEWAY_HTTP_URL", "http://127.0.0.1:9101"),
            ("scripts.e2e.e2e_simulation_test", "GATEWAY_HTTP_URL", "http://127.0.0.1:9101"),
            ("scripts.e2e.e2e_simulation_test", "PLANNER_URL", "http://127.0.0.1:9106"),
            ("scripts.bench.perf_diagnostic", "GATEWAY_HTTP_URL", "http://127.0.0.1:9101"),
            ("scripts.diagnostics.demo1_harness", "GATEWAY_HTTP_URL", "http://127.0.0.1:9101"),
            (
                "scripts.bench.bench_concurrency",
                "DB_URL",
                "postgresql+asyncpg://testuser:testpass@127.0.0.1:9213/testdb",
            ),
            (
                "scripts.bench.stress_test_db",
                "DB_URL",
                "postgresql+asyncpg://testuser:testpass@127.0.0.1:9213/testdb",
            ),
        ):
            mod = importlib.reload(importlib.import_module(mod_name))
            assert getattr(mod, attr) == expected, f"{mod_name}.{attr}"

    def test_no_script_kills_host_ollama_port_11434(self) -> None:
        scripts_dir = _REPO_ROOT / "scripts"
        for path in list(scripts_dir.rglob("*.sh")) + list(scripts_dir.rglob("*.py")):
            rel_parts = path.relative_to(scripts_dir).parts
            if "tests" in rel_parts or "e2e_hardening" in rel_parts:
                continue
            text = path.read_text(errors="ignore")
            assert "11434" not in text or "kill" not in text.lower(), f"Unsafe 11434 reference in {path}"
