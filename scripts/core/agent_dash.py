# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: hardened CORS env loading, WebSocket disconnect cleanup, and Pub/Sub subscription teardown.
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

import asyncio
import json
import logging
import os
from pathlib import Path
import random
import threading
import time
import uuid
from typing import List, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from google.cloud import pubsub_v1
from google.cloud.pubsub_v1.subscriber.message import Message

logger = logging.getLogger(__name__)

load_dotenv()

_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",") if o.strip()] or ["*"]
if (os.getenv("K_SERVICE") or os.getenv("CORS_REFUSE_WILDCARD", "").lower() == "true") and "*" in _cors_origins:
    _cors_origins = [o for o in _cors_origins if o != "*"]
app = FastAPI(root_path="/dash")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins and bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent_dash"}


def _resolve_project_id() -> str:
    """Resolve Pub/Sub project ID from env, falling back to test-project only on emulator."""
    pid = (
        os.getenv("PUBSUB_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("PROJECT_ID")
        or ""
    ).strip()
    if pid:
        return pid
    if os.getenv("PUBSUB_EMULATOR_HOST"):
        return "test-project"
    return ""


# Project & Pub/Sub config
PROJECT_ID = _resolve_project_id()
TOPIC_ID = os.getenv("PUBSUB_TOPIC_ID", "agent-telemetry")

# Active connections for live feed
connections: List[WebSocket] = []

# Global loop reference for thread-safe WebSocket broadcasting
main_loop: Optional[asyncio.AbstractEventLoop] = None

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INDEX_HTML_PATH = _REPO_ROOT / "web" / "agent-dash" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def get_index():
    global main_loop
    main_loop = asyncio.get_running_loop()
    try:
        with _INDEX_HTML_PATH.open(encoding="utf-8") as f:
            content = f.read()
            # Replace absolute paths with relative ones for path-based routing
            content = content.replace('src="/', 'src="./')
            content = content.replace('href="/', 'href="./')
            return content
    except FileNotFoundError:
        logger.error("Dashboard index HTML not found at %s", _INDEX_HTML_PATH)
        return HTMLResponse(content="<h1>Index not found</h1>", status_code=404)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global main_loop
    main_loop = asyncio.get_running_loop()
    await websocket.accept()
    connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("WebSocket connection error: %s", e)
    finally:
        if websocket in connections:
            connections.remove(websocket)


def _consume_ws_future(fut) -> None:
    """Drain background send_json exceptions on disconnected WebSockets."""
    try:
        fut.exception()
    except Exception:
        pass


def pubsub_callback(message: Message) -> None:
    """Callback triggered on new Pub/Sub message."""
    try:
        data = json.loads(message.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Dropping malformed telemetry message: {e}")
        message.ack()
        return

    try:
        # Broadcast to a snapshot of connected WebSockets to avoid mutation races
        if main_loop and main_loop.is_running():
            for ws in list(connections):
                fut = asyncio.run_coroutine_threadsafe(ws.send_json(data), main_loop)
                fut.add_done_callback(_consume_ws_future)
        message.ack()
    except Exception as e:
        print(f"Error processing message: {e}")
        message.nack()


def _is_already_exists(exc: Exception) -> bool:
    """Return True if exc indicates a Pub/Sub resource already exists."""
    try:
        from google.api_core import exceptions as gae

        if isinstance(exc, gae.AlreadyExists):
            return True
    except ImportError:
        pass
    return type(exc).__name__ == "AlreadyExists"


def _ensure_topic(publisher, topic_path: str) -> None:
    """Create Pub/Sub topic if absent; ignore AlreadyExists and re-raise other errors."""
    try:
        publisher.create_topic(name=topic_path)
    except Exception as e:
        if _is_already_exists(e):
            return
        logger.error("Failed to create Pub/Sub topic %s: %s", topic_path, e)
        raise


def _ensure_subscription(subscriber, subscription_path: str, topic_path: str) -> None:
    """Create Pub/Sub subscription if absent; ignore AlreadyExists and re-raise other errors."""
    try:
        subscriber.create_subscription(name=subscription_path, topic=topic_path)
    except Exception as e:
        if _is_already_exists(e):
            return
        logger.error("Failed to create Pub/Sub subscription %s: %s", subscription_path, e)
        raise


def start_subscriber(stop_event: threading.Event | None = None):
    """Start Pub/Sub subscriber with auto-reconnect on failure.

    Uses exponential backoff (5s-60s) with jitter, modeled on the
    dispatcher.py reconnect pattern.
    """
    project_id = (PROJECT_ID or _resolve_project_id()).strip()
    if not project_id:
        raise RuntimeError(
            "PUBSUB_PROJECT_ID, GOOGLE_CLOUD_PROJECT, or PROJECT_ID must be set when PUBSUB_EMULATOR_HOST is unset"
        )

    if stop_event is None:
        stop_event = threading.Event()

    backoff = 5  # Initial backoff seconds

    while not stop_event.is_set():
        sub_id = f"dash-subscriber-{uuid.uuid4().hex[:8]}"
        subscriber = pubsub_v1.SubscriberClient()
        topic_path = subscriber.topic_path(project_id, TOPIC_ID)
        subscription_path = subscriber.subscription_path(project_id, sub_id)

        try:
            # 1. Ensure Topic exists (close publisher transport afterwards to avoid gRPC channel leaks)
            publisher = pubsub_v1.PublisherClient()
            try:
                _ensure_topic(publisher, topic_path)
            finally:
                try:
                    publisher.transport.close()
                except Exception as e:
                    logger.warning("Failed to close Pub/Sub publisher transport: %s", e)

            # 2. Manage subscription lifecycle inside `with subscriber:` so
            # delete_subscription runs BEFORE the gRPC channel is closed on exit.
            with subscriber:
                try:
                    _ensure_subscription(subscriber, subscription_path, topic_path)

                    print(f"Listening for logs on {subscription_path}...")
                    streaming_pull_future = subscriber.subscribe(subscription_path, callback=pubsub_callback)
                    try:
                        streaming_pull_future.result()
                    finally:
                        try:
                            streaming_pull_future.cancel()
                        except Exception as e:
                            logger.warning("Failed to cancel Pub/Sub pull future: %s", e)
                finally:
                    try:
                        subscriber.delete_subscription(request={"subscription": subscription_path})
                    except Exception as e:
                        logger.warning("Failed to delete Pub/Sub subscription %s: %s", subscription_path, e)

            # Connection was alive before failing -- reset backoff
            backoff = 5

        except KeyboardInterrupt:
            stop_event.set()
        except Exception as e:
            if not stop_event.is_set():
                jitter = random.uniform(0, backoff * 0.25)
                print(f"PubSub subscriber crashed, reconnecting in {backoff + jitter:.1f}s: {e}")
                time.sleep(backoff + jitter)
                backoff = min(backoff * 2, 60)
        finally:
            try:
                subscriber.close()
            except Exception as e:
                logger.warning("Failed to close Pub/Sub subscriber: %s", e)


if __name__ == "__main__":
    import uvicorn

    # Run Pub/Sub subscriber in a separate thread
    _stop = threading.Event()
    sub_thread = threading.Thread(target=start_subscriber, args=(_stop,), daemon=True)
    sub_thread.start()

    # Run FastAPI
    port = int(os.getenv("PORT", 9111))
    uvicorn.run(app, host="0.0.0.0", port=port)
