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

"""Tenant-namespacing helper for Redis keys (Phase 2).

This is the Python mirror of ``internal/account/keys.go``. Both must
produce IDENTICAL key strings for the same inputs — the gateway
(Go) and the agents (Python) read and write the same Redis keys.

PHASE 2 STATUS: helper landed; not yet integrated into any
production code path. Integration into
``agents/utils/simulation_registry.py``, ``agents/utils/dispatcher.py``,
and the spawn-queue consumers is tracked as Phase 2.5 in
``concepts/multi_tenant_saas_plan.md``.

USAGE RULES (mirrored from the Go side):

* SAAS_MODE=off (default): ``key(ctx, ...)`` returns the legacy
  un-prefixed key. Behavior is byte-identical to the single-tenant
  app that exists today.
* SAAS_MODE=on, tenant present: prepends ``{ACCOUNT_KEY_PREFIX}:{user_id}``.
* SAAS_MODE=on, tenant missing: falls through to the legacy key so
  the Phase 2.5 compatibility shim can still find pre-migration data.

This module reads env vars on every call so test setup can mutate
``SAAS_MODE`` and ``ACCOUNT_KEY_PREFIX`` freely without re-importing.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional


def _saas_mode_enabled() -> bool:
    """Mirror of ``tenant.SAASModeEnabled`` in Go.

    Accepts ``on``/``true``/``1``/``yes`` (case-insensitive).
    """
    return os.environ.get("SAAS_MODE", "off").lower() in ("on", "true", "1", "yes")


def _key_prefix_for_user(user_id: str) -> str:
    if not user_id:
        return ""
    root = os.environ.get("ACCOUNT_KEY_PREFIX", "") or "u"
    return f"{root}:{user_id}"


def key_prefix(tenant_state: Optional[Mapping[str, object]]) -> str:
    """Return the namespace prefix for the given tenant state.

    ``tenant_state`` is the dict the gateway adds to session state
    under the key ``tenant`` (Phase 1 wires this up; Phase 2.5 reads
    it). It must look like ``{"user_id": ..., ...}``.
    None or a missing ``user_id`` means "no tenant" — caller decides
    whether to fall through to the legacy key or reject.

    Returns the empty string in SAAS_MODE=off or when no user is set.
    """
    if not _saas_mode_enabled():
        return ""
    if not tenant_state:
        return ""
    user_id = tenant_state.get("user_id") or ""
    return _key_prefix_for_user(str(user_id))


def key(tenant_state: Optional[Mapping[str, object]], *segments: str) -> str:
    """Compose a Redis key, prepending the per-tenant namespace if any.

    Examples (with ACCOUNT_KEY_PREFIX=u):

        # SAAS_MODE=off
        key(None, "session", "abc")        -> "session:abc"

        # SAAS_MODE=on, user=local-dev
        key({"user_id": "local-dev"}, "session", "abc")
                                           -> "u:local-dev:session:abc"

        # SAAS_MODE=on, no tenant (compat fallback)
        key(None, "session", "abc")        -> "session:abc"

    Empty segments are dropped so callers don't have to special-case
    a missing trailing ID.
    """
    return key_for_user(
        _user_from_state(tenant_state),
        *segments,
    )


def key_for_user(user_id: str, *segments: str) -> str:
    """User-explicit form of :func:`key`. Pass ``""`` to suppress prefix."""
    parts: list[str] = []
    if _saas_mode_enabled():
        prefix = _key_prefix_for_user(user_id)
        if prefix:
            parts.append(prefix)
    parts.extend(s for s in segments if s)
    return ":".join(parts)


def _user_from_state(tenant_state: Optional[Mapping[str, object]]) -> str:
    if not tenant_state:
        return ""
    return str(tenant_state.get("user_id") or "")
