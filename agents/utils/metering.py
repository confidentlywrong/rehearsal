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

"""Token metering pipeline for ADK LlmAgent (per-user).

* :class:`MeteringEvent` — the cross-language schema; one row per
  completed model turn. Keyed by ``user_id`` only (orgs have been
  removed entirely from the gateway-side model).
* :class:`MeteringPlugin` — an ADK ``BasePlugin`` that hooks
  ``before_model_callback`` (to capture the model name out of the
  request) and ``after_model_callback`` (to emit the event after a
  non-partial response).
* Sink implementations: :class:`NullSink` (default; true no-op),
  :class:`StdoutSink`, :class:`SqliteSink`, :class:`PubSubSink`.
* :func:`build_sink_from_env` — env-driven selection. When
  ``METERING_SINK`` is unset/blank/``none`` the factory returns
  :class:`NullSink`; the metering hot path is then a single
  attribute lookup. No global "saas mode" flag — sink presence
  alone decides whether the pipeline writes.

Design invariants:

* Never invent numeric token counts. When ``usage_metadata`` is
  missing on a response the event is emitted with
  ``total_tokens=None``; the UI surfaces these rows as ``unknown``.
* Failure to emit must NEVER break a simulation — sink errors are
  logged + swallowed in :class:`MeteringPlugin.after_model_callback`.

The Go gateway consumer drains the SQLite ledger / Pub/Sub topic into
the ``usage_ledger`` + ``usage_rollup_monthly`` Postgres tables created
by ``migrations/0001_users.sql``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default SQLite path. Mirrors the convention used by
# ``agents/utils/tenant_paths.py`` — env override honoured first, then a
# fixed location next to the repo's ``data/`` directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SQLITE_PATH = _REPO_ROOT / "data" / "metering.db"

# Env vars consumed by this module (read on every call — tests can
# mutate them without re-importing):
#
# * METERING_SINK         — `none|stdout|sqlite|pubsub`. Defaults to `none`.
# * METERING_SQLITE_PATH  — overrides the SQLite path (tests use this).
# * METERING_PUBSUB_TOPIC — Pub/Sub topic id. Defaults to `usage-metering`.
# * PUBSUB_PROJECT_ID     — GCP project for the Pub/Sub publisher.


def _sink_name() -> str:
    """Return the configured METERING_SINK, lower-cased and trimmed.

    Returns ``"none"`` for unset/blank values so callers don't have to
    special-case the empty string.
    """
    raw = os.environ.get("METERING_SINK", "").strip().lower()
    return raw or "none"


# ---------------------------------------------------------------------------
# Event schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeteringEvent:
    """One model-turn worth of usage.

    Fields are intentionally nullable so the "never invent data" rule
    can be honoured end-to-end: a missing ``usage_metadata`` becomes
    ``prompt_tokens=None``, never ``0``, and the UI surfaces the row
    as ``unknown``.

    ``timestamp_iso`` is RFC3339 UTC for cross-language portability
    (Go's ``time.RFC3339Nano`` parses it; SQLite's ``DATETIME`` stores
    it as TEXT in collatable order).
    """

    user_id: str
    sim_id: Optional[str]
    agent_type: Optional[str]
    model: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    timestamp_iso: str
    # P2 (2026-06-23 audit): per-request cost in micros (1e-6 USD).
    # Optional because the local SDK path doesn't compute price; the
    # Pub/Sub path may attach it. Nullable preserves the "never invent
    # data" rule — Go drainer treats nil as 0 contribution to the rollup.
    cost_micros: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


def _now_iso() -> str:
    """Return the current UTC time formatted for cross-language parse.

    Indirected through a helper so tests can monkeypatch it cheaply.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


class MeteringSink(Protocol):
    """The single method every sink must implement.

    Sinks are called from the ADK after_model_callback path. They MUST
    NOT raise on emission failure — the plugin wrapper logs and swallows
    so a metering outage cannot break a simulation.
    """

    def emit(self, event: MeteringEvent) -> None: ...

    def close(self) -> None: ...


class NullSink:
    """No-op sink. The default whenever ``METERING_SINK`` is unset.

    Under the no-op default the plugin still loads but every emit()
    goes here, so we never touch the filesystem, never import Pub/Sub,
    never log.
    """

    def emit(self, event: MeteringEvent) -> None:
        return None

    def close(self) -> None:
        return None


class StdoutSink:
    """JSONL sink that writes to a stream. Defaults to ``sys.stdout``.

    Useful in tests and for the ``METERING_SINK=stdout`` local-debug
    mode. The stream is flushed on every emit so pipelines that grep
    the output see each line immediately.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, event: MeteringEvent) -> None:
        self._stream.write(event.to_json() + "\n")
        self._stream.flush()

    def close(self) -> None:
        return None


# SQL schema for the local metering ledger. Mirrors the column layout of
# the Postgres ``usage_ledger`` table created by
# ``migrations/0001_users.sql`` so the Go gateway can drain rows by
# field name without a translation step.
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS metering_ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT,
    sim_id              TEXT,
    agent_type          TEXT,
    model               TEXT,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER,
    cost_micros         INTEGER,
    timestamp_iso       TEXT    NOT NULL,
    drained             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS metering_ledger_user_ts_idx
    ON metering_ledger(user_id, timestamp_iso);
CREATE INDEX IF NOT EXISTS metering_ledger_drained_idx
    ON metering_ledger(drained);
"""

# Idempotent migration for SQLite ledgers created before cost_micros was
# added (P2, 2026-06-23 audit). SQLite has no `ADD COLUMN IF NOT EXISTS`,
# so we run it inside a try/except and swallow the duplicate-column
# error — every other failure surfaces normally.
_SQLITE_MIGRATIONS: tuple[str, ...] = ("ALTER TABLE metering_ledger ADD COLUMN cost_micros INTEGER",)


class SqliteSink:
    """Append-only sink backed by a local SQLite file.

    Same schema as the Postgres ``usage_ledger`` table plus a
    ``drained`` flag so the Go gateway consumer can incrementally
    drain unmigrated rows into Cloud SQL without re-reading the
    full file every poll.

    Thread-safe — every ``sqlite3.connect`` is gated by a class-level
    lock because the dispatcher spawns model calls concurrently and
    SQLite throws ``database is locked`` under high write contention
    when called from multiple threads on the same connection.
    """

    _lock = threading.Lock()

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or _default_sqlite_path())
        # Create the parent dir on first construction. We deliberately
        # do this in __init__ (not on first emit) so configuration
        # errors surface at boot rather than on the first model call.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(self._path) as conn:
            conn.executescript(_SQLITE_SCHEMA)
            # Run additive migrations for existing local DBs. SQLite has
            # no `ADD COLUMN IF NOT EXISTS`, so we swallow the
            # "duplicate column name" error and re-raise anything else.
            for stmt in _SQLITE_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: MeteringEvent) -> None:
        with self._lock, sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO metering_ledger
                    (user_id, sim_id, agent_type, model,
                     prompt_tokens, completion_tokens, total_tokens,
                     cost_micros, timestamp_iso)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.user_id,
                    event.sim_id,
                    event.agent_type,
                    event.model,
                    event.prompt_tokens,
                    event.completion_tokens,
                    event.total_tokens,
                    event.cost_micros,
                    event.timestamp_iso,
                ),
            )
            conn.commit()

    def close(self) -> None:
        return None


def _default_sqlite_path() -> Path:
    """Resolve the SQLite ledger path, honouring the env override."""
    override = os.environ.get("METERING_SQLITE_PATH", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_SQLITE_PATH


class PubSubSink:
    """Pub/Sub sink for the cloud deployment.

    Lazy-imports ``google.cloud.pubsub_v1`` because that dependency is
    several MB; we don't want to pull it in for local dev runs that
    never publish. Constructor failures (missing credentials, wrong
    project) raise so misconfiguration is loud rather than silent.

    Per-publish errors are swallowed and logged — the plugin wrapper
    enforces the same rule, but defending in depth keeps the model
    call path safe even if something else accidentally instantiates
    this sink outside the plugin.

    Async-failure handling (2026-06-24): ``PublisherClient.publish``
    returns a Future that resolves when the SDK's background batcher
    flushes the message. Without a done-callback, server-side errors
    (topic not found, IAM denied, quota exceeded) raised after batch
    submission silently disappear because nothing observes the Future.
    We register ``_log_publish_result`` so async failures surface in
    the log instead of evaporating, and ``close()`` calls
    ``publisher.stop()`` to synchronously flush pending batches —
    Cloud Run gives a 10 s SIGTERM grace and the default 0.01 s batch
    latency means several unflushed batches can be in flight when an
    instance scales to zero.
    """

    def __init__(
        self,
        project_id: str | None = None,
        topic_id: str | None = None,
    ) -> None:
        # Imported lazily so the no-op default never imports the
        # Pub/Sub client. ``test_off_does_not_touch_filesystem`` asserts
        # on this.
        from google.cloud import pubsub_v1  # noqa: WPS433

        self._project_id = (project_id or os.environ.get("PUBSUB_PROJECT_ID", "")).strip()
        self._topic_id = (topic_id or os.environ.get("METERING_PUBSUB_TOPIC", "usage-metering")).strip()
        if not self._project_id:
            raise RuntimeError("PubSubSink: PUBSUB_PROJECT_ID must be set")
        self._publisher = pubsub_v1.PublisherClient()
        self._topic_path = self._publisher.topic_path(
            self._project_id,
            self._topic_id,
        )

    @property
    def topic_path(self) -> str:
        return self._topic_path

    @staticmethod
    def _log_publish_result(future: Any) -> None:
        """Done-callback for the publish Future.

        Logs async failures at ERROR. ``future.result()`` re-raises the
        SDK's exception; we never let it propagate out of the callback
        thread (the SDK would then swallow it silently anyway, but
        being explicit keeps the contract obvious).
        """
        try:
            future.result(timeout=0)
        except Exception:  # noqa: BLE001
            logger.exception("METERING_PUBSUB_ASYNC_EMIT_ERROR")

    def emit(self, event: MeteringEvent) -> None:
        try:
            future = self._publisher.publish(
                self._topic_path,
                event.to_json().encode("utf-8"),
                # Attributes let the Go consumer filter without parsing
                # the body — handy for per-user subscription routing.
                user_id=event.user_id or "",
                sim_id=event.sim_id or "",
            )
            # Without this callback, errors raised on the publish
            # background thread (topic missing, IAM denied, etc.) are
            # silently dropped.
            future.add_done_callback(self._log_publish_result)
        except Exception:  # noqa: BLE001
            logger.exception("METERING_PUBSUB_EMIT_ERROR")

    def close(self) -> None:
        # stop() flushes pending batches synchronously and waits for
        # all in-flight publishes to complete. Without this, anything
        # the SDK's batcher hasn't sent yet at SIGTERM is dropped on
        # Cloud Run scale-to-zero. Errors are swallowed — close() is
        # called from shutdown paths where raising would mask the
        # original termination signal.
        try:
            self._publisher.stop()
        except Exception:  # noqa: BLE001
            logger.exception("METERING_PUBSUB_CLOSE_ERROR")


# ---------------------------------------------------------------------------
# Sink factory
# ---------------------------------------------------------------------------


def build_sink_from_env() -> MeteringSink:
    """Construct the metering sink dictated by the current env.

    Returns :class:`NullSink` when ``METERING_SINK`` is unset, blank,
    or ``none``. This is the guard that makes the metering pipeline a
    true no-op when nothing has explicitly opted in.

    Unknown sink names log a warning and fall back to :class:`NullSink`
    rather than raise — a typo in a deployment env var must not
    break the simulation.
    """
    name = _sink_name()
    if name in ("", "none"):
        return NullSink()
    if name == "stdout":
        return StdoutSink()
    if name == "sqlite":
        return SqliteSink()
    if name == "pubsub":
        return PubSubSink()
    logger.warning(
        "METERING_SINK=%r is not recognised; falling back to NullSink",
        name,
    )
    return NullSink()


# ---------------------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------------------


def _state_from_context(callback_context: Any) -> dict[str, Any]:
    """Best-effort read of session state out of a CallbackContext.

    ADK exposes ``state`` as a dict-like proxy on CallbackContext. We
    coerce defensively so that tests can pass plain MagicMocks without
    needing to set up an ADK session.
    """
    state = getattr(callback_context, "state", None)
    if state is None:
        return {}
    try:
        # ADK State defines neither keys() nor __iter__, so dict(state) fails
        # on the real proxy. Prefer its to_dict() snapshot when available.
        if hasattr(state, "to_dict"):
            return dict(state.to_dict())
        return dict(state)
    except Exception:
        return {}


def _agent_name(callback_context: Any) -> Optional[str]:
    name = getattr(callback_context, "agent_name", None)
    return str(name) if name else None


def _user_id(callback_context: Any, tenant_state: dict[str, Any]) -> str:
    # Prefer the user the gateway injected into session state; fall
    # back to whatever ADK has on the invocation (which is the same
    # value when the gateway populated it).
    uid = tenant_state.get("user_id")
    if uid:
        return str(uid)
    uid = getattr(callback_context, "user_id", None)
    return str(uid) if uid else "unknown"


def _sim_id(state: dict[str, Any]) -> Optional[str]:
    for key in ("simulation_id", "sim_id"):
        v = state.get(key)
        if v:
            return str(v)
    return None


def _usage_fields(llm_response: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Extract (prompt, completion, total) tokens or three Nones.

    The "never invent data" rule means a missing ``usage_metadata`` →
    every field is None. Callers MUST NOT substitute 0.
    """
    usage = getattr(llm_response, "usage_metadata", None)
    if usage is None:
        return None, None, None

    def _coerce(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return (
        _coerce(getattr(usage, "prompt_token_count", None)),
        _coerce(getattr(usage, "candidates_token_count", None)),
        _coerce(getattr(usage, "total_token_count", None)),
    )


def build_event(
    callback_context: Any,
    llm_response: Any,
    *,
    model: Optional[str] = None,
) -> MeteringEvent:
    """Build a MeteringEvent from ADK callback args.

    Exposed for tests; the plugin uses this internally.
    """
    state = _state_from_context(callback_context)
    tenant_state: dict[str, Any] = state.get("tenant") or {}
    prompt, completion, total = _usage_fields(llm_response)
    return MeteringEvent(
        user_id=_user_id(callback_context, tenant_state),
        sim_id=_sim_id(state),
        agent_type=_agent_name(callback_context),
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        timestamp_iso=_now_iso(),
    )


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


def _is_complete_response(llm_response: Any) -> bool:
    """Should this response be metered?

    We only count the final, non-partial chunk so a streaming response
    isn't billed once per token. Mirrors the same guard in
    :class:`BaseDashLogPlugin.after_model_callback`.
    """
    if getattr(llm_response, "partial", False) is True:
        return False
    if getattr(llm_response, "turn_complete", None) is False:
        return False
    return True


class MeteringPlugin:
    """ADK plugin that emits one :class:`MeteringEvent` per model turn.

    Constructed by :func:`agents.utils.factory.create_simulation_runner`.
    When ``METERING_SINK`` is unset the constructor short-circuits to
    a :class:`NullSink` and the plugin's hot path is a single attribute
    lookup plus a function call that returns immediately.

    Implementation note: we subclass ADK's ``BasePlugin`` lazily so
    importing this module never requires ADK to be present. The
    ``__init_subclass__`` dance keeps the import deferred until the
    plugin is actually instantiated, which keeps the metering tests
    fast (no ADK boot) and the dispatcher unit tests independent of
    the plugin's transitive deps.
    """

    name = "metering"

    def __init__(self, sink: MeteringSink | None = None) -> None:
        # ADK BasePlugin wants a ``name`` kwarg; we wrap to avoid a
        # hard top-level import of ADK in this module.
        try:
            from google.adk.plugins.base_plugin import BasePlugin  # noqa: WPS433

            BasePlugin.__init__(self, name=self.name)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            # Tests run without an ADK Runner so missing base class is
            # acceptable — the plugin still exposes the callback
            # methods that the Runner would have invoked.
            pass
        self._sink: MeteringSink = sink or build_sink_from_env()
        # Map invocation_id -> model name, captured in before_model_callback
        # so after_model_callback can stamp it on the event. ADK doesn't
        # put the model on the response side.
        self._model_for_invocation: dict[str, str] = {}

    @property
    def sink(self) -> MeteringSink:
        return self._sink

    # --- ADK hooks ---------------------------------------------------------

    async def before_model_callback(
        self,
        *,
        callback_context: Any,
        llm_request: Any,
    ) -> None:
        """Capture the model name for later use by after_model_callback."""
        if isinstance(self._sink, NullSink):
            return None
        try:
            model = getattr(llm_request, "model", None)
            inv_id = getattr(callback_context, "invocation_id", None)
            if model and inv_id:
                self._model_for_invocation[str(inv_id)] = str(model)
        except Exception:  # noqa: BLE001
            logger.debug("METERING_BEFORE_MODEL_NOTE", exc_info=True)
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: Any,
        llm_response: Any,
    ) -> None:
        """Emit one :class:`MeteringEvent` per finalized model turn."""
        if isinstance(self._sink, NullSink):
            return None
        if not _is_complete_response(llm_response):
            return None
        try:
            inv_id = getattr(callback_context, "invocation_id", None)
            model = self._model_for_invocation.pop(str(inv_id), None) if inv_id else None
            event = build_event(callback_context, llm_response, model=model)
            # Offload synchronous sink.emit() (e.g. SqliteSink disk I/O)
            # to a thread so we don't stall the asyncio event loop.
            await asyncio.to_thread(self._sink.emit, event)
        except Exception:  # noqa: BLE001
            # Metering MUST NEVER break a simulation — the worst that
            # may happen is an under-count, surfaced as `unknown` in
            # the dashboard.
            logger.exception("METERING_EMIT_ERROR")
        return None


# ---------------------------------------------------------------------------
# Backward-compatibility alias
# ---------------------------------------------------------------------------


async def after_model_callback(*args: Any, **kwargs: Any) -> Optional[Any]:
    """Legacy no-op kept for compatibility with the Phase 0 import path.

    Anyone still importing :func:`after_model_callback` directly gets
    a true no-op — no events, no sinks. The real wiring is via
    :class:`MeteringPlugin` constructed in
    :mod:`agents.utils.factory`.
    """
    return None


__all__ = [
    "MeteringEvent",
    "MeteringPlugin",
    "MeteringSink",
    "NullSink",
    "PubSubSink",
    "SqliteSink",
    "StdoutSink",
    "after_model_callback",
    "build_event",
    "build_sink_from_env",
]
