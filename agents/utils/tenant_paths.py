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

"""Tenant-namespacing helper for on-disk simulation-run storage (Phase 3).

Sibling of :mod:`agents.utils.tenant_keys`. That module namespaces
Redis keys; this one namespaces filesystem paths under
``data/simulation_runs/``.

USAGE RULES (mirror the Redis-side guarantees):

* The storage layout relies entirely on the caller's fallback (e.g. the
  ``participant_id`` which acts as the ``user_id`` tenant boundary).
* The layout is strictly per-tenant ``data/simulation_runs/{tenant_id}/...``.

This module reads env vars on every call so test setup can mutate
``RACE_RUNS_DIR`` without re-importing.
"""

from __future__ import annotations

import os
from pathlib import Path

# Default base directory: ``<repo-root>/data/simulation_runs``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BASE_DIR = _REPO_ROOT / "data" / "simulation_runs"


def _saas_mode_enabled() -> bool:
    """Mirror of ``tenant.saasModeEnabled`` in Go.

    Accepts ``on``/``true``/``1``/``yes`` (case-insensitive — matches
    Go and :mod:`agents.utils.tenant_keys`).
    """
    return os.environ.get("SAAS_MODE", "off").lower() in ("on", "true", "1", "yes")


def runs_base_dir() -> Path:
    """Return the base directory holding every tenant's simulation runs.

    Honours ``RACE_RUNS_DIR`` for test/sandbox overrides; otherwise
    points at ``data/simulation_runs/`` next to the repo root.
    """
    override = os.environ.get("RACE_RUNS_DIR", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_BASE_DIR


def resolve_tenant_id(fallback: str | None = None) -> str:
    """Return the tenant id that should own a write.

    In a user_id multi-tenant system, the fallback (participant_id) IS
    the tenant_id. We no longer rely on DISPATCHER_USER_ID here.

    ``fallback`` is returned verbatim. An empty/None fallback also
    returns the empty string — callers must handle that themselves
    rather than have this helper synthesize a tenant.
    """
    return fallback or ""


def tenant_runs_dir(tenant_id: str) -> Path:
    """Return ``data/simulation_runs/{tenant_id}/``.

    Does NOT create the directory — callers do that with the same
    ``mkdir(parents=True, exist_ok=True)`` pattern used today, so
    failure modes stay identical.
    """
    if not tenant_id:
        raise ValueError("tenant_paths.tenant_runs_dir: tenant_id must be non-empty")
    return runs_base_dir() / tenant_id


def run_path(tenant_id: str, sim_id: str) -> Path:
    """Return ``data/simulation_runs/{tenant_id}/{sim_id}.json``."""
    if not sim_id:
        raise ValueError("tenant_paths.run_path: sim_id must be non-empty")
    return tenant_runs_dir(tenant_id) / f"{sim_id}.json"


def summary_path(tenant_id: str) -> Path:
    """Return ``data/simulation_runs/{tenant_id}/runs.ndjson``.

    This is the append-only longitudinal summary file. One per tenant.
    """
    return tenant_runs_dir(tenant_id) / "runs.ndjson"
