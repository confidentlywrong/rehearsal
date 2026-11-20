#!/bin/bash
# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: adapted simulation runtime, agents, and infrastructure for executive leadership rehearsal.
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

# Robust wrapper for ADK agents under Honcho
# Uses the PORT env var provided by the parent
if [[ -z "${PORT:-}" ]]; then
    echo "ERROR: PORT environment variable is required." >&2
    exit 1
fi
echo "Starting Agent with PORT=$PORT"
if command -v uv >/dev/null 2>&1; then
    exec uv run python -m google.adk.cli api_server "$@" --port "$PORT"
fi
exec python3 -m google.adk.cli api_server "$@" --port "$PORT"
