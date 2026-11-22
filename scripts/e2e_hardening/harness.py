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

"""Shared opaque-box E2E test harness, service doubles, and isolation helpers."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any

import asyncpg
import httpx
import psycopg2
import redis

from scripts.e2e_hardening.compliance_scanners import check_protected_files_untouched

REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent

_HOST_OLLAMA_PORT = 11434

_INHERITED_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "GOPATH",
    "GOMODCACHE",
    "GOCACHE",
    "GOROOT",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

# Any key present in os.environ (even as "") prevents godotenv.Load() in Go's
# config.Load() from backfilling values from the repository root .env file.
_DOTENV_BLOCKED_KEYS: tuple[str, ...] = (
    "DATABASE_URL",
    "ALLOYDB_HOST",
    "ALLOYDB_PORT",
    "ALLOYDB_USER",
    "ALLOYDB_PASSWORD",
    "ALLOYDB_DATABASE",
    "ALLOYDB_SCHEMA",
    "ALLOYDB_SSLMODE",
    "REDIS_ADDR",
    "REDIS_PASSWORD",
    "REDIS_PREFIX",
    "AGENT_URLS",
    "STRIPE_API_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_RETURN_BASE_URL",
    "STRIPE_PRICE_MAP",
    "PUBSUB_EMULATOR_HOST",
    "PUBSUB_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "PROJECT_ID",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_CREDENTIALS",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AI_SETTINGS_PATH",
    "METERING_SINK",
    "METERING_SQLITE_PATH",
    "MEMORY_STORAGE_DIR",
    "CORS_ALLOWED_ORIGINS",
    "GATEWAY_URL",
    "GATEWAY_INTERNAL_URL",
    "LOCAL_RESOLVER_ALLOW_NON_LOOPBACK",
    "SAAS_MODE",
    "BILLING_PROVIDER",
    "TESTER_REQUIRE_AUTH",
    "FRONTEND_DEV_URL",
    "GATEWAY_PORT",
    "ADMIN_PORT",
    "ADMIN_URL",
    "BILLING_PORT",
    "FRONTEND_PORT",
    "FRONTEND_BFF_PORT",
    "TESTER_PORT",
    "DASH_PORT",
    "TENANT_KEY_PREFIX",
    "ACCOUNT_KEY_PREFIX",
    "BOOTSTRAP_SUPERADMIN_EMAIL",
    "LOCAL_DEV_USER_EMAIL",
    "ALLOW_LOCAL_AUTH",
    "USE_ALLOYDB",
    "MAX_RUNNERS",
    "AGENT_CATALOG_PATH",
)


class PostgresUnavailableError(RuntimeError):
    """Raised when no test or dev PostgreSQL instance is reachable."""


class RedisUnavailableError(RuntimeError):
    """Raised when no test or dev Redis instance is reachable."""


def run_cmd(
    cmd: str | Sequence[str | Path],
    cwd: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured text output without raising on non-zero exit."""
    use_shell = isinstance(cmd, str)
    resolved_cmd: str | list[str]
    if isinstance(cmd, str):
        resolved_cmd = cmd
    else:
        resolved_cmd = [str(part) for part in cmd]
    return subprocess.run(
        resolved_cmd,
        cwd=str(cwd),
        env=dict(env) if env is not None else None,
        shell=use_shell,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def build_go_binary(pkg_path: str, out_dir: Path, name: str) -> Path:
    """Compile a Go package into an external directory and assert it is executable."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    result = run_cmd(["go", "build", "-o", str(out_path), pkg_path], cwd=REPO_ROOT, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"go build failed for {pkg_path} (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    if not out_path.is_file() or not os.access(out_path, os.X_OK):
        raise RuntimeError(f"Built Go binary is missing or not executable: {out_path}")
    return out_path


def free_port() -> int:
    """Return an ephemeral loopback TCP port, guaranteeing it is never 11434."""
    for _ in range(16):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != _HOST_OLLAMA_PORT:
            return port
    raise RuntimeError(f"Unable to allocate an ephemeral port != {_HOST_OLLAMA_PORT}")


def _sanitize_go_env(
    binary_name: str,
    port: int,
    caller_env: Mapping[str, str] | None,
) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key in _INHERITED_ENV_KEYS:
        if key in os.environ:
            sanitized[key] = os.environ[key]

    for key in _DOTENV_BLOCKED_KEYS:
        sanitized[key] = ""

    sanitized["PORT"] = str(port)
    lower_name = binary_name.lower()
    if "gateway" in lower_name:
        sanitized["GATEWAY_PORT"] = str(port)
    if "admin" in lower_name:
        sanitized["ADMIN_PORT"] = str(port)

    sanitized["IDENTITY_PROVIDER"] = "local"
    sanitized["USERS_MIGRATIONS_DIR"] = str(REPO_ROOT / "migrations")

    if caller_env:
        for forbidden_key in ("PORT", "GATEWAY_PORT", "ADMIN_PORT"):
            if forbidden_key in caller_env:
                raise ValueError(
                    f"Pass port=... to EphemeralGoServer instead of env[{forbidden_key!r}]"
                )
        for key, value in caller_env.items():
            sanitized[key] = value
    return sanitized


class EphemeralGoServer:
    """Context manager managing an ephemeral compiled Go HTTP/WS service."""

    def __init__(
        self,
        binary_path: Path,
        *,
        port: int | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path = REPO_ROOT,
        health_path: str | None = None,
        ready_statuses: tuple[int, ...] = (200,),
        startup_timeout: float = 15.0,
    ) -> None:
        self.binary_path = Path(binary_path)
        self.port = port if port is not None else free_port()
        if self.port == _HOST_OLLAMA_PORT:
            raise ValueError(f"Refusing to bind Go server on host Ollama port {_HOST_OLLAMA_PORT}")
        self.cwd = Path(cwd)
        if health_path is None:
            self.health_path = "/healthz" if "billing" in self.binary_path.name.lower() else "/health"
        else:
            self.health_path = health_path if health_path.startswith("/") else f"/{health_path}"
        self.ready_statuses = ready_statuses
        self.startup_timeout = startup_timeout
        self.env = _sanitize_go_env(self.binary_path.name, self.port, env)
        self._temp_ai_dir: tempfile.TemporaryDirectory[str] | None = None
        if "gateway" in self.binary_path.name.lower() and not self.env.get("AI_SETTINGS_PATH"):
            self._temp_ai_dir = tempfile.TemporaryDirectory(prefix="e2e-ai-settings-")
            ai_path = Path(self._temp_ai_dir.name) / "ai_settings.json"
            ai_path.write_text(
                json.dumps({"provider": "cloud_gemini", "endpoint": f"http://127.0.0.1:{free_port()}"}),
                encoding="utf-8",
            )
            self.env["AI_SETTINGS_PATH"] = str(ai_path)

        self.process: subprocess.Popen[bytes] | None = None
        self._stdout_file: IO[bytes] | None = None
        self._stderr_file: IO[bytes] | None = None
        self._cached_stdout = ""
        self._cached_stderr = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    @property
    def stdout(self) -> str:
        if self._stdout_file is not None and not self._stdout_file.closed:
            self._stdout_file.flush()
            self._stdout_file.seek(0)
            return self._stdout_file.read().decode("utf-8", errors="replace")
        return self._cached_stdout

    @property
    def stderr(self) -> str:
        if self._stderr_file is not None and not self._stderr_file.closed:
            self._stderr_file.flush()
            self._stderr_file.seek(0)
            return self._stderr_file.read().decode("utf-8", errors="replace")
        return self._cached_stderr

    def __enter__(self) -> EphemeralGoServer:
        self._stdout_file = tempfile.TemporaryFile(mode="w+b")
        self._stderr_file = tempfile.TemporaryFile(mode="w+b")
        self.process = subprocess.Popen(
            [str(self.binary_path)],
            cwd=str(self.cwd),
            env=self.env,
            stdout=self._stdout_file,
            stderr=self._stderr_file,
        )

        deadline = time.monotonic() + self.startup_timeout
        probe_url = f"{self.base_url}{self.health_path}"
        last_error: str = "no probe attempted"

        with httpx.Client(timeout=1.0) as client:
            while time.monotonic() < deadline:
                exit_code = self.process.poll()
                if exit_code is not None:
                    out = self.stdout
                    err = self.stderr
                    self._cleanup_process()
                    raise RuntimeError(
                        f"{self.binary_path.name} exited early with code {exit_code}.\n"
                        f"STDOUT:\n{out}\nSTDERR:\n{err}"
                    )
                try:
                    resp = client.get(probe_url)
                    if resp.status_code in self.ready_statuses:
                        return self
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                time.sleep(0.1)

        out = self.stdout
        err = self.stderr
        self._cleanup_process()
        raise RuntimeError(
            f"{self.binary_path.name} did not become ready at {probe_url} within "
            f"{self.startup_timeout}s (last error: {last_error}).\n"
            f"STDOUT:\n{out}\nSTDERR:\n{err}"
        )

    def _cleanup_process(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)
        if self._stdout_file is not None and not self._stdout_file.closed:
            self._stdout_file.flush()
            self._stdout_file.seek(0)
            self._cached_stdout = self._stdout_file.read().decode("utf-8", errors="replace")
            self._stdout_file.close()
        if self._stderr_file is not None and not self._stderr_file.closed:
            self._stderr_file.flush()
            self._stderr_file.seek(0)
            self._cached_stderr = self._stderr_file.read().decode("utf-8", errors="replace")
            self._stderr_file.close()
        if self._temp_ai_dir is not None:
            self._temp_ai_dir.cleanup()
            self._temp_ai_dir = None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._cleanup_process()


class MockOllamaServer:
    """Thread-safe local HTTP double for Ollama on an ephemeral loopback port."""

    def __init__(
        self,
        *,
        tags_models: Sequence[str] = ("gemma2:9b", "embeddinggemma:latest"),
        chat_response_content: str = '{"status": "ok"}',
        embed_dim: int = 768,
        fail_chat: bool = False,
        fail_embed: bool = False,
        empty_embed: bool = False,
        status_override: Mapping[str, int] | None = None,
    ) -> None:
        self.tags_models: list[str] = list(tags_models)
        self.chat_response_content = chat_response_content
        self.embed_dim = embed_dim
        self.fail_chat = fail_chat
        self.fail_embed = fail_embed
        self.empty_embed = empty_embed
        self.status_override: dict[str, int] = dict(status_override or {})

        self._lock = threading.Lock()
        self.requests: list[tuple[str, str, Any]] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record_request(self, method: str, path: str, body: Any) -> None:
        with self._lock:
            self.requests.append((method, path, body))

    def __enter__(self) -> MockOllamaServer:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _read_body(self) -> Any:
                raw_len = self.headers.get("Content-Length")
                if not raw_len:
                    return None
                length = int(raw_len)
                raw = self.rfile.read(length)
                text = raw.decode("utf-8", errors="replace")
                if not text:
                    return None
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_ndjson(self, status: int, frames: Sequence[dict[str, Any]]) -> None:
                raw = "".join(json.dumps(frame) + "\n" for frame in frames).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                parent.record_request("GET", path, None)
                if path in parent.status_override:
                    code = parent.status_override[path]
                    if code != 200:
                        self._send_json(code, {"error": f"injected status {code}"})
                        return

                if path == "/api/tags":
                    self._send_json(
                        200,
                        {"models": [{"name": m, "model": m} for m in parent.tags_models]},
                    )
                elif path == "/v1/models":
                    self._send_json(
                        200,
                        {
                            "object": "list",
                            "data": [{"id": m, "object": "model"} for m in parent.tags_models],
                        },
                    )
                else:
                    self._send_json(404, {"error": f"not found: {path}"})

            def do_POST(self) -> None:  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                body = self._read_body()
                parent.record_request("POST", path, body)

                if path in parent.status_override:
                    code = parent.status_override[path]
                    if code != 200:
                        self._send_json(code, {"error": f"injected status {code}"})
                        return

                body_dict = body if isinstance(body, dict) else {}

                if path == "/api/chat":
                    if parent.fail_chat:
                        self._send_json(500, {"error": "injected chat failure"})
                        return
                    model = str(body_dict.get("model") or parent.tags_models[0])
                    self._send_json(
                        200,
                        {
                            "model": model,
                            "message": {
                                "role": "assistant",
                                "content": parent.chat_response_content,
                            },
                            "done": True,
                        },
                    )
                elif path == "/api/embed":
                    if parent.fail_embed:
                        self._send_json(500, {"error": "injected embed failure"})
                        return
                    if parent.empty_embed:
                        self._send_json(200, {"embeddings": []})
                        return
                    raw_input = body_dict.get("input", "")
                    count = len(raw_input) if isinstance(raw_input, list) and raw_input else 1
                    vec = [0.1] * parent.embed_dim
                    self._send_json(200, {"embeddings": [list(vec) for _ in range(count)]})
                elif path == "/api/embeddings":
                    if parent.fail_embed:
                        self._send_json(500, {"error": "injected embed failure"})
                        return
                    if parent.empty_embed:
                        self._send_json(200, {"embedding": []})
                        return
                    self._send_json(200, {"embedding": [0.1] * parent.embed_dim})
                elif path == "/api/pull":
                    model_name = str(body_dict.get("name") or "gemma2:9b")
                    self._send_ndjson(
                        200,
                        [
                            {"status": f"pulling {model_name}", "completed": 50, "total": 100},
                            {"status": "success", "completed": 100, "total": 100},
                        ],
                    )
                else:
                    self._send_json(404, {"error": f"not found: {path}"})

        class _MockHTTPServer(ThreadingHTTPServer):
            request_queue_size = 128
            daemon_threads = True
            allow_reuse_address = True

        httpd = _MockHTTPServer(("127.0.0.1", 0), _Handler)
        bound_port = int(httpd.server_address[1])
        if bound_port == _HOST_OLLAMA_PORT:
            httpd.server_close()
            raise RuntimeError(f"MockOllamaServer accidentally bound host Ollama port {_HOST_OLLAMA_PORT}")
        self._httpd = httpd
        self.port = bound_port
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _build_search_path_dsn(base_dsn: str, schema_name: str) -> str:
    parts = urllib.parse.urlsplit(base_dsn)
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    params = [(k, v) for k, v in params if k != "options"]
    params.append(("options", f"-csearch_path={schema_name},public"))
    new_query = urllib.parse.urlencode(params)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _candidate_pg_dsns(explicit_dsn: str | None) -> list[str]:
    if explicit_dsn:
        return [explicit_dsn]
    env_dsn = os.environ.get("E2E_DATABASE_URL", "").strip()
    if env_dsn:
        return [env_dsn]
    return [
        "postgresql://testuser:testpass@127.0.0.1:9213/testdb",
        "postgresql://postgres:localdev@127.0.0.1:9113/postgres",
    ]


class IsolatedPostgresSchema:
    """Sync (`psycopg2`) and async (`asyncpg`) context manager for an ephemeral Postgres schema."""

    def __init__(self, dsn: str | None = None, *, require_available: bool = True) -> None:
        self._explicit_dsn = dsn
        self.require_available = require_available
        self.schema_name: str = f"e2e_{uuid.uuid4().hex[:12]}"
        self.dsn: str = ""
        self.search_path_dsn: str = ""
        self.available: bool = False
        self._sync_conn: psycopg2.extensions.connection | None = None
        self._async_conn: asyncpg.Connection | None = None

    def __enter__(self) -> IsolatedPostgresSchema:
        last_err: Exception | None = None
        for candidate in _candidate_pg_dsns(self._explicit_dsn):
            try:
                conn = psycopg2.connect(candidate, connect_timeout=2)
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema_name}"')
                self._sync_conn = conn
                self.dsn = candidate
                self.search_path_dsn = _build_search_path_dsn(candidate, self.schema_name)
                self.available = True
                return self
            except Exception as exc:  # noqa: BLE001
                last_err = exc

        self.available = False
        if self.require_available:
            raise PostgresUnavailableError(
                f"Unable to connect to any candidate PostgreSQL instance: {last_err}"
            )
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._sync_conn is not None:
            try:
                self._sync_conn.autocommit = True
                with self._sync_conn.cursor() as cur:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{self.schema_name}" CASCADE')
            finally:
                self._sync_conn.close()
                self._sync_conn = None

    async def __aenter__(self) -> IsolatedPostgresSchema:
        last_err: Exception | None = None
        for candidate in _candidate_pg_dsns(self._explicit_dsn):
            try:
                conn = await asyncpg.connect(candidate, timeout=2.0)
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema_name}"')
                self._async_conn = conn
                self.dsn = candidate
                self.search_path_dsn = _build_search_path_dsn(candidate, self.schema_name)
                self.available = True
                return self
            except Exception as exc:  # noqa: BLE001
                last_err = exc

        self.available = False
        if self.require_available:
            raise PostgresUnavailableError(
                f"Unable to connect asynchronously to any candidate PostgreSQL instance: {last_err}"
            )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._async_conn is not None:
            try:
                await self._async_conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema_name}" CASCADE')
            finally:
                await self._async_conn.close()
                self._async_conn = None


def _candidate_redis_endpoints() -> list[tuple[str, int, str]]:
    env_url = os.environ.get("E2E_REDIS_URL", "").strip()
    if env_url:
        parsed = urllib.parse.urlsplit(env_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        password = parsed.password or ""
        return [(host, port, password)]
    return [
        ("127.0.0.1", 9202, "testpass"),
        ("127.0.0.1", 9102, "localdev"),
        ("127.0.0.1", 9202, ""),
        ("127.0.0.1", 9102, ""),
    ]


class IsolatedRedisNamespace:
    """Context manager providing an isolated REDIS_PREFIX and deterministic key cleanup."""

    def __init__(self, *, require_available: bool = True) -> None:
        self.require_available = require_available
        self.prefix: str = f"e2e_{uuid.uuid4().hex[:12]}"
        self.host: str = ""
        self.port: int = 0
        self.password: str = ""
        self.addr: str = ""
        self.url: str = ""
        self.available: bool = False
        self._client: redis.Redis | None = None

    def __enter__(self) -> IsolatedRedisNamespace:
        last_err: Exception | None = None
        for host, port, password in _candidate_redis_endpoints():
            client = redis.Redis(
                host=host,
                port=port,
                password=password or None,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
                decode_responses=True,
            )
            try:
                if client.ping():
                    self._client = client
                    self.host = host
                    self.port = port
                    self.password = password
                    self.addr = f"{host}:{port}"
                    auth_part = f":{urllib.parse.quote(password, safe='')}@" if password else ""
                    self.url = f"redis://{auth_part}{host}:{port}/0"
                    self.available = True
                    return self
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                client.close()

        self.available = False
        if self.require_available:
            raise RedisUnavailableError(f"Unable to reach Redis test or dev instance: {last_err}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._client is not None:
            try:
                cursor = 0
                pattern = f"*{self.prefix}*"
                while True:
                    cursor, keys = self._client.scan(cursor=cursor, match=pattern, count=200)
                    if keys:
                        self._client.delete(*keys)
                    if cursor == 0:
                        break
            finally:
                self._client.close()
                self._client = None


def get_git_status_snapshot(repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """Return mapping of repo-relative paths to 2-char porcelain v1 status codes."""
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=str(repo_root),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        stderr_txt = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"git status failed in {repo_root}: {stderr_txt}")

    raw_tokens = proc.stdout.split(b"\0")
    snapshot: dict[str, str] = {}
    idx = 0
    while idx < len(raw_tokens):
        token = raw_tokens[idx]
        idx += 1
        if not token:
            continue
        decoded = token.decode("utf-8", errors="replace")
        if len(decoded) < 4:
            continue
        status = decoded[:2]
        path = decoded[3:]
        snapshot[path] = status
        if status[0] in {"R", "C"} and idx < len(raw_tokens):
            orig = raw_tokens[idx].decode("utf-8", errors="replace")
            idx += 1
            if orig:
                snapshot[orig] = status
    return snapshot


def assert_workspace_preserved(
    baseline: Mapping[str, str],
    allow_new_prefixes: tuple[str, ...] = ("scripts/e2e_hardening/",),
    repo_root: Path = REPO_ROOT,
) -> None:
    """Assert baseline uncommitted files were not wiped and no unexpected files changed."""
    current = get_git_status_snapshot(repo_root)

    missing: list[str] = []
    deleted: list[str] = []
    for path, orig_status in baseline.items():
        if path not in current:
            missing.append(path)
        elif "D" in current[path] and "D" not in orig_status:
            deleted.append(path)

    unexpected: list[str] = []
    for path in current:
        if path not in baseline and not any(path.startswith(prefix) for prefix in allow_new_prefixes):
            unexpected.append(path)

    protected_touched = check_protected_files_untouched(repo_root)

    errors: list[str] = []
    if missing:
        errors.append(f"Baseline paths disappeared from git status ({len(missing)}): {missing[:10]}")
    if deleted:
        errors.append(f"Baseline paths transitioned to deleted ({len(deleted)}): {deleted[:10]}")
    if unexpected:
        errors.append(
            f"Unexpected modified/untracked paths outside {allow_new_prefixes} ({len(unexpected)}): {unexpected[:10]}"
        )
    if protected_touched:
        errors.append(f"Protected files modified/untracked: {protected_touched}")

    if errors:
        raise AssertionError("Workspace preservation violation:\n- " + "\n- ".join(errors))
