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

"""Reactive AI Echo (Tier 2) — selective LLM activation.

Extends the base AI Echo agent with a ``before_model_callback`` that
decides per-tick whether to use the LLM or respond deterministically.
LLM is activated only when the participant's decision touches this
Echo's domain principles or when a contradiction alert fires.

Direct analogue of the hybrid between ``runner`` (LLM) and
``runner_autopilot`` (deterministic).
"""

from agents.utils.env import configure_project_env

configure_project_env("ai_echo_reactive")

import logging  # noqa: E402

from google.adk.apps import App  # noqa: E402
from google.adk.agents.context_cache_config import ContextCacheConfig  # noqa: E402

from agents.ai_echo.agent import (  # noqa: E402
    ECHO_SUPPRESSED_EVENTS,
    _is_gemini,
    get_agent as get_base_agent,
)
from agents.ai_echo_reactive.callback import reactive_echo_callback  # noqa: E402
from agents.utils import config  # noqa: E402
from agents.utils.communication_plugin import SimulationCommunicationPlugin  # noqa: E402
from agents.utils.deployment import create_a2a_deployment  # noqa: E402
from agents.utils.factory import create_simulation_runner  # noqa: E402
from agents.utils.plugins import RedisDashLogPlugin  # noqa: E402

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_agent():
    """Create a Tier 2 (Reactive) AI Echo agent."""
    agent = get_base_agent()
    agent.name = "ai_echo_reactive"
    agent.before_model_callback = reactive_echo_callback
    # Keep before_agent_callback for initialization on first tick
    # Keep static_instruction for when LLM IS invoked
    return agent


root_agent = get_agent()

_echo_app: "App | None" = None


def _get_echo():
    global _echo_app
    runner, app, _ = create_simulation_runner(
        name="ai_echo_reactive",
        root_agent=root_agent,
        extra_plugins=[SimulationCommunicationPlugin()],
        dash_log_plugin=RedisDashLogPlugin(
            fire_and_forget=True,
            suppressed_events=ECHO_SUPPRESSED_EVENTS,
        ),
        suppress_gateway_emission=True,
    )
    _echo_app = app
    return runner


# --- A2A Deployment ---
_cache_config = ContextCacheConfig(cache_intervals=10, ttl_seconds=1800) if _is_gemini else None
app = App(
    name="ai_echo_reactive",
    root_agent=root_agent,
    context_cache_config=_cache_config,
)

echo_a2a_agent, agent_card = create_a2a_deployment(
    name="ai_echo_reactive",
    app_or_agent=app,
    agent_getter=get_agent,
)

if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent

    config.load_env()
    port = int(config.optional("PORT", config.optional("AI_ECHO_REACTIVE_PORT", "8216")))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger.info("Starting AI Echo Reactive (Tier 2) agent server on port %d", port)

    _echo = _get_echo()

    api_app = create_agent_app(
        name="ai_echo_reactive",
        agents_dir="agents",
        adk_app=_echo_app,
        agent_card=agent_card,
        simulation_runner=_echo,
    )
    serve_agent(api_app, port=port)
