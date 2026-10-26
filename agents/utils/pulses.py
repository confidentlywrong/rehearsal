# Copyright 2026 Google LLC
# Modifications copyright 2026 The Confidently Wrong Authors
# Changed for The Rehearsal: adapted simulation, agents, and infrastructure for leadership scenario rehearsal.
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Optional, Any, Dict, List

from agents.utils import redis_pool
from agents.utils import tenant_keys
from gen_proto.gateway import gateway_pb2

logger = logging.getLogger(__name__)

_NUM_PUBLISH_WORKERS = 16

# P1-7 (2026-06-23 audit): publish-queue capacity is operator-tunable so
# burst capacity can be raised without a code change. Default 50_000
# (was 10_000) gives the worker pool roughly 5x more headroom before any
# producer hits asyncio.QueueFull. Capped at 500_000 to bound RSS growth
# under a stuck-worker scenario (each entry is a serialized wrapper, ~1KB
# average → ~500MB worst case at the cap).
_PULSE_QUEUE_MAXSIZE: int = max(
    100,
    min(500_000, int(os.environ.get("PULSE_QUEUE_MAXSIZE", "50000"))),
)
# Emit a WARN once per crossing of this fill-percentage so operators can see
# producer-vs-consumer imbalance long before the queue saturates. 0 disables.
_PULSE_QUEUE_HIGH_WATER_PCT: int = max(
    0,
    min(100, int(os.environ.get("PULSE_QUEUE_HIGH_WATER_PCT", "80"))),
)
# Edge-trigger latch per loop so we WARN on the first crossing rather than
# spamming on every put once the queue is hot. Cleared when queue drops
# back below the threshold.
_high_water_latched: dict[int, bool] = {}

# Retry policy for the pipeline publish path (added 2026-06-24 to replace
# the silent single-attempt drop). Pool exhaustion under 1000-runner bursts
# can briefly stall a publish; three attempts with exponential backoff give
# the pool time to drain without making the worker block forever.
_MAX_PUBLISH_ATTEMPTS = 3
_PUBLISH_RETRY_BACKOFF_S: tuple[float, ...] = (0.1, 0.3, 1.0)

# Cumulative drop counters. Incremented when a batch is unrecoverably
# dropped (Redis client unavailable, queue full, or all retries exhausted).
# Exposed via get_drop_stats() for tests and future health endpoints.
_dropped_batches_total: int = 0
_dropped_messages_total: int = 0

# H6 fix: Scope queue/workers per event loop. The dispatcher runs in its
# own thread with its own loop; module-level globals bound to the wrong
# loop caused "Future attached to a different loop" errors or silent drops.
_loop_state: dict[int, tuple["asyncio.Queue[bytes]", list["asyncio.Task[None]"]]] = {}


def get_drop_stats() -> dict[str, int]:
    """Return cumulative drop counters for monitoring/observability."""
    return {
        "dropped_batches": _dropped_batches_total,
        "dropped_messages": _dropped_messages_total,
    }


def _reset_drop_stats() -> None:
    """Reset drop counters. Test-only — production code never calls this."""
    global _dropped_batches_total, _dropped_messages_total
    _dropped_batches_total = 0
    _dropped_messages_total = 0


def _get_queue() -> asyncio.Queue[bytes]:
    """Lazy-init the publish queue for the *current* event loop."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    if key not in _loop_state:
        _loop_state[key] = (asyncio.Queue(maxsize=_PULSE_QUEUE_MAXSIZE), [])
    return _loop_state[key][0]


def reset() -> None:
    """Reset publish state. Call between test cases or on shutdown."""
    for key, (queue, tasks) in list(_loop_state.items()):
        for task in tasks:
            task.cancel()
        tasks.clear()
    _loop_state.clear()
    _high_water_latched.clear()
    _reset_drop_stats()


def _get_redis_client():
    return redis_pool.get_shared_redis_client()


async def _publish_worker() -> None:
    """Background task: drain queue, pipeline publishes to Redis with bounded retry."""
    global _dropped_batches_total, _dropped_messages_total
    while True:
        # Block for at least one item
        queue = _get_queue()
        batch: list[bytes] = [await queue.get()]
        # Drain any additional ready items (up to 100)
        while not queue.empty() and len(batch) < 100:
            batch.append(queue.get_nowait())

        r = _get_redis_client()
        if r is None:
            _dropped_batches_total += 1
            _dropped_messages_total += len(batch)
            logger.error(
                "GATEWAY_DROP: Redis client unavailable, dropping %d messages (cumulative batches=%d msgs=%d)",
                len(batch),
                _dropped_batches_total,
                _dropped_messages_total,
            )
            continue

        last_err: Exception | None = None
        for attempt in range(_MAX_PUBLISH_ATTEMPTS):
            try:
                # Tenant-scope the broadcast channel name so each tenant's
                # collector only receives its own traffic (M3). SAAS_MODE=off
                # collapses to the legacy literal "gateway:broadcast" so the
                # single-tenant local stack and switchboard tests are
                # byte-identical.
                channel = tenant_keys.key_for_user(
                    os.environ.get("DISPATCHER_USER_ID", "").strip(),
                    "gateway",
                    "broadcast",
                )
                async with r.pipeline(transaction=False) as pipe:
                    for msg in batch:
                        pipe.publish(channel, msg)
                    t_pub_start = time.perf_counter()
                    await pipe.execute()
                    t_pub_end = time.perf_counter()
                logger.info(
                    "PERF_TRACE: batch_published count=%d elapsed_ms=%.1f attempt=%d",
                    len(batch),
                    (t_pub_end - t_pub_start) * 1000,
                    attempt + 1,
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt + 1 < _MAX_PUBLISH_ATTEMPTS:
                    backoff = _PUBLISH_RETRY_BACKOFF_S[attempt]
                    logger.warning(
                        "GATEWAY_RETRY: Pipeline publish failed (%d msgs, attempt %d/%d), retrying in %.2fs: %s",
                        len(batch),
                        attempt + 1,
                        _MAX_PUBLISH_ATTEMPTS,
                        backoff,
                        e,
                    )
                    await asyncio.sleep(backoff)
        if last_err is not None:
            _dropped_batches_total += 1
            _dropped_messages_total += len(batch)
            logger.error(
                "GATEWAY_DROP: Pipeline publish failed after %d attempts; "
                "dropping %d messages (cumulative batches=%d msgs=%d): %s",
                _MAX_PUBLISH_ATTEMPTS,
                len(batch),
                _dropped_batches_total,
                _dropped_messages_total,
                last_err,
            )


async def _ensure_worker() -> None:
    """Start the publish worker pool if not already running."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    if key not in _loop_state:
        _loop_state[key] = (asyncio.Queue(maxsize=_PULSE_QUEUE_MAXSIZE), [])
    queue, tasks = _loop_state[key]
    # Remove dead tasks
    tasks[:] = [t for t in tasks if not t.done()]
    # Start missing workers
    while len(tasks) < _NUM_PUBLISH_WORKERS:
        task = loop.create_task(_publish_worker())
        tasks.append(task)


async def _publish_to_gateway(wrapper_bytes: bytes) -> None:
    global _dropped_batches_total, _dropped_messages_total
    await _ensure_worker()
    queue = _get_queue()
    try:
        queue.put_nowait(wrapper_bytes)
    except asyncio.QueueFull:
        _dropped_batches_total += 1  # treat queue-overflow as a single-message batch
        _dropped_messages_total += 1
        logger.error(
            "GATEWAY_DROP: Publish queue full (maxsize=%d), dropping 1 message (cumulative batches=%d msgs=%d)",
            _PULSE_QUEUE_MAXSIZE,
            _dropped_batches_total,
            _dropped_messages_total,
        )
        return
    # P1-7 high-water-mark observability: edge-trigger a WARN when fill
    # crosses the configured percentage. Helps operators spot a stalled
    # worker or Redis backpressure before the queue actually saturates.
    if _PULSE_QUEUE_HIGH_WATER_PCT > 0:
        loop_key = id(asyncio.get_running_loop())
        size = queue.qsize()
        threshold = (_PULSE_QUEUE_MAXSIZE * _PULSE_QUEUE_HIGH_WATER_PCT) // 100
        if size >= threshold:
            if not _high_water_latched.get(loop_key, False):
                _high_water_latched[loop_key] = True
                logger.warning(
                    "GATEWAY_HIGH_WATER: publish queue at %d/%d (%.0f%%) "
                    "— producers outpacing the %d-worker pool. Consider raising "
                    "PULSE_QUEUE_MAXSIZE or investigating Redis publish latency.",
                    size,
                    _PULSE_QUEUE_MAXSIZE,
                    100.0 * size / max(1, _PULSE_QUEUE_MAXSIZE),
                    _NUM_PUBLISH_WORKERS,
                )
        elif _high_water_latched.get(loop_key, False):
            _high_water_latched[loop_key] = False


async def emit_gateway_message(
    origin: Dict[str, str],
    destination: List[str],
    status: str,
    msg_type: str,
    event: str,
    data: Any,
    metadata: Optional[Dict[str, Any]] = None,
    simulation_id: Optional[str] = None,
):
    """Unified function to emit standardized gateway messages."""
    try:
        # Import dynamically to avoid circular dependencies
        from agents.utils.plugins import _safe_json_dumps
    except ImportError:

        def _safe_json_dumps(obj: Any) -> str:
            return json.dumps(obj, default=str)

    origin_msg = gateway_pb2.Origin(
        type=origin.get("type", "agent"),
        id=origin.get("id", "unknown"),
        session_id=origin.get("session_id", "system"),
    )

    # Serialize payload based on data type.
    if isinstance(data, (dict, list)):
        payload_bytes = _safe_json_dumps(data).encode("utf-8")
        # M11: Intentional promotion — when the caller passes structured
        # data (dict/list) but labels msg_type="text", we correct it to
        # "json" so the frontend knows to JSON.parse() the payload.
        # This is deliberate, not a bug.  Callers who truly want text
        # should pass a string, not a dict.
        if msg_type == "text":
            msg_type = "json"
    else:
        # Pydantic models and other objects will be handled by _safe_json_dumps
        payload_str = _safe_json_dumps(data) if not isinstance(data, str) else data
        payload_bytes = payload_str.encode("utf-8")

    meta_bytes = b""
    if metadata:
        meta_bytes = _safe_json_dumps(metadata).encode("utf-8")

    wrapper = gateway_pb2.Wrapper(
        timestamp=datetime.now().isoformat(),
        type=msg_type,
        request_id=f"gw-{uuid.uuid4().hex[:12]}",
        session_id=origin_msg.session_id,  # Legacy fallback for routing
        payload=payload_bytes,
        origin=origin_msg,
        destination=destination,
        status=status,
        event=event,
        metadata=meta_bytes,
    )

    if simulation_id:
        wrapper.simulation_id = simulation_id

    await _publish_to_gateway(wrapper.SerializeToString())
    logger.debug(f"GATEWAY_MSG: [{event}] from {origin_msg.id} to {destination} ({status})")


# [START emit_narrative_pulse]
async def emit_narrative_pulse(
    session_id: str,
    text: str,
    author: Optional[str] = None,
    type: str = "text",
    metadata: Optional[dict] = None,
    simulation_id: Optional[str] = None,
):
    """Legacy wrapper for narrative pulses. Maps to emit_gateway_message."""
    await emit_gateway_message(
        origin={"type": "agent", "id": author or "system", "session_id": session_id},
        destination=[session_id],  # Session-isolated by default
        status="success",
        msg_type="json",
        event=type,
        data={"text": text},
        metadata=metadata,
        simulation_id=simulation_id,
    )


async def emit_inter_agent_pulse(
    session_id: str,
    from_agent: str,
    to_agent: str,
    message: str,
    direction: str = "request",
    simulation_id: Optional[str] = None,
):
    """Legacy wrapper for inter-agent communication visibility."""
    if direction == "request":
        text = f"📢 {from_agent} -> {to_agent}: {message}"
    else:
        text = f"📨 {from_agent} <- {to_agent}: {message}"

    await emit_narrative_pulse(
        session_id=session_id,
        text=text,
        type="inter-agent",
        author=from_agent,
        simulation_id=simulation_id,
    )


# [END emit_narrative_pulse]


async def emit_telemetry_pulse(
    session_id: str,
    payload: dict,
    type: str = "agent-telemetry",
    simulation_id: Optional[str] = None,
):
    """Legacy wrapper for raw telemetry events. Maps to emit_gateway_message."""
    agent_id = payload.get("agent", "system")
    event_type = payload.get("type", "telemetry")
    status = "error" if "error" in event_type else "info"

    await emit_gateway_message(
        origin={"type": "agent", "id": agent_id, "session_id": session_id},
        destination=[session_id],
        status=status,
        msg_type="telemetry",
        event=event_type,
        data=payload,
        metadata={"seq": payload.get("seq")},
        simulation_id=simulation_id,
    )
