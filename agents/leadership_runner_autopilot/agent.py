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

"""Deterministic leadership runner autopilot — extends the leadership runner agent.

Overrides the LLM with a before_model_callback that handles all decisions
deterministically using leadership physics (process_leadership_tick).
The model is never called.  Zero LLM cost per tick.

Direct analogue of ``agents/runner_autopilot/agent.py`` but for leadership physics.
"""

from agents.utils.env import configure_project_env

configure_project_env("leadership_runner_autopilot")

import logging  # noqa: E402

from google.adk.apps import App  # noqa: E402
from google.genai import types  # noqa: E402

from agents.leadership_runner.agent import RUNNER_SUPPRESSED_EVENTS, get_agent as get_base_agent  # noqa: E402
from agents.leadership_runner_autopilot.autopilot import autopilot_leadership_callback  # noqa: E402
from agents.utils import config  # noqa: E402
from agents.utils.communication_plugin import SimulationCommunicationPlugin  # noqa: E402
from agents.utils.deployment import create_a2a_deployment  # noqa: E402
from agents.utils.factory import create_simulation_runner  # noqa: E402
from agents.utils.plugins import RedisDashLogPlugin  # noqa: E402

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_agent():
    """Create a leadership_runner_autopilot agent by extending the leadership runner."""
    agent = get_base_agent()
    agent.name = "leadership_runner_autopilot"
    agent.instruction = "Deterministic leadership autopilot runner."
    agent.before_model_callback = autopilot_leadership_callback
    agent.before_agent_callback = None  # Autopilot handles init in callback
    agent.generate_content_config = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=1,
    )
    return agent


root_agent = get_agent()

_runner_app: "App | None" = None


def _get_runner():
    global _runner_app
    runner, app, _ = create_simulation_runner(
        name="leadership_runner_autopilot",
        root_agent=root_agent,
        extra_plugins=[SimulationCommunicationPlugin()],
        dash_log_plugin=RedisDashLogPlugin(
            fire_and_forget=True,
            suppressed_events=RUNNER_SUPPRESSED_EVENTS,
        ),
        suppress_gateway_emission=True,
    )
    _runner_app = app
    return runner


# --- A2A Deployment ---
# No context cache -- model never runs for autopilot.
app = App(
    name="leadership_runner_autopilot",
    root_agent=root_agent,
)

runner_a2a_agent, agent_card = create_a2a_deployment(
    name="leadership_runner_autopilot",
    app_or_agent=app,
    agent_getter=get_agent,
)

if __name__ == "__main__":
    from agents.utils.serve import create_agent_app, serve_agent  # noqa: E402

    config.load_env()
    port = int(config.optional("PORT", config.optional("LEADERSHIP_RUNNER_AUTOPILOT_PORT", "9120")))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger.info("Starting leadership_runner_autopilot agent server on port %d", port)

    _runner = _get_runner()

    api_app = create_agent_app(
        name="leadership_runner_autopilot",
        agents_dir="agents",
        adk_app=_runner_app,
        agent_card=agent_card,
        simulation_runner=_runner,
    )
    serve_agent(api_app, port=port)
