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

"""Configuration for the leadership scenario evaluator.

Mirrors agents/planner_with_eval/evaluator/config.py with leadership-
specific criteria weights and thresholds.
"""

import os

# Model configuration
MODEL = os.getenv("EVALUATOR_MODEL", "gemini-3.1-pro-preview")

# Criterion weights must sum to 1.0 (equal weighting across 6 criteria)
CRITERION_WEIGHTS = {
    "stakeholder_alignment": 1.0 / 6,
    "decision_quality": 1.0 / 6,
    "resource_stewardship": 1.0 / 6,
    "crisis_readiness": 1.0 / 6,
    "strategic_coherence": 1.0 / 6,
    "ethical_alignment": 1.0 / 6,
}

# Evaluation criteria (6 LLM metrics — no deterministic metric equivalent)
EVALUATION_CRITERIA = [
    "stakeholder_alignment",
    "decision_quality",
    "resource_stewardship",
    "crisis_readiness",
    "strategic_coherence",
    "ethical_alignment",
]

# Pass threshold for overall evaluation score (0-100 scale).
PASS_THRESHOLD = 75

# Severity mapping for evaluation findings (0-100 scale)
SEVERITY_THRESHOLDS = {
    "high": 40,
    "medium": 60,
    "low": 80,
}
