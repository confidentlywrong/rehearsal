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

"""Round-2 regression tests for operational, runner, and E2E scripts."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.core import agent_dash, sim
from scripts.ops import emergency_flush

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / rel_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSimDotenvAndPortSafety:
    def test_parse_dotenv_line_handles_export_quotes_and_inline_comments(self):
        assert sim._parse_dotenv_line("") is None
        assert sim._parse_dotenv_line("   # full comment") is None
        assert sim._parse_dotenv_line("export API_KEY='secret # keep'") == ("API_KEY", "secret # keep")
        assert sim._parse_dotenv_line('export DB_URL="postgres://u:p@localhost/db"') == (
            "DB_URL",
            "postgres://u:p@localhost/db",
        )
        assert sim._parse_dotenv_line("PORT=8101 # gateway port") == ("PORT", "8101")
        assert sim._parse_dotenv_line("RAW_HASH=abc#def") == ("RAW_HASH", "abc#def")

    def test_kill_process_on_port_validates_port_range(self, monkeypatch):
        called = []
        monkeypatch.setattr(sim.subprocess, "check_output", lambda *a, **kw: called.append(a) or b"")
        for invalid_port in (-1, 65536, 99999, "8080;rm"):
            sim.kill_process_on_port(invalid_port)  # type: ignore[arg-type]
        assert called == [], f"Out-of-range/non-numeric ports must short-circuit before lsof, got {called}"

    def test_preflight_infra_never_kills_host_ollama_daemon(self, monkeypatch):
        killed_ports: list[int] = []
        monkeypatch.setattr(sim, "kill_process_on_port", lambda p: killed_ports.append(int(p)))
        monkeypatch.setattr(sim.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0, stdout=""))
        monkeypatch.setattr(sim, "wait_for_infra", lambda **kw: True)

        sim.preflight_infra()
        assert 11434 not in killed_ports, "preflight_infra must NEVER kill host Ollama on port 11434"


class TestAgentDashMalformedPubSubAck:
    def test_pubsub_callback_acks_invalid_utf8_and_invalid_json(self):
        bad_utf8 = MagicMock()
        bad_utf8.data = b"\xff\xfe\xfd"
        agent_dash.pubsub_callback(bad_utf8)
        bad_utf8.ack.assert_called_once()
        bad_utf8.nack.assert_not_called()

        bad_json = MagicMock()
        bad_json.data = b"{not-valid-json"
        agent_dash.pubsub_callback(bad_json)
        bad_json.ack.assert_called_once()
        bad_json.nack.assert_not_called()


class TestEmergencyFlushAuthAndEnv:
    def test_parse_env_line_strips_export_quotes_and_comments(self):
        assert emergency_flush._parse_env_line("export REDIS_PASSWORD='pw # 1'") == ("REDIS_PASSWORD", "pw # 1")
        assert emergency_flush._parse_env_line("REDIS_ADDR=127.0.0.1:6379 # local") == (
            "REDIS_ADDR",
            "127.0.0.1:6379",
        )

    def test_flush_redis_passes_redis_password_when_url_has_no_auth(self, monkeypatch):
        monkeypatch.setenv("REDIS_ADDR", "127.0.0.1:6379")
        monkeypatch.setenv("REDIS_PASSWORD", "s3cr3t")

        fake_client = MagicMock()
        fake_client.info = AsyncMock(return_value={})
        fake_client.dbsize = AsyncMock(return_value=3)
        fake_client.flushall = AsyncMock(return_value=True)
        fake_client.aclose = AsyncMock(return_value=None)

        captured: dict = {}

        def fake_from_url(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return fake_client

        with patch("redis.asyncio.from_url", side_effect=fake_from_url):
            ok = asyncio.run(emergency_flush.flush_redis())

        assert ok is True
        assert captured["url"] == "redis://127.0.0.1:6379"
        assert captured["kwargs"].get("password") == "s3cr3t"


class TestSparkProcessorAndVllmHelpers:
    def test_spark_parse_gcs_uri_and_default_schema(self, monkeypatch):
        monkeypatch.setenv("LOCAL_DEV", "false")
        monkeypatch.delenv("ALLOYDB_SCHEMA", raising=False)
        mod = _load_module("spark_alloydb_processor_r2", "scripts/ops/spark_alloydb_processor.py")

        assert mod.alloydb_schema == "public"
        assert mod._parse_gcs_uri("gs://my-bucket/laws/nevada") == ("my-bucket", "laws/nevada")
        assert mod._parse_gcs_uri("gs://my-bucket/") == ("my-bucket", "")
        for bad_uri in ("", "https://storage.googleapis.com/b/o", "gs://", "gs:///no-bucket"):
            with pytest.raises(ValueError):
                mod._parse_gcs_uri(bad_uri)

    def test_vllm_base_url_removesuffix_preserves_host_ending_in_1_or_v(self):
        # Regression check for `base_url.rstrip('/v1')` stripping trailing '1' / 'v' from hostnames.
        base_url = "http://vllm-node1/v1/"
        health_base = base_url.rstrip("/").removesuffix("/v1")
        assert health_base == "http://vllm-node1"
