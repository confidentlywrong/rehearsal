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

"""Tenant-safe LoopAgent.

The upstream ``google.adk.agents.LoopAgent`` reads ``self.max_iterations``
on every loop turn, which means mutating that attribute from a
``before_agent_callback`` (the historical pattern for setting it from
the current session's ``max_ticks``) races between concurrent
invocations: two simulations sharing the same module-level singleton
will overwrite each other's iteration cap, so a long race can be
truncated to a short one's limit.

``TenantSafeLoopAgent`` keeps the singleton's ``self.max_iterations``
unchanged and instead reads a per-invocation override from
``ctx.session.state`` (which IS invocation-local in ADK). The
state key is namespaced by agent name so multiple loop agents
in the same pipeline (e.g. ``race_engine``) cannot collide:

* State key: ``f"_loop_max_iterations_{self.name}"``.
* Fallback: ``self.max_iterations`` when the state key is absent.

The rest of the loop implementation is a faithful copy of upstream
LoopAgent._run_async_impl (Apache-2.0, Google LLC) so behavior — including
resumability, escalate handling, and pause/invocation semantics — is
otherwise unchanged.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.loop_agent import LoopAgent, LoopAgentState
from google.adk.events.event import Event
from google.adk.utils.context_utils import Aclosing

logger = logging.getLogger(__name__)


class TenantSafeLoopAgent(LoopAgent):
    """LoopAgent variant that reads max_iterations from session state per invocation."""

    def _effective_max_iterations(self, ctx: InvocationContext) -> int | None:
        """Return the per-invocation iteration cap, falling back to the static field.

        Reads ``ctx.session.state[f"_loop_max_iterations_{self.name}"]`` and
        coerces to ``int`` when present and non-zero. Zero or missing values
        fall through to ``self.max_iterations`` so callers that explicitly
        want "unlimited" can clear the override.
        """
        key = f"_loop_max_iterations_{self.name}"
        try:
            session_state = ctx.session.state  # type: ignore[union-attr]
        except AttributeError:
            return self.max_iterations
        if session_state is None:
            return self.max_iterations
        raw = session_state.get(key) if hasattr(session_state, "get") else None
        if raw in (None, 0, "0", ""):
            return self.max_iterations
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("TenantSafeLoopAgent[%s]: ignoring non-int %s=%r", self.name, key, raw)
            return self.max_iterations

    async def _run_async_impl(  # type: ignore[override]
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if not self.sub_agents:
            return

        agent_state = self._load_agent_state(ctx, LoopAgentState)
        is_resuming_at_current_agent = agent_state is not None
        times_looped, start_index = self._get_start_state(agent_state)

        max_iter = self._effective_max_iterations(ctx)

        should_exit = False
        pause_invocation = False
        while (max_iter is None or times_looped < max_iter) and not (should_exit or pause_invocation):
            for i in range(start_index, len(self.sub_agents)):
                sub_agent = self.sub_agents[i]

                if ctx.is_resumable and not is_resuming_at_current_agent:
                    agent_state = LoopAgentState(
                        current_sub_agent=sub_agent.name,
                        times_looped=times_looped,
                    )
                    ctx.set_agent_state(self.name, agent_state=agent_state)
                    yield self._create_agent_state_event(ctx)

                is_resuming_at_current_agent = False

                async with Aclosing(sub_agent.run_async(ctx)) as agen:
                    async for event in agen:
                        yield event
                        if event.actions.escalate:
                            should_exit = True
                        if ctx.should_pause_invocation(event):
                            pause_invocation = True

                if should_exit or pause_invocation:
                    break

            start_index = 0
            times_looped += 1
            ctx.reset_sub_agent_states(self.name)

        if pause_invocation:
            return
