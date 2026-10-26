# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
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

"""Tests for the publish batching worker in pulses.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agents.utils.pulses as pulses_mod


class TestPublishBatching:
    def setup_method(self) -> None:
        pulses_mod.reset()

    def teardown_method(self) -> None:
        pulses_mod.reset()

    @pytest.mark.asyncio
    async def test_messages_are_batched_via_pipeline(self) -> None:
        """Putting 10 messages should result in 1 pipeline.execute() call."""
        mock_pipeline = MagicMock()
        mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
        mock_pipeline.__aexit__ = AsyncMock(return_value=False)
        mock_pipeline.execute = AsyncMock()

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            # Enqueue 10 messages
            for i in range(10):
                await pulses_mod._publish_to_gateway(f"msg-{i}".encode())

            # Give the worker time to drain the queue
            await asyncio.sleep(0.1)

        # Pipeline should have been used at least once
        mock_redis.pipeline.assert_called()
        # All messages should have been published
        assert mock_pipeline.publish.call_count == 10
        # But execute should have been called far fewer times than 10
        assert mock_pipeline.execute.call_count < 10

    @pytest.mark.asyncio
    async def test_queue_full_drops_gracefully(self) -> None:
        """When the queue is full, new messages should be dropped, not crash."""
        mock_redis = MagicMock()

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            queue = pulses_mod._get_queue()
            # Fill the queue to capacity
            for i in range(queue.maxsize):
                queue.put_nowait(f"fill-{i}".encode())

            # This should NOT raise -- should log a warning and drop
            await pulses_mod._publish_to_gateway(b"overflow-message")

        # Queue should still be at capacity (overflow was dropped)
        queue = pulses_mod._get_queue()
        assert queue.qsize() == queue.maxsize

    @pytest.mark.asyncio
    async def test_worker_starts_on_first_publish(self) -> None:
        """The background worker pool should be created on first _publish_to_gateway call."""
        assert len(pulses_mod._loop_state) == 0

        mock_redis = MagicMock()

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            await pulses_mod._publish_to_gateway(b"trigger")

        loop = asyncio.get_running_loop()
        key = id(loop)
        assert key in pulses_mod._loop_state
        _, tasks = pulses_mod._loop_state[key]
        assert len(tasks) == pulses_mod._NUM_PUBLISH_WORKERS
        assert all(not t.done() for t in tasks)

    @pytest.mark.asyncio
    async def test_worker_continues_after_pipeline_error(self) -> None:
        """Worker should keep processing after a pipeline.execute() failure."""
        call_count = 0

        mock_pipeline = MagicMock()
        mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
        mock_pipeline.__aexit__ = AsyncMock(return_value=False)
        mock_pipeline.execute = AsyncMock()

        original_execute = mock_pipeline.execute

        async def execute_side_effect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Simulated Redis failure")
            return await original_execute()

        mock_pipeline.execute = AsyncMock(side_effect=execute_side_effect)

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            # First message will trigger a pipeline error
            await pulses_mod._publish_to_gateway(b"msg-fail")
            await asyncio.sleep(0.1)

            # Second message should still be processed
            await pulses_mod._publish_to_gateway(b"msg-ok")
            await asyncio.sleep(0.1)

        # Worker should have attempted pipeline at least twice
        assert mock_pipeline.execute.call_count >= 2
        # Workers should still be alive
        loop = asyncio.get_running_loop()
        key = id(loop)
        _, tasks = pulses_mod._loop_state[key]
        assert len(tasks) > 0
        assert any(not t.done() for t in tasks)

    @pytest.mark.asyncio
    async def test_worker_logs_when_redis_client_is_none(self) -> None:
        """Worker should log a warning when Redis client is unavailable."""
        with patch.object(pulses_mod, "_get_redis_client", return_value=None):
            await pulses_mod._publish_to_gateway(b"orphaned-message")
            await asyncio.sleep(0.1)

        # Workers should still be alive (not crashed)
        loop = asyncio.get_running_loop()
        key = id(loop)
        _, tasks = pulses_mod._loop_state[key]
        assert len(tasks) > 0
        assert any(not t.done() for t in tasks)
        # Queue should be empty (message was consumed and dropped)
        assert pulses_mod._get_queue().empty()

    @pytest.mark.asyncio
    async def test_multiple_workers_drain_concurrently(self) -> None:
        """Multiple workers should drain the queue in parallel."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
        mock_pipe.__aexit__ = AsyncMock(return_value=False)
        mock_pipe.execute = AsyncMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            # Queue 400 messages
            queue = pulses_mod._get_queue()
            for i in range(400):
                queue.put_nowait(f"msg-{i}".encode())

            # Start workers (should be 4 by default)
            await pulses_mod._ensure_worker()
            # Give workers time to drain
            await asyncio.sleep(0.5)

        # With 4 workers, we expect multiple pipeline.execute() calls
        # (each worker batches up to 100)
        assert mock_pipe.execute.call_count >= 4, (
            f"Expected at least 4 execute() calls with 4 workers, got {mock_pipe.execute.call_count}"
        )

        assert pulses_mod._get_queue().empty(), "Queue should be fully drained"


class TestPublishQueueReset:
    """Verify lazy-init Queue works across event loops."""

    def test_reset_clears_queue_and_workers(self):
        """After reset(), loop_state is empty."""
        import agents.utils.pulses as mod

        mod.reset()
        assert mod._loop_state == {}


def test_publish_workers_count():
    """NUM_PUBLISH_WORKERS should be 16 for high-throughput simulations."""
    from agents.utils.pulses import _NUM_PUBLISH_WORKERS

    assert _NUM_PUBLISH_WORKERS == 16


class TestPublishRetryAndDropCounters:
    """Verify the bounded-retry + drop-counter behaviour added 2026-06-24.

    Replaces the previous silent-drop behaviour where a single pipeline
    failure caused the whole batch to vanish with only a WARNING log.
    """

    def setup_method(self) -> None:
        pulses_mod.reset()

    def teardown_method(self) -> None:
        pulses_mod.reset()

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_does_not_increment_drops(self) -> None:
        """First attempt fails, second succeeds → no drop counted."""
        call_count = 0

        mock_pipeline = MagicMock()
        mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
        mock_pipeline.__aexit__ = AsyncMock(return_value=False)

        async def execute_side_effect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient redis blip")
            return None

        mock_pipeline.execute = AsyncMock(side_effect=execute_side_effect)

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            await pulses_mod._publish_to_gateway(b"will-retry")
            # Give worker time to fail once + backoff (0.1s) + retry succeed.
            await asyncio.sleep(0.3)

        stats = pulses_mod.get_drop_stats()
        assert stats == {"dropped_batches": 0, "dropped_messages": 0}, (
            f"Retry success should not count as a drop; got {stats}"
        )
        assert mock_pipeline.execute.call_count >= 2, "Worker should have retried"

    @pytest.mark.asyncio
    async def test_drops_after_max_attempts_exhausted(self) -> None:
        """All 3 attempts fail → batch counted as dropped."""
        mock_pipeline = MagicMock()
        mock_pipeline.__aenter__ = AsyncMock(return_value=mock_pipeline)
        mock_pipeline.__aexit__ = AsyncMock(return_value=False)
        mock_pipeline.execute = AsyncMock(side_effect=ConnectionError("redis permanently down"))

        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipeline)

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            await pulses_mod._publish_to_gateway(b"will-fail")
            # All 3 attempts: 0.1 + 0.3 = 0.4s of backoff plus exec time.
            await asyncio.sleep(1.0)

        stats = pulses_mod.get_drop_stats()
        assert stats == {"dropped_batches": 1, "dropped_messages": 1}, (
            f"Exhausted retries should count exactly one dropped batch; got {stats}"
        )
        assert mock_pipeline.execute.call_count == pulses_mod._MAX_PUBLISH_ATTEMPTS

    @pytest.mark.asyncio
    async def test_redis_client_none_counts_as_drop(self) -> None:
        """When the shared client is None the batch is dropped — must be counted."""
        with patch.object(pulses_mod, "_get_redis_client", return_value=None):
            await pulses_mod._publish_to_gateway(b"orphan-1")
            await pulses_mod._publish_to_gateway(b"orphan-2")
            await asyncio.sleep(0.1)

        stats = pulses_mod.get_drop_stats()
        # Both messages may land in one batch (drained together) or two
        # batches depending on worker timing; either way msgs==2.
        assert stats["dropped_messages"] == 2, f"Both orphans must count; got {stats}"
        assert stats["dropped_batches"] >= 1

    @pytest.mark.asyncio
    async def test_queue_full_counts_as_drop(self) -> None:
        """Overflow on the in-process queue must also bump the drop counter."""
        mock_redis = MagicMock()

        with patch.object(pulses_mod, "_get_redis_client", return_value=mock_redis):
            queue = pulses_mod._get_queue()
            for i in range(queue.maxsize):
                queue.put_nowait(f"fill-{i}".encode())
            await pulses_mod._publish_to_gateway(b"overflow")

        stats = pulses_mod.get_drop_stats()
        assert stats == {"dropped_batches": 1, "dropped_messages": 1}, (
            f"QueueFull overflow must count as a drop; got {stats}"
        )

    def test_get_drop_stats_returns_zero_on_fresh_module(self) -> None:
        """reset() must zero the counters."""
        pulses_mod._dropped_batches_total = 7
        pulses_mod._dropped_messages_total = 42
        pulses_mod.reset()
        assert pulses_mod.get_drop_stats() == {
            "dropped_batches": 0,
            "dropped_messages": 0,
        }
