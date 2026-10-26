#!/usr/bin/env bash
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

# scripts/core/start_db.sh
# Starts either the local Postgres container or the AlloyDB Auth Proxy
# based on the USE_ALLOYDB environment variable.
#
# - USE_ALLOYDB=false (default): starts a local Postgres container via docker-compose.
# - USE_ALLOYDB=true:            starts the AlloyDB Auth Proxy (auto-installs if missing).
#
# No passwords or manual steps required in either mode.
#
# Cloud Run: this script exits immediately — Cloud Run connects to AlloyDB
# directly via VPC connector; no proxy or local container is needed.

set -euo pipefail

# Guard: do nothing on Cloud Run (K_SERVICE is set by the platform).
if [ -n "${K_SERVICE:-}" ]; then
    echo "==> Cloud Run detected (K_SERVICE=${K_SERVICE}): skipping local DB setup <=="
    exit 0
fi

# Resolve the project root (two levels up from this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [ "${USE_ALLOYDB:-false}" = "true" ]; then
    echo "==> USE_ALLOYDB=true: Starting AlloyDB Auth Proxy <=="

    PROXY_BIN="${ROOT_DIR}/alloydb-auth-proxy"

    # Auto-install the proxy binary if it's not present.
    # install_alloydb_proxy.sh is a silent curl download — no passwords needed.
    if [ ! -f "${PROXY_BIN}" ]; then
        echo "    AlloyDB Auth Proxy not found — installing automatically..."
        bash "${SCRIPT_DIR}/install_alloydb_proxy.sh"
    fi

    # Read the AlloyDB instance URI from the environment.
    # Format: projects/<project>/locations/<region>/clusters/<cluster>/instances/<instance>
    ALLOYDB_INSTANCE_URI="${ALLOYDB_INSTANCE_URI:-}"
    if [ -z "${ALLOYDB_INSTANCE_URI}" ]; then
        echo "❌ ERROR: ALLOYDB_INSTANCE_URI is required when USE_ALLOYDB=true."
        echo "   Set it in .env to your AlloyDB instance connection name, e.g.:"
        echo "   ALLOYDB_INSTANCE_URI=projects/my-project/locations/us-central1/clusters/my-cluster/instances/my-instance"
        exit 1
    fi

    # Use exec so that honcho correctly handles signals for the child process
    exec "${PROXY_BIN}" \
        "${ALLOYDB_INSTANCE_URI}" \
        --public-ip \
        --port="${ALLOYDB_PORT:-8104}"
else
    echo "==> USE_ALLOYDB=false (default): Starting local PostgreSQL container <=="

    # Ensure DOCKER_HOST is set for Colima users.
    # standalone docker-compose does not inherit the docker CLI context,
    # so we point it at the Colima socket explicitly if not already set.
    if [ -z "${DOCKER_HOST:-}" ] && [ -S "${HOME}/.colima/default/docker.sock" ]; then
        export DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock"
        echo "    (auto-configured DOCKER_HOST for Colima)"
    fi

    # preflight_infra() in sim.py already starts all containers
    # (docker compose up -d). We just follow the logs to keep the
    # Honcho process alive and forward signals for a clean shutdown.
    exec docker compose logs -f postgres
fi

