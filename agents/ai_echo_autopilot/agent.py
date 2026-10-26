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

"""Deterministic AI Echo Autopilot — extends the base AI Echo agent.

Overrides the LLM with a before_model_callback that handles all decisions
deterministically. The model is never called. Zero LLM cost per tick.

Direct analogue of ``agents/runner_autopilot/agent.py``.
"""

from agents.utils.env import configure_project_env

configure_project_env("ai_echo_autopilot")

import logging  # noqa: E402

from google.adk.apps import App  # noqa: E402
from google.genai import types  # noqa: E402

from agents.ai_echo.agent import ECHO_SUPPRESSED_EVENTS, get_agent as get_base_agent  # noqa: E402
from agents.ai_echo_autopilot.autopilot import autopilot_echo_callback  # noqa: E402
from agents.utils import config  # noqa: E402
from agents.utils.communication_plugin import SimulationCommunicationPlugin  # noqa: E402
from agents.utils.deployment import create_a2a_deployment  # noqa: E402
from agents.utils.factory import create_simulation_runner  # noqa: E402
from agents.utils.plugins import RedisDashLogPlugin  # noqa: E402

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_agent():
    """Create an AI Echo Autopilot agent by extending the base Echo."""
    agent = get_base_agent()
    agent.name = "ai_echo_autopilot"
    agent.instruction = "Deterministic AI Echo autopilot."
    agent.before_model_callback = autopilot_echo_callback
    agent.before_agent_callback = None  # Autopilot handles init in callback
    agent.generate_content_config = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=1,
    )
    return agent


root_agent = get_agent()

_echo_app: "App | None" = None


def _get_echo():
    global _echo_app
    runner, app, _ = create_simulation_runner(
        name="ai_echo_autopilot",
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
app = App(
    name="ai_echo_autopilot",
    root_agent=root_agent,
)

echo_a2a_agent, agent_card = create_a2a_deployment(
    name="ai_echo_autopilot",
    app_or_agent=app,
    agent_getter=get_agent,
)

if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent

    config.load_env()
    port = int(config.optional("PORT", config.optional("AI_ECHO_AUTOPILOT_PORT", "8217")))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger.info("Starting AI Echo Autopilot (Tier 3) agent server on port %d", port)

    _echo = _get_echo()

    api_app = create_agent_app(
        name="ai_echo_autopilot",
        agents_dir="agents",
        adk_app=_echo_app,
        agent_card=agent_card,
        simulation_runner=_echo,
    )
    serve_agent(api_app, port=port)
