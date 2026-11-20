# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: support rediss:// TLS Redis URIs in PrunedRedisSessionService override.
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

"""Unified runtime service abstraction — env-var-driven target switching.

Selects the correct ADK session, artifact, and memory services based on
deployment environment variables:

- No cloud env vars       → InMemorySessionService (local dev)
- DATABASE_URL set        → DatabaseSessionService (Cloud Run)
- GOOGLE_CLOUD_AGENT_ENGINE_ID set → VertexAiSessionService (Agent Engine)

CRITICAL: This module must NEVER import or reference SQLite session stores.
"""

import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _redact_redis_addr(addr: str) -> str:
    """Mask inline password in redis:// or rediss:// URIs before logging."""
    return re.sub(r"(rediss?://[^:/@\s]*:)[^@\s]+@", r"\1***@", addr)


def _parse_redis_host_port(addr: str) -> tuple[str, int]:
    """Parse host and port from a non-URI REDIS_ADDR (supports IPv4, hostnames, and IPv6)."""
    s = (addr or "").strip()
    if not s:
        return "localhost", 6379
    if s.startswith("["):
        if "]:" in s:
            bracket_host, _, port_str = s.rpartition("]:")
            host = bracket_host[1:] or "::1"
            return host, _int_env_raw(port_str, 6379, "REDIS_ADDR port")
        if s.endswith("]"):
            return s[1:-1] or "::1", 6379
    # Unbracketed IPv6 literal (2+ colons, e.g. "::1") has no port suffix
    if s.count(":") >= 2:
        return s, 6379
    if ":" in s:
        host, _, port_str = s.rpartition(":")
        return host or "localhost", _int_env_raw(port_str, 6379, "REDIS_ADDR port")
    return s, 6379


def _int_env(name: str, default: int) -> int:
    """Read an integer from an environment variable, falling back to *default*."""
    raw = os.environ.get(name, "")
    return _int_env_raw(raw, default, name)


def _int_env_raw(raw: str, default: int, label: str) -> int:
    """Parse *raw* as an integer, raising ``ValueError`` if non-empty and invalid."""
    cleaned = raw.strip()
    if not cleaned:
        return default
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {label}={raw!r}") from exc


@dataclass
class ServiceConfig:
    """Container for ADK service instances selected by deployment target."""

    session_service: Any
    artifact_service: Optional[Any]
    memory_service: Optional[Any]
    target: str


def create_services() -> ServiceConfig:
    """Create ADK services based on deployment environment variables.

    Precedence:
        0. SESSION_STORE_OVERRIDE=inmemory|redis → Force override
        1. GOOGLE_CLOUD_AGENT_ENGINE_ID → Agent Engine
        2. DATABASE_URL → Cloud Run (Database session + GCS artifacts)
        3. Neither → Local dev (InMemory everything)

    SESSION_STORE_OVERRIDE values:
        inmemory — InMemorySessionService (diagnostic only, no durability)
        redis    — RedisSessionService from google-adk-community (durable,
                   fast, uses REDIS_ADDR for connection)

    Returns:
        ServiceConfig with the appropriate service instances.
    """
    # Check for session store override first
    override = os.environ.get("SESSION_STORE_OVERRIDE", "").strip().lower()
    if override == "inmemory":
        return _create_inmemory_override_services()
    elif override == "redis":
        return _create_redis_override_services()
    elif override:
        raise ValueError(
            f"Unrecognized SESSION_STORE_OVERRIDE={override!r}; expected 'inmemory' or 'redis'"
        )

    agent_engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")
    database_url = os.environ.get("DATABASE_URL", "")

    if agent_engine_id:
        return _create_agent_engine_services()
    elif database_url:
        return _create_cloud_run_services()
    else:
        return _create_local_services()


def _create_inmemory_override_services() -> ServiceConfig:
    """Override: force InMemory session service regardless of cloud env vars.

    Used for performance diagnostics — isolates session-store latency from
    application logic. All other services (artifacts, memory) are set to None
    since this is a diagnostic mode, not a full local-dev environment.
    """
    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    logger.warning(
        "Runtime target: inmemory_override (SESSION_STORE_OVERRIDE=inmemory). "
        "Database/Agent Engine session stores BYPASSED for perf testing."
    )
    return ServiceConfig(
        session_service=InMemorySessionService(),
        artifact_service=None,
        memory_service=None,
        target="inmemory_override",
    )


def _create_redis_override_services() -> ServiceConfig:
    """Override: use PrunedRedisSessionService for durable, fast sessions.

    Wraps the community RedisSessionService with event pruning to prevent
    session blob growth.  Without pruning, the blob grows ~4 events per tick
    and causes progressive degradation at scale (1000 runners).

    Reads REDIS_ADDR from the environment (same Redis used by the dispatcher).
    The session service creates its own async connection — separate from
    the dispatcher's shared pool, which is fine for Redis.
    """
    from agents.utils.pruned_session_service import PrunedRedisSessionService

    redis_addr = os.environ.get("REDIS_ADDR", "localhost:6379")
    redis_password = os.environ.get("REDIS_PASSWORD", "")
    tls_enabled = os.environ.get("REDIS_TLS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    max_conn = _int_env("REDIS_SESSION_MAX_CONNECTIONS", 100)
    if redis_addr.startswith(("redis://", "rediss://")):
        uri = redis_addr
        if tls_enabled and uri.startswith("redis://"):
            uri = "rediss://" + uri[len("redis://") :]
        redis_svc = PrunedRedisSessionService(uri=uri, max_connections=max_conn)
    else:
        host, port = _parse_redis_host_port(redis_addr)
        if redis_password or tls_enabled:
            scheme = "rediss" if tls_enabled else "redis"
            auth_part = f":{urllib.parse.quote(redis_password, safe='')}@" if redis_password else ""
            host_part = f"[{host}]" if ":" in host else host
            uri = f"{scheme}://{auth_part}{host_part}:{port}"
            redis_svc = PrunedRedisSessionService(uri=uri, max_connections=max_conn)
        else:
            redis_svc = PrunedRedisSessionService(host=host, port=port, max_connections=max_conn)

    logger.warning(
        "Runtime target: redis_override (SESSION_STORE_OVERRIDE=redis). "
        "Using PrunedRedisSessionService at %s (max_connections=%d).",
        _redact_redis_addr(redis_addr),
        max_conn,
    )
    return ServiceConfig(
        session_service=redis_svc,
        artifact_service=None,
        memory_service=None,
        target="redis_override",
    )


def _create_local_services() -> ServiceConfig:
    """Local dev: InMemory session, artifacts, and memory."""
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    logger.info("Runtime target: local (InMemory everything)")
    return ServiceConfig(
        session_service=InMemorySessionService(),
        artifact_service=InMemoryArtifactService(),
        memory_service=InMemoryMemoryService(),
        target="local",
    )


def _create_agent_engine_services() -> ServiceConfig:
    """Agent Engine: VertexAi session + VertexAi memory bank.

    IMPORTANT: The custom SimulationExecutor (not ADK's A2aAgentExecutor)
    handles session creation via SessionManager, which correctly calls
    create_session() WITHOUT user-provided session IDs.
    """
    from google.adk.memory import VertexAiMemoryBankService
    from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService

    agent_engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = (
        os.environ.get("AGENT_ENGINE_LOCATION", "")
        or os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "")
        or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    )
    logger.info(
        "Runtime target: agent_engine (VertexAiSessionService + VertexAiMemoryBankService, id=%s)",
        agent_engine_id,
    )
    return ServiceConfig(
        session_service=VertexAiSessionService(
            project=project,
            location=location,
            agent_engine_id=agent_engine_id,
        ),
        artifact_service=None,  # Agent Engine manages artifacts internally
        memory_service=VertexAiMemoryBankService(
            project=project,
            location=location,
            agent_engine_id=agent_engine_id,
        ),
        target="agent_engine",
    )


def _create_cloud_run_services() -> ServiceConfig:
    """Cloud Run: Database session + GCS artifacts. No memory (runner is ephemeral)."""
    from google.adk.sessions.database_session_service import DatabaseSessionService
    import urllib.parse

    database_url = os.environ["DATABASE_URL"]
    logger.info("Runtime target: cloud_run (DatabaseSessionService)")

    # Connection pool sizing — forwarded as **kwargs to create_async_engine().
    # Defaults are intentionally higher than SQLAlchemy's (5/10) to support
    # concurrent session fan-out during broadcast events.
    pool_size = max(1, _int_env("DB_POOL_SIZE", 20))
    max_overflow = max(0, _int_env("DB_MAX_OVERFLOW", 20))
    logger.info("DB pool: pool_size=%d, max_overflow=%d", pool_size, max_overflow)

    # Workaround for asyncpg dialect not supporting options=-c search_path
    connect_args = {}
    if "?" in database_url:
        parsed = urllib.parse.urlsplit(database_url)
        query = urllib.parse.parse_qs(parsed.query)
        schema_name = None

        # Check for options=-c search_path=schema (preserve unrelated -c options)
        if "options" in query:
            remaining_opts: list[str] = []
            for opt_val in query["options"]:
                m = re.search(r"(?:^|\s)(?:-c\s*)?search_path=([^\s]+)", opt_val)
                if m:
                    schema_name = m.group(1).strip()
                    cleaned_opt = re.sub(r"(?:^|\s)(?:-c\s*)?search_path=[^\s]+", "", opt_val).strip()
                    if cleaned_opt:
                        remaining_opts.append(cleaned_opt)
                else:
                    remaining_opts.append(opt_val)
            if remaining_opts:
                query["options"] = remaining_opts
            else:
                query.pop("options")

        # Also check for explicit server_settings.search_path
        if "server_settings.search_path" in query:
            schema_name = query["server_settings.search_path"][0].strip()
            query.pop("server_settings.search_path")

        if schema_name:
            connect_args["server_settings"] = {"search_path": schema_name}
            new_query = urllib.parse.urlencode(query, doseq=True)
            database_url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
            )

    kwargs: dict[str, Any] = {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
    }
    if connect_args:
        kwargs["connect_args"] = connect_args

    # GCS artifact service — only if bucket is configured
    artifact_service = None
    gcs_bucket = os.environ.get("GCS_ARTIFACT_BUCKET", "")
    if gcs_bucket:
        from google.adk.artifacts.gcs_artifact_service import GcsArtifactService

        artifact_service = GcsArtifactService(bucket_name=gcs_bucket)
        logger.info("Cloud Run artifact store: GCS bucket %s", gcs_bucket)

    return ServiceConfig(
        session_service=DatabaseSessionService(db_url=database_url, **kwargs),
        artifact_service=artifact_service,
        memory_service=None,  # Runner NPCs are ephemeral — no cross-session memory
        target="cloud_run",
    )
