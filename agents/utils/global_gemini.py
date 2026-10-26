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

"""Gemini model subclass that routes to the global endpoint.

Agent Engine auto-configures vertexai with location='us-central1', which
prevents access to Gemini 3 preview models that require the global endpoint.

This module provides GlobalGemini — a drop-in replacement for Gemini that
explicitly creates a genai Client with location='global', bypassing AE's
platform-level vertexai.init() override.

Usage in LlmAgent definitions:
    from agents.utils.global_gemini import GlobalGemini

    agent = LlmAgent(
        model=GlobalGemini(model="gemini-3-flash-preview"),
        ...
    )

Ref: https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/overview
"""

import os
from functools import cached_property
from typing import AsyncGenerator, TYPE_CHECKING

from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

if TYPE_CHECKING:
    from google.genai import Client


class GlobalGemini(Gemini):
    """Gemini model with explicit location control for Vertex AI.

    On Agent Engine, vertexai is auto-initialized with the AE region
    (us-central1). The standard Gemini class creates Client() which
    inherits this location. GlobalGemini overrides api_client to create
    a Client with an explicit location, defaulting to 'global' for
    Gemini 3 preview models.

    For per-agent location control::

        # Global endpoint (default, required for Gemini 3 previews)
        model = GlobalGemini(model="gemini-3-flash-preview")

        # Regional endpoint
        model = GlobalGemini(model="gemini-2.0-flash", location="us-central1")
    """

    location: str = "global"
    """Vertex AI API location. Defaults to 'global' for Gemini 3 preview models.
    Set to a region (e.g. 'us-central1') for GA models."""

    @cached_property
    def api_client(self) -> "Client":
        """Create a genai Client with the configured location (or API key when no GCP project is active)."""
        from google.genai import Client

        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID") or ""
        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() not in ("0", "false", "no")
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        http_opts = types.HttpOptions(
            headers=self._tracking_headers(),
            retry_options=self.retry_options,
            base_url=self.base_url,
        )
        if not project and api_key or not use_vertex and api_key:
            return Client(
                vertexai=False,
                api_key=api_key,
                http_options=http_opts,
            )
        return Client(
            vertexai=True,
            project=project,
            location=self.location,
            http_options=http_opts,
        )

    async def generate_content_async(
        self,
        llm_request: LlmRequest,
        stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        """Route to Local Google Gemma via Ollama when `local_gemma` is active."""
        from agents.utils.local_ai import (
            build_non_gemini_litellm,
            get_local_litellm,
            is_local_gemma_active,
        )

        if is_local_gemma_active():
            local_model = get_local_litellm()
            async for resp in local_model.generate_content_async(
                llm_request,
                stream=stream,
            ):
                yield resp
            return

        model_str = (self.model or "").strip()
        if model_str and not model_str.lower().startswith("gemini"):
            non_gemini = build_non_gemini_litellm(model_str)
            async for resp in non_gemini.generate_content_async(
                llm_request,
                stream=stream,
            ):
                yield resp
            return

        async for resp in super().generate_content_async(
            llm_request,
            stream=stream,
        ):
            yield resp

