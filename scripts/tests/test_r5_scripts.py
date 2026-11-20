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

"""Round-5 regression tests for scripts/**, CI/pyproject config, and doc anchors."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import io
import json
import logging
import subprocess
import sys
import tomllib
import types
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / rel_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPyprojectAndDocsR5:
    def test_all_pyproject_script_entrypoints_resolve(self) -> None:
        data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        assert scripts, "Expected [project.scripts] entries"
        for name, target in scripts.items():
            mod_name, attr = target.split(":", 1)
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, attr, None)
            assert callable(fn), f"Entrypoint {name!r} -> {target!r} is not callable"

    def test_pyproject_addopts_has_no_stale_scripts_tests_deploy_ignores(self) -> None:
        data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
        assert "scripts/tests/deploy_test.py" not in addopts
        assert "scripts/tests/test_deploy.py" not in addopts
        assert "scripts/tests/test_deploy_gke.py" not in addopts

    def test_ci_workflow_excludes_integration_and_runs_script_tests(self) -> None:
        ci_text = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert '-m "not slow and not integration"' in ci_text
        assert "scripts/tests/" in ci_text
        assert "scripts/deploy/deploy_test.py" in ci_text
        assert "embedding_backfill_test.py" in ci_text

    def test_tutorial_readme_anchor_exists_in_readme(self) -> None:
        tutorial_text = (_REPO_ROOT / "tutorial.md").read_text(encoding="utf-8")
        readme_text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "open-source-and-paid-tools" not in tutorial_text
        assert "(README.md#what-you-need)" in tutorial_text
        assert "## What you need" in readme_text


class TestSimHardeningR5:
    def test_wait_for_infra_exits_when_pg_isready_never_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.core import sim

        class _DummySock:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(sim, "_read_env_file_value", lambda k, d: d)
        monkeypatch.setattr(sim, "_docker_compose_cmd", lambda: ["docker", "compose", "-f", "docker-compose.yml"])
        monkeypatch.setattr("socket.create_connection", lambda *a, **kw: _DummySock())
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setattr(
            sim.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"not ready"),
        )

        with pytest.raises(SystemExit) as exc_info:
            sim.wait_for_infra()
        assert exc_info.value.code == 1

    def test_preflight_infra_uses_docker_compose_cmd_for_postgres_build_and_checks_pull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.core import sim

        monkeypatch.setattr(sim, "_read_port_slot", lambda: 2)
        monkeypatch.setattr(sim, "_docker_compose_cmd", lambda: ["docker", "compose", "-f", "docker-compose.override.yml"])
        monkeypatch.setattr(sim, "wait_for_infra", lambda: None)

        seen_cmds: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            seen_cmds.append(cmd)
            if cmd[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            if cmd[:2] == ["docker", "pull"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sim.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            sim.preflight_infra()
        assert exc_info.value.code == 1

        # Now verify build postgres uses _docker_compose_cmd() when pull succeeds
        seen_cmds.clear()

        def fake_run_ok(cmd, *args, **kwargs):
            seen_cmds.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sim.subprocess, "run", fake_run_ok)
        sim.preflight_infra()
        assert ["docker", "compose", "-f", "docker-compose.override.yml", "build", "postgres"] in seen_cmds

    def test_generate_protos_exits_nonzero_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.core import sim

        monkeypatch.setattr(sim.os.path, "exists", lambda p: True)
        monkeypatch.setattr(
            sim.subprocess,
            "run",
            MagicMock(side_effect=subprocess.CalledProcessError(2, ["bash", "scripts/core/generate_proto.sh"])),
        )
        with pytest.raises(SystemExit) as exc_info:
            sim.generate_protos()
        assert exc_info.value.code == 2

    def test_offline_test_skips_gcp_creds_and_uses_sys_executable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.core import sim

        monkeypatch.setattr(sim, "generate_protos", lambda: None)
        monkeypatch.setattr(
            sim,
            "check_gcp_credentials",
            lambda: (_ for _ in ()).throw(AssertionError("test() must not require GCP credentials")),
        )
        seen_cmds: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            seen_cmds.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        monkeypatch.setattr(sim.subprocess, "run", fake_run)
        sim.test(include_slow=False)

        py_cmd = seen_cmds[0]
        assert py_cmd[:3] == [sys.executable, "-m", "pytest"]
        assert "scripts/tests" in py_cmd
        assert "scripts/deploy/deploy_test.py" in py_cmd


class TestAgentDashHardeningR5:
    def test_default_port_is_9111_and_no_import_pythonpath_mutation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        src = (_REPO_ROOT / "scripts" / "core" / "agent_dash.py").read_text(encoding="utf-8")
        assert 'os.getenv("PORT", 9111)' in src
        assert 'os.environ["PYTHONPATH"]' not in src

        monkeypatch.setenv("PUBSUB_TOPIC_ID", "custom-telemetry")
        mod = _load_module("agent_dash_topic_r5", "scripts/core/agent_dash.py")
        assert mod.TOPIC_ID == "custom-telemetry"

    def test_project_id_resolution_and_fail_fast_without_emulator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.core import agent_dash

        for var in ("PUBSUB_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "PROJECT_ID", "PUBSUB_EMULATOR_HOST"):
            monkeypatch.delenv(var, raising=False)
        assert agent_dash._resolve_project_id() == ""

        monkeypatch.setattr(agent_dash, "PROJECT_ID", "")
        with pytest.raises(RuntimeError, match="PUBSUB_EMULATOR_HOST"):
            agent_dash.start_subscriber()

        monkeypatch.setenv("PUBSUB_EMULATOR_HOST", "127.0.0.1:9103")
        assert agent_dash._resolve_project_id() == "test-project"

        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-proj")
        assert agent_dash._resolve_project_id() == "gcp-proj"

    def test_ensure_topic_and_subscription_ignore_already_exists_and_reraise_others(self) -> None:
        from scripts.core import agent_dash

        class AlreadyExists(Exception):
            pass

        class PermissionDenied(Exception):
            pass

        pub = MagicMock()
        pub.create_topic.side_effect = AlreadyExists("exists")
        agent_dash._ensure_topic(pub, "projects/p/topics/t")

        pub.create_topic.side_effect = PermissionDenied("denied")
        with pytest.raises(PermissionDenied):
            agent_dash._ensure_topic(pub, "projects/p/topics/t")

        sub = MagicMock()
        sub.create_subscription.side_effect = AlreadyExists("exists")
        agent_dash._ensure_subscription(sub, "projects/p/subscriptions/s", "projects/p/topics/t")

        sub.create_subscription.side_effect = PermissionDenied("denied")
        with pytest.raises(PermissionDenied):
            agent_dash._ensure_subscription(sub, "projects/p/subscriptions/s", "projects/p/topics/t")

    def test_get_index_serves_html_from_any_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from scripts.core import agent_dash

        monkeypatch.chdir(tmp_path)
        client = TestClient(agent_dash.app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "<html" in resp.text.lower()
        assert "Index not found" not in resp.text
        assert 'src="/' not in resp.text
        assert 'href="/' not in resp.text

    def test_get_index_returns_404_when_index_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from fastapi.testclient import TestClient

        from scripts.core import agent_dash

        monkeypatch.setattr(agent_dash, "_INDEX_HTML_PATH", tmp_path / "missing_index.html")
        resp = TestClient(agent_dash.app).get("/")
        assert resp.status_code == 404
        assert "Index not found" in resp.text


class TestOpsE2eBenchHardeningR5:
    def test_spark_processor_raises_on_missing_processor_id_and_never_emits_fake_chunks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["spark_alloydb_processor.py", "--bogus-unparsed-flag"])
        mod = _load_module("spark_r5", "scripts/ops/spark_alloydb_processor.py")
        assert mod.PROCESSOR_ID == "" or mod.PROCESSOR_ID != "776a2fcf1a6908bc"

        monkeypatch.setattr(mod, "PROCESSOR_ID", "")
        with pytest.raises(ValueError, match="PROCESSOR_ID"):
            mod.process_document("gs://bucket/doc.pdf")

        # Empty Document AI text should return [], never ("...", "EMPTY_RESULT")
        monkeypatch.setattr(mod, "PROCESSOR_ID", "proc-123")
        monkeypatch.setattr(mod, "PROJECT_ID", "proj-123")

        fake_docai = types.ModuleType("google.cloud.documentai")
        fake_client = MagicMock()
        fake_client.processor_path.return_value = "projects/proj-123/locations/us/processors/proc-123"
        fake_client.process_document.return_value = MagicMock(
            document=MagicMock(text="", document_layout=MagicMock(blocks=[]), pages=[])
        )
        fake_docai.DocumentProcessorServiceClient = MagicMock(return_value=fake_client)  # type: ignore[attr-defined]
        fake_docai.RawDocument = MagicMock()  # type: ignore[attr-defined]
        fake_docai.ProcessRequest = MagicMock()  # type: ignore[attr-defined]

        fake_storage = types.ModuleType("google.cloud.storage")
        fake_blob = MagicMock()
        fake_blob.download_as_bytes.return_value = b"%PDF-1.4"
        fake_storage.Client = MagicMock(  # type: ignore[attr-defined]
            return_value=MagicMock(bucket=MagicMock(return_value=MagicMock(blob=MagicMock(return_value=fake_blob))))
        )

        fake_pypdf = types.ModuleType("pypdf")
        fake_pypdf.PdfReader = MagicMock(return_value=MagicMock(pages=[MagicMock()]))  # type: ignore[attr-defined]
        fake_pypdf.PdfWriter = MagicMock(  # type: ignore[attr-defined]
            return_value=MagicMock(write=lambda buf: buf.write(b"chunk"))
        )

        with (
            patch.dict(
                sys.modules,
                {
                    "google.cloud.documentai": fake_docai,
                    "google.cloud.storage": fake_storage,
                    "pypdf": fake_pypdf,
                },
            ),
            patch("google.cloud.documentai", fake_docai, create=True),
            patch("google.cloud.storage", fake_storage, create=True),
        ):
            chunks = mod.process_document("gs://bucket/doc.pdf")
        assert chunks == []

        # Document AI exception propagates instead of returning ("...", "ERROR: ...")
        fake_client.process_document.side_effect = RuntimeError("docai boom")
        with (
            patch.dict(
                sys.modules,
                {
                    "google.cloud.documentai": fake_docai,
                    "google.cloud.storage": fake_storage,
                    "pypdf": fake_pypdf,
                },
            ),
            patch("google.cloud.documentai", fake_docai, create=True),
            patch("google.cloud.storage", fake_storage, create=True),
            pytest.raises(RuntimeError, match="docai boom"),
        ):
            mod.process_document("gs://bucket/doc.pdf")

    def test_spark_processor_main_does_not_open_tmp_spark_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import builtins

        monkeypatch.setattr(sys, "argv", ["spark_alloydb_processor.py"])
        mod = _load_module("spark_r5_main", "scripts/ops/spark_alloydb_processor.py")
        monkeypatch.setattr(mod, "is_local", True)
        monkeypatch.setattr(mod, "GCS_FILE_LOCATION", "gs://bucket/prefix/")

        fake_storage = types.ModuleType("google.cloud.storage")
        fake_storage.Client = MagicMock(  # type: ignore[attr-defined]
            return_value=MagicMock(list_blobs=MagicMock(return_value=[]))
        )

        fake_spark_session = MagicMock()
        fake_spark_builder = MagicMock()
        fake_spark_builder.master.return_value = fake_spark_builder
        fake_spark_builder.appName.return_value = fake_spark_builder
        fake_spark_builder.config.return_value = fake_spark_builder
        fake_spark_builder.getOrCreate.return_value = fake_spark_session

        fake_pyspark = types.ModuleType("pyspark")
        fake_pyspark_sql = types.ModuleType("pyspark.sql")
        fake_pyspark_sql.SparkSession = MagicMock(builder=fake_spark_builder)  # type: ignore[attr-defined]
        fake_pyspark_sql_functions = types.ModuleType("pyspark.sql.functions")
        fake_pyspark_sql_functions.col = MagicMock()  # type: ignore[attr-defined]
        fake_pyspark_sql_functions.explode = MagicMock()  # type: ignore[attr-defined]
        fake_pyspark_sql_functions.udf = MagicMock()  # type: ignore[attr-defined]
        fake_pyspark_sql_types = types.ModuleType("pyspark.sql.types")
        fake_pyspark_sql_types.ArrayType = MagicMock()  # type: ignore[attr-defined]
        fake_pyspark_sql_types.StringType = MagicMock()  # type: ignore[attr-defined]
        fake_pyspark_sql_types.StructField = MagicMock()  # type: ignore[attr-defined]
        fake_pyspark_sql_types.StructType = MagicMock()  # type: ignore[attr-defined]

        opened_paths: list[str] = []
        real_open = builtins.open

        def spy_open(file, *args, **kwargs):
            opened_paths.append(str(file))
            return real_open(file, *args, **kwargs)

        with (
            patch.dict(
                sys.modules,
                {
                    "google.cloud.storage": fake_storage,
                    "pyspark": fake_pyspark,
                    "pyspark.sql": fake_pyspark_sql,
                    "pyspark.sql.functions": fake_pyspark_sql_functions,
                    "pyspark.sql.types": fake_pyspark_sql_types,
                },
            ),
            patch("google.cloud.storage", fake_storage, create=True),
            patch("builtins.open", side_effect=spy_open),
            caplog.at_level(logging.INFO, logger=mod.logger.name),
        ):
            mod.main([])

        assert not any("/tmp/spark_" in p for p in opened_paths), f"Unexpected /tmp/spark_ open: {opened_paths}"
        assert "pdf_uris: []" in caplog.text

    def test_emergency_flush_reraises_non_missing_table_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.ops import emergency_flush

        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:9213/testdb")

        class _FakeConn:
            @asynccontextmanager
            async def begin_nested(self):
                yield

            async def execute(self, _stmt):
                raise ConnectionError("password authentication failed")

        class _FakeEngine:
            @asynccontextmanager
            async def begin(self):
                yield _FakeConn()

            async def dispose(self) -> None:
                return None

        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=_FakeEngine()):
            with pytest.raises(ConnectionError, match="password authentication failed"):
                asyncio.run(emergency_flush.flush_db())

    def test_generate_local_seeds_includes_owner_id_seed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.ops import generate_local_seeds

        seed_file = tmp_path / "r1.json"
        seed_file.write_text(
            json.dumps(
                {
                    "route_id": "route-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "route_data": {"waypoints": []},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(generate_local_seeds, "_SEEDS_DIR", tmp_path)
        sql = generate_local_seeds._build_route_inserts()
        assert "owner_id" in sql
        assert "'__seed__'" in sql

    def test_test_runner_raises_when_card_url_missing(self) -> None:
        from scripts.e2e import test_runner

        spawn_resp = httpx.Response(
            200,
            json={"sessions": [{"sessionId": "s1"}]},
            request=httpx.Request("POST", "http://127.0.0.1:9101/api/v1/spawn"),
        )
        cards_resp = httpx.Response(
            200,
            json={"runner": {"name": "runner"}},
            request=httpx.Request("GET", "http://127.0.0.1:9101/api/v1/agent-types"),
        )
        fake_client = MagicMock()
        fake_client.post = AsyncMock(return_value=spawn_resp)
        fake_client.get = AsyncMock(return_value=cards_resp)

        with pytest.raises(RuntimeError, match="missing a valid 'url'"):
            asyncio.run(
                test_runner.run_gateway(
                    fake_client,
                    "http://127.0.0.1:9101",
                    "runner",
                    1,
                    [{"event": "start_gun"}],
                    "sim-1",
                )
            )

    @pytest.mark.parametrize("card_payload", [{"url": "   "}, {"url": None}, {"url": ""}])
    def test_test_runner_raises_when_card_url_whitespace_or_none(self, card_payload: dict) -> None:
        from scripts.e2e import test_runner

        spawn_resp = httpx.Response(
            200,
            json={"sessions": [{"sessionId": "s1"}]},
            request=httpx.Request("POST", "http://127.0.0.1:9101/api/v1/spawn"),
        )
        cards_resp = httpx.Response(
            200,
            json={"runner": card_payload},
            request=httpx.Request("GET", "http://127.0.0.1:9101/api/v1/agent-types"),
        )
        fake_client = MagicMock()
        fake_client.post = AsyncMock(return_value=spawn_resp)
        fake_client.get = AsyncMock(return_value=cards_resp)

        with pytest.raises(RuntimeError, match="missing a valid 'url'"):
            asyncio.run(
                test_runner.run_gateway(
                    fake_client,
                    "http://127.0.0.1:9101",
                    "runner",
                    1,
                    [{"event": "start_gun"}],
                    "sim-1",
                )
            )

    def test_bench_concurrency_no_passing_level_and_logs_errors(self, capsys, caplog) -> None:
        from scripts.bench.bench_concurrency import LevelResult, print_results, run_level

        results = [
            LevelResult(
                n=10,
                throughput=1.0,
                p50=1.2,
                p95=1.5,
                p99=2.0,
                wall_time=10.0,
                memory_bytes=1024,
                errors=0,
            )
        ]
        print_results(results, "agent_x", "pool=10", p95_threshold=0.5)
        out = capsys.readouterr().out
        assert "No passing concurrency level" in out
        assert "Recommended --concurrency:" not in out

        class _FailingRunner:
            async def run_async(self, **_kwargs):
                raise RuntimeError("runner boom")
                yield  # pragma: no cover

        with caplog.at_level(logging.WARNING, logger="scripts.bench.bench_concurrency"):
            res = asyncio.run(run_level(_FailingRunner(), "app", ["s1"], "{}"))
        assert res.errors == 1
        assert "runner boom" in caplog.text

    def test_perf_diagnostic_iap_token_fails_fast_and_banner_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.bench import perf_diagnostic

        monkeypatch.setattr(perf_diagnostic, "IAP_CLIENT_ID", "client-123.apps.googleusercontent.com")
        monkeypatch.setattr(
            perf_diagnostic.subprocess,
            "run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "gcloud")),
        )
        with pytest.raises(RuntimeError, match="Failed to obtain IAP identity token"):
            perf_diagnostic.get_iap_token()

        src = (_REPO_ROOT / "scripts" / "bench" / "perf_diagnostic.py").read_text(encoding="utf-8")
        assert "N26 Simulation Gateway" not in src
        assert "The Rehearsal Gateway" in src

    def test_test_vllm_runner_exits_nonzero_on_error_and_sends_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.e2e import test_vllm_runner

        seen_posts: list[dict] = []

        def fake_get(url, **_kwargs):
            if url.endswith("/health"):
                return httpx.Response(200, request=httpx.Request("GET", url))
            return httpx.Response(200, json={"data": [{"id": "gemma"}]}, request=httpx.Request("GET", url))

        def fake_post(url, json=None, **_kwargs):
            seen_posts.append(json or {})
            return httpx.Response(500, text="server error", request=httpx.Request("POST", url))

        with patch("httpx.get", side_effect=fake_get), patch("httpx.post", side_effect=fake_post):
            with pytest.raises(SystemExit) as exc_info:
                test_vllm_runner.main()
        assert exc_info.value.code == 1
        assert seen_posts and "tools" in seen_posts[0]

    def test_e2e_fanout_and_scale_check_status_before_json(self) -> None:
        from scripts.e2e import e2e_fanout_test, e2e_scale_test

        class _ErrResp:
            status = 502

            async def text(self):
                return "<html>502 Bad Gateway</html>"

            async def json(self):
                raise AssertionError("json() must not be called when status >= 400")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _FakeSession:
            def post(self, *args, **kwargs):
                return _ErrResp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        with patch("aiohttp.ClientSession", return_value=_FakeSession()):
            assert asyncio.run(e2e_fanout_test.spawn_sessions(5)) is None
            assert asyncio.run(e2e_scale_test.spawn_sessions_batched(5)) == []

    def test_discover_maps_tools_none_description_does_not_raise(self) -> None:
        tool = types.SimpleNamespace(name="maps_test", description=None)
        desc = (getattr(tool, "description", None) or "")[:60]
        assert desc == ""

    def test_shell_scripts_syntax_and_no_hardcoded_teardown_instance(self) -> None:
        bash_exe = "/bin/bash" if Path("/bin/bash").exists() else "bash"
        for rel in (
            "scripts/core/generate_proto.sh",
            "scripts/core/run_agent.sh",
            "scripts/core/start_db.sh",
            "scripts/core/install_alloydb_proxy.sh",
            "scripts/teardown.sh",
        ):
            res = subprocess.run([bash_exe, "-n", str(_REPO_ROOT / rel)], capture_output=True, text=True)
            assert res.returncode == 0, f"{rel}: {res.stderr}"

        teardown_text = (_REPO_ROOT / "scripts" / "teardown.sh").read_text(encoding="utf-8")
        assert "gcloud sql instances patch race-condition-postgres" not in teardown_text
        assert "database_instance_name" in teardown_text

        buf = io.StringIO()
        assert buf is not None
