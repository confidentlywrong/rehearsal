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

"""Vertex AI embedding helper for leadership memory.

Identical to agents/planner_with_memory/memory/embeddings.py.
Duplicated here to avoid importing through planner_with_memory.__init__
which eagerly bootstraps the full marathon agent (requires GOOGLE_CLOUD_PROJECT).
"""

from __future__ import annotations

import os
from functools import lru_cache

from google import genai


@lru_cache(maxsize=1)
def _get_genai_client() -> genai.Client:
    """Process-global google-genai client. Vertex AI when a project is set,
    otherwise API-key auth (local dev with GEMINI_API_KEY / GOOGLE_API_KEY)."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() not in ("0", "false", "no")
    if project and use_vertex:
        return genai.Client(
            vertexai=True,
            project=project,
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    return genai.Client(vertexai=False, api_key=api_key)


# NOTE: This function is currently unused. All embedding is done
# server-side by the Go gateway's VertexEmbedder. Kept for potential
# future use in offline/batch scenarios.
async def compute_embedding(text: str, *, dimension: int = 3072) -> list[float]:
    """Compute an embedding for ``text`` via Vertex AI / Gemini.

    Default dimension 3072 matches the ``VECTOR(3072)`` column in
    ``leadership_planner_with_memory`` schemas. Errors propagate; callers
    decide retry/fallback policy.
    """
    from agents.utils.local_ai import (
        compute_local_embedding_async,
        is_local_gemma_active,
    )

    if is_local_gemma_active():
        local_vec = await compute_local_embedding_async(text, dimension=dimension)
        if local_vec is not None:
            return local_vec
        raise RuntimeError(
            "Local Google embeddinggemma failed to compute vector embedding via Ollama"
        )

    client = _get_genai_client()
    model = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001")
    response = await client.aio.models.embed_content(
        model=model,
        contents=text,
        config={"output_dimensionality": dimension},
    )
    if not response.embeddings:
        raise RuntimeError(f"embed_content returned no embeddings for model={model}")
    values = response.embeddings[0].values
    if values is None:
        raise RuntimeError(f"embed_content returned embedding with no values for model={model}")
    return list(values)
