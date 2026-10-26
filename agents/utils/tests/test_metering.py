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

"""Tests for :mod:`agents.utils.metering` (per-user).

Coverage:

* ``TestBuildSinkFromEnv`` — env-driven factory selection. NullSink is
  the default when ``METERING_SINK`` is unset/blank/``none``.
* ``TestNullSink`` — guaranteed no-op.
* ``TestStdoutSink`` — JSONL emission and stream flushing.
* ``TestSqliteSink`` — schema creation, INSERTs land, columns map 1:1
  to the Postgres ``usage_ledger`` shape.
* ``TestBuildEvent`` — extraction from a fake CallbackContext; honours
  the "never invent data" rule when ``usage_metadata`` is absent.
* ``TestMeteringPlugin`` — full plugin lifecycle: before captures model,
  after emits an event, partial responses are skipped, sink failure
  is swallowed.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agents.utils import metering


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear every metering-related env var so each test starts clean."""
    for var in (
        "METERING_SINK",
        "METERING_SQLITE_PATH",
        "METERING_PUBSUB_TOPIC",
        "PUBSUB_PROJECT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _fake_ctx(
    *,
    state=None,
    agent_name="planner",
    invocation_id="inv-1",
    user_id="unknown",
):
    """Construct a CallbackContext stand-in that exposes the attrs
    the metering module reads. Plain SimpleNamespace is enough because
    ``build_event`` only does attribute access + dict coercion.
    """
    return SimpleNamespace(
        state=state or {},
        agent_name=agent_name,
        invocation_id=invocation_id,
        user_id=user_id,
    )


def _fake_response(*, prompt=None, completion=None, total=None, partial=False):
    """Construct an LlmResponse-shaped object with usage_metadata."""
    if prompt is None and completion is None and total is None:
        usage = None
    else:
        usage = SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=completion,
            total_token_count=total,
        )
    return SimpleNamespace(
        usage_metadata=usage,
        partial=partial,
        turn_complete=not partial,
        content=None,
    )


# ---------------------------------------------------------------------------
# build_sink_from_env
# ---------------------------------------------------------------------------


class TestBuildSinkFromEnv:
    """Sink selection by env."""

    def test_unset_returns_null_sink(self):
        assert isinstance(metering.build_sink_from_env(), metering.NullSink)

    def test_blank_returns_null_sink(self, monkeypatch):
        monkeypatch.setenv("METERING_SINK", "")
        assert isinstance(metering.build_sink_from_env(), metering.NullSink)

    def test_none_returns_null_sink(self, monkeypatch):
        monkeypatch.setenv("METERING_SINK", "none")
        assert isinstance(metering.build_sink_from_env(), metering.NullSink)

    def test_stdout(self, monkeypatch):
        monkeypatch.setenv("METERING_SINK", "stdout")
        assert isinstance(metering.build_sink_from_env(), metering.StdoutSink)

    def test_sqlite(self, monkeypatch, tmp_path):
        monkeypatch.setenv("METERING_SINK", "sqlite")
        monkeypatch.setenv("METERING_SQLITE_PATH", str(tmp_path / "metering.db"))
        sink = metering.build_sink_from_env()
        assert isinstance(sink, metering.SqliteSink)
        assert sink.path == tmp_path / "metering.db"

    def test_unknown_sink_falls_back_to_null(self, monkeypatch):
        """A typo in METERING_SINK must not break the simulation."""
        monkeypatch.setenv("METERING_SINK", "bigquery-typo")
        sink = metering.build_sink_from_env()
        assert isinstance(sink, metering.NullSink)

    def test_none_default_does_not_touch_filesystem(self, monkeypatch, tmp_path):
        """Even with METERING_SQLITE_PATH set, no sink env → no file."""
        monkeypatch.setenv(
            "METERING_SQLITE_PATH",
            str(tmp_path / "leaked.db"),
        )
        metering.build_sink_from_env()
        assert not (tmp_path / "leaked.db").exists()


# ---------------------------------------------------------------------------
# NullSink
# ---------------------------------------------------------------------------


class TestNullSink:
    """NullSink does nothing, full stop."""

    def test_emit_returns_none_and_does_nothing(self):
        sink = metering.NullSink()
        ev = metering.MeteringEvent(
            user_id="unknown",
            sim_id=None,
            agent_type=None,
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            timestamp_iso="2026-01-01T00:00:00Z",
        )
        assert sink.emit(ev) is None
        assert sink.close() is None


# ---------------------------------------------------------------------------
# StdoutSink
# ---------------------------------------------------------------------------


class TestStdoutSink:
    def test_emits_one_jsonl_line_per_event(self):
        buf = io.StringIO()
        sink = metering.StdoutSink(stream=buf)
        ev = metering.MeteringEvent(
            user_id="u-1",
            sim_id="s-1",
            agent_type="planner",
            model="gemini-3-flash-preview",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            timestamp_iso="2026-01-01T00:00:00.000000Z",
        )
        sink.emit(ev)
        sink.emit(ev)
        lines = buf.getvalue().splitlines()
        assert len(lines) == 2
        parsed = json.loads(lines[0])
        assert parsed["user_id"] == "u-1"
        assert parsed["total_tokens"] == 120
        # org_id must not leak back into the wire format.
        assert "org_id" not in parsed

    def test_emit_with_null_tokens_preserves_nulls(self):
        """Never invent data: None must serialise as JSON null."""
        buf = io.StringIO()
        sink = metering.StdoutSink(stream=buf)
        ev = metering.MeteringEvent(
            user_id="unknown",
            sim_id=None,
            agent_type=None,
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            timestamp_iso="2026-01-01T00:00:00Z",
        )
        sink.emit(ev)
        parsed = json.loads(buf.getvalue())
        assert parsed["prompt_tokens"] is None
        assert parsed["total_tokens"] is None


# ---------------------------------------------------------------------------
# SqliteSink
# ---------------------------------------------------------------------------


class TestSqliteSink:
    def test_creates_schema_on_construct(self, tmp_path):
        path = tmp_path / "metering.db"
        metering.SqliteSink(path=path)
        assert path.exists()
        with sqlite3.connect(path) as conn:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metering_ledger'")
            assert cur.fetchone() is not None

    def test_schema_has_no_org_id_column(self, tmp_path):
        """The per-user schema must not carry the legacy org_id column."""
        path = tmp_path / "metering.db"
        metering.SqliteSink(path=path)
        with sqlite3.connect(path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(metering_ledger)")}
        assert "org_id" not in cols
        assert "user_id" in cols

    def test_emit_inserts_row(self, tmp_path):
        sink = metering.SqliteSink(path=tmp_path / "m.db")
        ev = metering.MeteringEvent(
            user_id="u-1",
            sim_id="s-1",
            agent_type="planner",
            model="gemini-3-flash-preview",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            timestamp_iso="2026-01-01T00:00:00Z",
        )
        sink.emit(ev)
        with sqlite3.connect(sink.path) as conn:
            row = conn.execute(
                "SELECT user_id, sim_id, agent_type, prompt_tokens, "
                "completion_tokens, total_tokens, drained "
                "FROM metering_ledger"
            ).fetchone()
        assert row == ("u-1", "s-1", "planner", 10, 5, 15, 0)

    def test_emit_handles_null_token_columns(self, tmp_path):
        sink = metering.SqliteSink(path=tmp_path / "m.db")
        ev = metering.MeteringEvent(
            user_id="unknown",
            sim_id=None,
            agent_type=None,
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            timestamp_iso="2026-01-01T00:00:00Z",
        )
        sink.emit(ev)
        with sqlite3.connect(sink.path) as conn:
            row = conn.execute("SELECT prompt_tokens, completion_tokens, total_tokens FROM metering_ledger").fetchone()
        assert row == (None, None, None)

    def test_multiple_emits_all_persist(self, tmp_path):
        sink = metering.SqliteSink(path=tmp_path / "m.db")
        for i in range(5):
            sink.emit(
                metering.MeteringEvent(
                    user_id="u-1",
                    sim_id=f"s-{i}",
                    agent_type="planner",
                    model=None,
                    prompt_tokens=i,
                    completion_tokens=i,
                    total_tokens=2 * i,
                    timestamp_iso="2026-01-01T00:00:00Z",
                )
            )
        with sqlite3.connect(sink.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM metering_ledger").fetchone()[0]
        assert count == 5

    def test_creates_parent_dir(self, tmp_path):
        """Construction must mkdir -p the parent so deploy scripts can
        point at a fresh path without pre-creating it."""
        nested = tmp_path / "nested" / "subdir" / "m.db"
        metering.SqliteSink(path=nested)
        assert nested.exists()
        assert nested.parent.is_dir()


# ---------------------------------------------------------------------------
# build_event
# ---------------------------------------------------------------------------


class TestBuildEvent:
    def test_full_extraction(self):
        ctx = _fake_ctx(
            state={
                "tenant": {"user_id": "u-1"},
                "simulation_id": "sim-42",
            },
            agent_name="leadership_planner",
        )
        resp = _fake_response(prompt=100, completion=20, total=120)
        ev = metering.build_event(ctx, resp, model="gemini-3-flash-preview")
        assert ev.user_id == "u-1"
        assert ev.sim_id == "sim-42"
        assert ev.agent_type == "leadership_planner"
        assert ev.model == "gemini-3-flash-preview"
        assert ev.prompt_tokens == 100
        assert ev.completion_tokens == 20
        assert ev.total_tokens == 120
        assert ev.timestamp_iso  # RFC3339 string

    def test_missing_usage_metadata_emits_nulls(self):
        """Never invent data: no usage_metadata → token fields None."""
        ctx = _fake_ctx(state={"tenant": {"user_id": "u-1"}})
        resp = _fake_response()  # no usage
        ev = metering.build_event(ctx, resp)
        assert ev.prompt_tokens is None
        assert ev.completion_tokens is None
        assert ev.total_tokens is None
        assert ev.user_id == "u-1"

    def test_user_id_falls_back_to_ctx(self):
        """No user_id in tenant state → read off CallbackContext."""
        ctx = _fake_ctx(state={"tenant": {}}, user_id="adk-user-1")
        ev = metering.build_event(ctx, _fake_response(total=1))
        assert ev.user_id == "adk-user-1"

    def test_user_id_absent_yields_unknown(self):
        """No user_id anywhere → 'unknown'."""
        ctx = _fake_ctx(state={})
        ev = metering.build_event(ctx, _fake_response(total=1))
        assert ev.user_id == "unknown"

    def test_sim_id_aliases(self):
        """Both `simulation_id` and `sim_id` state keys are honoured."""
        ctx = _fake_ctx(state={"sim_id": "via-short-key"})
        ev = metering.build_event(ctx, _fake_response(total=1))
        assert ev.sim_id == "via-short-key"


# ---------------------------------------------------------------------------
# MeteringPlugin
# ---------------------------------------------------------------------------


class _RecordingSink:
    """In-memory sink for plugin tests."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def close(self):
        pass


class TestMeteringPlugin:
    def test_after_callback_emits_event(self):
        sink = _RecordingSink()
        plugin = metering.MeteringPlugin(sink=sink)
        ctx = _fake_ctx(
            state={
                "tenant": {"user_id": "u-1"},
                "simulation_id": "sim-7",
            }
        )
        resp = _fake_response(prompt=50, completion=10, total=60)

        asyncio.run(
            plugin.after_model_callback(
                callback_context=ctx,
                llm_response=resp,
            )
        )

        assert len(sink.events) == 1
        ev = sink.events[0]
        assert ev.user_id == "u-1"
        assert ev.total_tokens == 60
        assert ev.sim_id == "sim-7"

    def test_before_callback_captures_model_for_event(self):
        sink = _RecordingSink()
        plugin = metering.MeteringPlugin(sink=sink)
        ctx = _fake_ctx(state={"tenant": {"user_id": "u-1"}}, invocation_id="inv-99")
        req = SimpleNamespace(model="gemini-3-flash-preview")
        resp = _fake_response(prompt=1, completion=1, total=2)

        asyncio.run(
            plugin.before_model_callback(
                callback_context=ctx,
                llm_request=req,
            )
        )
        asyncio.run(
            plugin.after_model_callback(
                callback_context=ctx,
                llm_response=resp,
            )
        )

        assert sink.events[0].model == "gemini-3-flash-preview"
        # Model map cleared after read so memory doesn't grow.
        assert "inv-99" not in plugin._model_for_invocation

    def test_partial_response_skipped(self):
        """Streaming partial chunks must not be metered."""
        sink = _RecordingSink()
        plugin = metering.MeteringPlugin(sink=sink)
        ctx = _fake_ctx(state={"tenant": {"user_id": "u-1"}})
        resp = _fake_response(prompt=10, completion=5, total=15, partial=True)

        asyncio.run(
            plugin.after_model_callback(
                callback_context=ctx,
                llm_response=resp,
            )
        )

        assert sink.events == []

    def test_null_sink_short_circuits(self):
        """A NullSink plugin must not even build an event."""
        sink = metering.NullSink()
        plugin = metering.MeteringPlugin(sink=sink)
        # If it tried to build_event it would touch ctx attrs; pass
        # something that would explode on attribute access to prove
        # the early-return path runs first.
        bomb = object()
        # Should not raise.
        asyncio.run(
            plugin.after_model_callback(
                callback_context=bomb,
                llm_response=bomb,
            )
        )

    def test_sink_exception_is_swallowed(self):
        """Metering MUST NEVER break a simulation."""

        class _BrokenSink:
            def emit(self, event):
                raise RuntimeError("metering down")

            def close(self):
                pass

        plugin = metering.MeteringPlugin(sink=_BrokenSink())
        ctx = _fake_ctx(state={"tenant": {"user_id": "u-1"}})
        resp = _fake_response(prompt=1, completion=1, total=2)

        # Must not raise.
        asyncio.run(
            plugin.after_model_callback(
                callback_context=ctx,
                llm_response=resp,
            )
        )

    def test_env_sink_selection_by_default(self, monkeypatch, tmp_path):
        """No sink arg + METERING_SINK=sqlite → SqliteSink picked."""
        monkeypatch.setenv("METERING_SINK", "sqlite")
        monkeypatch.setenv(
            "METERING_SQLITE_PATH",
            str(tmp_path / "m.db"),
        )
        plugin = metering.MeteringPlugin()
        assert isinstance(plugin.sink, metering.SqliteSink)


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


class TestLegacyShim:
    def test_legacy_after_model_callback_export_is_noop(self):
        """The Phase-0 import path is preserved as a no-op."""
        assert asyncio.run(metering.after_model_callback()) is None
        assert (
            asyncio.run(
                metering.after_model_callback("a", b=1),
            )
            is None
        )


# ---------------------------------------------------------------------------
# PubSubSink async-failure handling (P0-4, 2026-06-24)
# ---------------------------------------------------------------------------


class TestPubSubSinkFutureCallback:
    """Verify PubSubSink observes the publish Future so async failures surface.

    Without ``add_done_callback`` the SDK silently swallows server-side
    publish failures (topic missing, IAM denied, quota). Cloud Run
    scale-to-zero also drops any unflushed batch, so ``close()`` must
    call ``publisher.stop()`` to flush synchronously.
    """

    @pytest.fixture
    def fake_pubsub_module(self, monkeypatch):
        """Inject a fake google.cloud.pubsub_v1 so PubSubSink imports succeed
        without touching the real client."""
        from unittest.mock import MagicMock
        from types import ModuleType

        ps = ModuleType("google.cloud.pubsub_v1")

        # Build a fake PublisherClient that records publish calls.
        class _FakeClient:
            def __init__(self, *args, **kwargs):
                self.published = []
                self.stopped = False

            def topic_path(self, project, topic):
                return f"projects/{project}/topics/{topic}"

            def publish(self, topic_path, data, **attrs):
                fut = MagicMock()
                fut._cbs = []
                fut.add_done_callback = lambda cb: fut._cbs.append(cb)
                # Default: successful future (result() returns msg id).
                fut.result = MagicMock(return_value="msg-id-1")
                self.published.append((topic_path, data, attrs, fut))
                return fut

            def stop(self):
                self.stopped = True

        ps.PublisherClient = _FakeClient

        # In test_metering, we want to mock google.cloud.pubsub_v1 so that
        # pubsub_v1.PublisherClient resolves to our fake.
        monkeypatch.setattr("google.cloud.pubsub_v1.PublisherClient", _FakeClient)
        monkeypatch.setenv("PUBSUB_PROJECT_ID", "test-proj")
        return ps

    def test_emit_registers_done_callback(self, fake_pubsub_module):
        """emit() must call future.add_done_callback so async errors are logged."""
        sink = metering.PubSubSink()
        ev = metering.MeteringEvent(
            user_id="u-1",
            sim_id="s-1",
            agent_type="planner",
            model="gemini",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            timestamp_iso="2026-01-01T00:00:00Z",
        )
        sink.emit(ev)
        # One publish, with a done-callback registered.
        assert len(sink._publisher.published) == 1
        _, _, _, fut = sink._publisher.published[0]
        assert len(fut._cbs) == 1, "emit() must register exactly one done-callback"

    def test_async_publish_failure_is_logged(self, fake_pubsub_module, caplog):
        """A failing future.result() in the callback must log at ERROR."""
        import logging
        from unittest.mock import MagicMock

        sink = metering.PubSubSink()
        ev = metering.MeteringEvent(
            user_id="u-1",
            sim_id="s-1",
            agent_type="planner",
            model="gemini",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            timestamp_iso="2026-01-01T00:00:00Z",
        )
        sink.emit(ev)
        _, _, _, fut = sink._publisher.published[0]
        # Simulate Pub/Sub returning an error after batch submission.
        fut.result = MagicMock(side_effect=RuntimeError("topic not found"))

        # Manually fire the callback the same way the SDK's background
        # thread would when the publish completes.
        with caplog.at_level(logging.ERROR, logger="agents.utils.metering"):
            for cb in fut._cbs:
                cb(fut)

        assert any("METERING_PUBSUB_ASYNC_EMIT_ERROR" in r.message for r in caplog.records), (
            "Async publish failure must surface as METERING_PUBSUB_ASYNC_EMIT_ERROR"
        )

    def test_close_flushes_publisher(self, fake_pubsub_module):
        """close() must call publisher.stop() to flush pending batches on shutdown."""
        sink = metering.PubSubSink()
        assert sink._publisher.stopped is False
        sink.close()
        assert sink._publisher.stopped is True, (
            "close() must call publisher.stop() so Cloud Run SIGTERM doesn't drop unflushed batches"
        )

    def test_close_swallows_stop_errors(self, fake_pubsub_module):
        """close() must not raise — it runs in shutdown paths where raising masks SIGTERM."""
        sink = metering.PubSubSink()
        sink._publisher.stop = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        # Must not raise.
        sink.close()
