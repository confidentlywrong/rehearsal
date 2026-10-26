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

"""Session-scoped user auth token store.

Purpose
-------
When the SPA calls the gateway (`POST /api/v1/sessions` or
`POST /api/v1/spawn`) to start an agent run, the gateway already knows
*which user* is spawning the run. Today that identity was flattened to
a string ``user_id`` in the spawn payload and the agent then called
back into the gateway using a *shared* ``MEMORY_GATEWAY_TOKEN`` env
var — meaning every memory-retrieval call in the cloud came from one
service identity, regardless of which end user actually triggered it.

That is a **correctness and data-safety bug** on a multi-user
deployment: user B's scenario would fetch (or write to) whichever
user the shared token belongs to.

This module fixes that by giving the dispatcher a place to stash the
end user's raw bearer token *at spawn time*, keyed by the ADK
session id, and letting the retrieval tool read it back at run time
via ``tool_context.session.id``.

Why a process-local dict (not ADK session state)
------------------------------------------------
* **Security.** ADK session state is persisted (Vertex AI session
  service, Redis L2). Writing a bearer token there would leak it
  into DB backups, memory-service exports, and any future audit
  log that dumps state. A process-local dict never touches disk.
* **Simplicity.** No schema migration, no ADK-runner plumbing, no
  state-delta events.
* **Correctness.** Each dispatcher process owns its own set of
  active sessions (see ``self.active_sessions`` in dispatcher.py).
  A given session is always handled by the same dispatcher instance
  for its lifetime, so a per-process cache is the right scope.

Contract
--------
* :func:`set_session_token` — dispatcher calls this on ``spawn_agent``
  when the payload carries ``user_auth_token``.
* :func:`get_session_token` — the retrieval tool calls this with
  ``tool_context.session.id``.
* :func:`clear_session_token` — dispatcher calls this from
  ``remove_session`` so the map stays bounded across long-running
  processes.

Empty / whitespace tokens are treated as absent — writing one is a
no-op, and callers see ``None``. That matches the local-dev flow
where the gateway's ``LocalResolver`` accepts loopback callers with
no auth header at all.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

# Hard upper bound on the number of tokens we hold at once. This is a
# guardrail against a bug elsewhere that drops sessions without
# calling ``clear_session_token``. At ~1 KB per JWT, 10 000 sessions
# is under 10 MB — cheap insurance. When exceeded, the oldest entries
# are evicted (LRU-ish; we don't touch on read to keep get() lock-free).
_MAX_ENTRIES = 10_000

_lock = threading.Lock()
_tokens: OrderedDict[str, str] = OrderedDict()


def set_session_token(session_id: str, token: str | None) -> None:
    """Bind ``token`` to ``session_id`` for the rest of the session.

    Overwrites any prior value. Ignoring calls with an empty/None
    token means the dispatcher can pipe ``payload.get("user_auth_token")``
    straight through — a spawn from a background/service context
    (no user bearer) simply leaves the mapping unset and the
    retrieval tool falls back to the shared env-var path.
    """
    if not session_id:
        return
    tok = (token or "").strip()
    if not tok:
        return
    with _lock:
        _tokens[session_id] = tok
        _tokens.move_to_end(session_id)
        while len(_tokens) > _MAX_ENTRIES:
            # Evict oldest — protects against a leak elsewhere.
            evicted_sid, _ = _tokens.popitem(last=False)
            logger.warning(
                "user_auth_context: evicted token for session=%s "
                "(cap=%d hit — check dispatcher.remove_session cleanup)",
                evicted_sid,
                _MAX_ENTRIES,
            )


def get_session_token(session_id: str) -> str | None:
    """Return the bearer token for ``session_id`` or ``None``.

    Never raises. Callers should treat ``None`` as "no per-user
    token available — fall back to the env-var path or send no
    auth header at all".
    """
    if not session_id:
        return None
    # dict.get is atomic under the GIL — no lock needed on the read
    # path, which is called from every retrieval tool invocation.
    return _tokens.get(session_id)


def clear_session_token(session_id: str) -> None:
    """Forget the token for ``session_id``. Idempotent."""
    if not session_id:
        return
    with _lock:
        _tokens.pop(session_id, None)


def _reset_for_tests() -> None:
    """Drop every mapping. Test-only."""
    with _lock:
        _tokens.clear()
