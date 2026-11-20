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

"""Vertex AI embedding helper for OSS deployments without AlloyDB ai.embedding().

Cloud SQL Postgres lacks AlloyDB's ai.embedding() / ai.initialize_embeddings
extension, so OSS deployments must compute embeddings client-side via
google-genai. Selected by EMBEDDING_BACKEND=vertex_ai on the calling code.
"""

from __future__ import annotations

import math
import os

from google import genai

from agents.utils.models import DEFAULT_EMBEDDING_MODEL, get_model
from agents.utils.retry import resilient_http_options

_client: genai.Client | None = None
_client_key: tuple[bool, str, str, str] | None = None


def _get_genai_client() -> genai.Client:
    """Process-global google-genai client keyed by active auth/env config.

    Vertex AI when a project is set (unless GOOGLE_GENAI_USE_VERTEXAI=0),
    otherwise API-key auth (local dev with GEMINI_API_KEY / GOOGLE_API_KEY).
    """
    global _client, _client_key
    project = (
        (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
        or (os.environ.get("PROJECT_ID") or "").strip()
    )
    location = (os.environ.get("GOOGLE_CLOUD_LOCATION") or "").strip() or "global"
    use_vertex = (os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") or "").strip().lower() not in ("0", "false", "no")
    api_key = (
        (os.environ.get("GEMINI_API_KEY") or "").strip()
        or (os.environ.get("GOOGLE_API_KEY") or "").strip()
    )
    vertex_active = bool(project and use_vertex)
    key = (
        vertex_active,
        project if vertex_active else "",
        location if vertex_active else "",
        "" if vertex_active else api_key,
    )
    if _client is None or _client_key != key:
        http_opts = resilient_http_options()
        if vertex_active:
            _client = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                http_options=http_opts,
            )
        else:
            _client = genai.Client(
                vertexai=False,
                api_key=api_key,
                http_options=http_opts,
            )
        _client_key = key
    return _client


def _clear_genai_client_cache() -> None:
    global _client, _client_key
    _client = None
    _client_key = None


_get_genai_client.cache_clear = _clear_genai_client_cache  # type: ignore[attr-defined]


def _validate_embedding_vector(values: list[float], *, source: str) -> list[float]:
    if not values:
        raise RuntimeError(f"{source} returned an empty embedding vector")
    out: list[float] = []
    for v in values:
        if isinstance(v, bool):
            raise ValueError(f"{source} returned non-numeric bool in embedding vector")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"{source} returned non-finite value {v!r} in embedding vector")
        out.append(f)
    return out


async def compute_embedding(text: str, *, dimension: int = 3072) -> list[float]:
    """Compute an embedding for ``text`` via Local EmbeddingGemma or Vertex AI / Gemini.

    Default dimension 3072 matches the ``VECTOR(3072)`` column in
    ``planner_with_memory`` schemas. Errors propagate; callers decide
    retry/fallback policy.
    """
    if isinstance(dimension, bool) or dimension <= 0:
        raise ValueError(f"Embedding dimension must be a positive integer, got {dimension!r}")

    from agents.utils.local_ai import (
        compute_local_embedding_async,
        is_local_gemma_active,
    )

    if is_local_gemma_active():
        local_vec = await compute_local_embedding_async(text, dimension=dimension)
        if local_vec is not None:
            return _validate_embedding_vector(local_vec, source="local_gemma")
        raise RuntimeError(
            "Local Google embeddinggemma failed to compute vector embedding via Ollama"
        )

    client = _get_genai_client()
    model = get_model("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
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
    return _validate_embedding_vector(list(values), source=f"embed_content({model})")
