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

"""Canonical Gemini model constants and environment-variable resolution."""

from __future__ import annotations

import os

DEFAULT_PLANNER_MODEL: str = "gemini-3-flash-preview"
DEFAULT_EVALUATOR_MODEL: str = "gemini-3.1-pro-preview"
DEFAULT_FLASH_LITE_MODEL: str = "gemini-3.5-flash-lite"
DEFAULT_EMBEDDING_MODEL: str = "gemini-embedding-001"


def get_model(
    env_var: str,
    default: str,
    *,
    fallback_env_vars: tuple[str, ...] = (),
) -> str:
    """Resolve a model name from environment variables with a canonical default.

    Checks ``env_var`` first, then each variable in ``fallback_env_vars`` in
    order, skipping unset or whitespace-only values.  Falls back to ``default``.
    """
    for key in (env_var, *fallback_env_vars):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return default
