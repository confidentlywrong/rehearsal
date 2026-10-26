# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
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

"""Runner type constants for simulation agent selection.

The simulator defaults to leadership_runner (LLM-powered, leadership physics).
The planner can request a different type via simulation_config.
leadership_runner_autopilot is available as a deterministic opt-in for
testing, benchmarking, and scaling.
"""

import os

RUNNER_AUTOPILOT = "runner_autopilot"
RUNNER_CLOUDRUN = "runner_cloudrun"
RUNNER_GKE = "runner_gke"
RUNNER = "runner"
LEADERSHIP_RUNNER = "leadership_runner"
LEADERSHIP_RUNNER_AUTOPILOT = "leadership_runner_autopilot"

DEFAULT_RUNNER_TYPE = LEADERSHIP_RUNNER_AUTOPILOT

ALL_RUNNER_TYPES = (
    RUNNER_AUTOPILOT,
    RUNNER_CLOUDRUN,
    RUNNER_GKE,
    RUNNER,
    LEADERSHIP_RUNNER,
    LEADERSHIP_RUNNER_AUTOPILOT,
)

LLM_RUNNER_TYPES = (RUNNER, RUNNER_CLOUDRUN, RUNNER_GKE, LEADERSHIP_RUNNER)


def cap_for_runner_type(runner_type: str) -> int:
    """Return the configured runner-count cap for the given runner_type.

    The cap is read from environment variables at call time so that deploy-time
    overrides (.env.{env} files) take effect without code changes:

    - ``runner_autopilot`` / ``leadership_runner_autopilot`` → ``MAX_RUNNERS_AUTOPILOT`` (default ``1000``)
    - any LLM runner type → ``MAX_RUNNERS_LLM`` (default ``10``)

    Defaults are local-friendly. GCP deployments override via ``.env.dev``
    (autopilot=1000, LLM=100). Unknown runner types fall through to the LLM
    cap as a defensive default — better undersized than uncapped.
    """
    if runner_type in (RUNNER_AUTOPILOT, LEADERSHIP_RUNNER_AUTOPILOT):
        return int(os.environ.get("MAX_RUNNERS_AUTOPILOT", "1000"))
    return int(os.environ.get("MAX_RUNNERS_LLM", "10"))
