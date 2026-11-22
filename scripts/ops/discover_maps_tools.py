#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Modifications copyright 2026 Christian Haeussinger
# Changed for The Rehearsal: guard missing Available Tools heading and use explicit UTF-8 encoding.
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

"""Discover tool names from the Google Maps MCP server.

Usage:
    uv run python scripts/ops/discover_maps_tools.py

Requires:
    GOOGLE_CLOUD_PROJECT and GOOGLE_MAPS_API_KEY env vars.

Compares discovered tool names against agents/planner/skills/mapping/SKILL.md
and exits 0 if all documented tools exist, 1 if mismatched.
"""

import asyncio
import os
import re
import sys


async def main() -> int:
    from agents.planner.adk_tools import _MAPS_MCP_DISPLAY_NAME, _normalize_maps_toolset, header_provider
    from google.adk.integrations.agent_registry import AgentRegistry

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    maps_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()

    if not project_id or not maps_key:
        print("ERROR: Set GOOGLE_CLOUD_PROJECT and GOOGLE_MAPS_API_KEY", file=sys.stderr)
        return 2

    registry = AgentRegistry(
        project_id=project_id,
        location="global",
        header_provider=header_provider,
    )

    # Discover Maps MCP server by display name
    servers = registry.list_mcp_servers()
    mcp_server_name = None
    for server in servers.get("mcpServers", []):
        if server.get("displayName") == _MAPS_MCP_DISPLAY_NAME:
            mcp_server_name = server["name"]
            break

    if not mcp_server_name:
        print(f"ERROR: Maps MCP server ({_MAPS_MCP_DISPLAY_NAME}) not found in Agent Registry", file=sys.stderr)
        return 2

    toolset = _normalize_maps_toolset(registry.get_mcp_toolset(mcp_server_name=mcp_server_name))

    # Resolve tools from MCP server
    tools = await toolset.get_tools()

    print(f"\n{'Tool Name':<40} {'Description'}")
    print("-" * 80)
    actual_names = set()
    for tool in tools:
        name = tool.name if hasattr(tool, "name") else type(tool).__name__
        desc = (getattr(tool, "description", None) or "")[:60]
        print(f"{name:<40} {desc}")
        actual_names.add(name)

    # Extract backtick-quoted tool names from the "## Available Tools" section of SKILL.md.
    skill_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "agents", "planner", "skills", "mapping", "SKILL.md"
    )
    with open(skill_path, encoding="utf-8") as f:
        content = f.read()

    documented = extract_documented_tools(content)

    print(f"\nDocumented in SKILL.md: {sorted(documented)}")
    print(f"Actual from MCP server: {sorted(actual_names)}")

    missing = documented - actual_names
    extra = actual_names - documented

    if missing:
        print(f"\nMISSING (in SKILL.md but not on server): {sorted(missing)}")
    if extra:
        print(f"\nEXTRA (on server but not in SKILL.md): {sorted(extra)}")

    if missing:
        print("\nFAIL: SKILL.md references tools that don't exist on the MCP server.")
        print("Update agents/planner/skills/mapping/SKILL.md with correct tool names.")
        return 1

    print("\nPASS: All documented tools exist on the MCP server.")
    return 0


def extract_documented_tools(content: str) -> set[str]:
    """Return backtick-quoted tool names under the '## Available Tools' section."""
    marker = "## Available Tools"
    if marker not in content:
        return set()
    section = content.split(marker, 1)[1]
    next_heading = re.search(r"^##\s+", section, re.MULTILINE)
    if next_heading:
        section = section[: next_heading.start()]
    return set(re.findall(r"`(\w+)`", section))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
