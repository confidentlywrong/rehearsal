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

"""Leadership Runner Autopilot — deterministic runner using callback interception.

Uses leadership physics (process_leadership_tick) instead of marathon physics.
Zero LLM cost per tick.  Opt-in via runner_type="leadership_runner_autopilot".
"""

import os

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

from agents.leadership_runner_autopilot import agent  # noqa: E402
