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

"""Inter-Echo Influence Network for the Leadership Crucible.

Models coalition dynamics between AI Echo stakeholders by propagating
sentiment along weighted edges after each tick.  In real organizations,
if the CTO and CFO form an opposing bloc, it's qualitatively different
from two isolated resistant stakeholders.

The network is defined in quest YAML as an optional ``influence_edges``
list and applied post-tick during aggregation.

No LLM dependency — pure deterministic graph propagation.
"""

import logging

logger = logging.getLogger(__name__)

# Default propagation rate — controls how much sentiment flows per tick.
# At 0.1, a sentiment delta of 0.5 between connected echoes produces
# a 0.05 shift per tick (converging over ~10 ticks).
DEFAULT_PROPAGATION_RATE = 0.1

# Alliance fatigue — edge weight decays slightly each tick to model
# the diminishing returns of coalition pressure.
DEFAULT_DECAY_PER_TICK = 0.005


class InfluenceEdge:
    """A directed influence relationship between two Echoes."""

    __slots__ = ("source", "target", "weight", "original_weight")

    def __init__(self, source: str, target: str, weight: float):
        self.source = source
        self.target = target
        self.weight = min(1.0, max(0.0, weight))
        self.original_weight = self.weight

    def decay(self, amount: float = DEFAULT_DECAY_PER_TICK) -> None:
        """Apply alliance fatigue — weight decays toward zero."""
        self.weight = max(0.0, self.weight - amount)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "weight": round(self.weight, 4),
            "original_weight": round(self.original_weight, 4),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "InfluenceEdge":
        edge = cls(d["source"], d["target"], d.get("original_weight", d["weight"]))
        edge.weight = d["weight"]
        return edge


class InfluenceNetwork:
    """Directed graph of inter-Echo influence relationships.

    After each tick, ``propagate()`` adjusts Echo sentiments based on
    their neighbors' sentiments and edge weights.
    """

    def __init__(self, edges: list[InfluenceEdge] | None = None):
        self.edges: list[InfluenceEdge] = edges or []
        self._adjacency: dict[str, list[InfluenceEdge]] = {}
        self._rebuild_adjacency()

    def _rebuild_adjacency(self) -> None:
        self._adjacency.clear()
        for edge in self.edges:
            self._adjacency.setdefault(edge.source, []).append(edge)

    def add_edge(self, source: str, target: str, weight: float) -> None:
        edge = InfluenceEdge(source, target, weight)
        self.edges.append(edge)
        self._adjacency.setdefault(source, []).append(edge)

    def propagate(
        self,
        echo_sentiments: dict[str, float],
        propagation_rate: float = DEFAULT_PROPAGATION_RATE,
    ) -> dict[str, float]:
        """Propagate sentiment along influence edges.

        Computes sentiment deltas for all target Echoes based on their
        source Echoes' sentiments and edge weights.  Returns a dict
        of role → sentiment delta (NOT the final sentiment).

        Args:
            echo_sentiments: Current sentiment by role name.
            propagation_rate: How much of the delta is applied per tick.

        Returns:
            Dict of role → sentiment delta to apply.
        """
        deltas: dict[str, float] = {}

        for source_role, edges in self._adjacency.items():
            src_sentiment = echo_sentiments.get(source_role)
            if src_sentiment is None:
                continue

            for edge in edges:
                dst_sentiment = echo_sentiments.get(edge.target)
                if dst_sentiment is None:
                    continue

                # Sentiment flows from source to target
                raw_delta = (src_sentiment - dst_sentiment) * edge.weight * propagation_rate
                deltas[edge.target] = deltas.get(edge.target, 0.0) + raw_delta

        return deltas

    def apply_decay(self) -> None:
        """Apply alliance fatigue to all edges."""
        for edge in self.edges:
            edge.decay()

    def to_dict_list(self) -> list[dict]:
        return [e.to_dict() for e in self.edges]

    @classmethod
    def from_dict_list(cls, edges: list[dict]) -> "InfluenceNetwork":
        return cls([InfluenceEdge.from_dict(e) for e in edges])

    @classmethod
    def from_quest_config(cls, quest_edges: list[dict] | None) -> "InfluenceNetwork":
        """Build network from quest YAML ``influence_edges`` list.

        Expected format:
        ```yaml
        influence_edges:
          - source: "cto"
            target: "data_scientist_lead"
            weight: 0.6
        ```

        Args:
            quest_edges: List of edge dicts from quest YAML.

        Returns:
            InfluenceNetwork instance.
        """
        if not quest_edges:
            return cls()
        edges = []
        for e in quest_edges:
            src = e.get("source", e.get("from", ""))
            dst = e.get("target", e.get("to", ""))
            weight = float(e.get("weight", 0.5))
            if src and dst:
                edges.append(InfluenceEdge(src, dst, weight))
        return cls(edges)

    def __len__(self) -> int:
        return len(self.edges)


def build_default_influence_edges(echo_configs: list[dict]) -> list[dict]:
    """Generate default influence edges based on role heuristics.

    When a quest YAML doesn't define ``influence_edges``, this function
    generates plausible edges based on common organizational relationships:

    - C-suite → their direct reports (moderate influence)
    - HR → union/workforce roles (solidarity influence)
    - Board → C-suite (authority influence)
    - Executive coach → all (weak advisory influence)

    Args:
        echo_configs: List of Echo config dicts from the quest.

    Returns:
        List of edge dicts suitable for InfluenceNetwork.from_quest_config.
    """
    roles = {e.get("role", ""): e for e in echo_configs if e.get("role")}
    edges: list[dict] = []

    # Known relationship patterns
    RELATIONSHIP_MAP = {
        # C-suite → technical
        ("cto", "data_scientist_lead"): 0.6,
        ("cto", "sre_lead"): 0.5,
        ("cto", "senior_engineer"): 0.4,
        ("cto", "vp_infrastructure"): 0.5,
        # C-suite → finance
        ("cfo", "board_member"): 0.7,
        ("cfo", "board_representative"): 0.7,
        ("cfo", "finance_analyst"): 0.5,
        # HR → workforce
        ("hr_director", "union_rep"): 0.6,
        ("hr_director", "middle_manager"): 0.5,
        ("head_hr", "union_rep"): 0.6,
        ("head_hr", "senior_engineer"): 0.4,
        ("head_hr", "support_team_lead"): 0.5,
        # Board authority
        ("board_member", "cfo"): 0.6,
        ("board_member", "cto"): 0.5,
        ("board_representative", "cfo"): 0.6,
        # Peer influence
        ("vp_operations", "middle_manager"): 0.5,
        ("vp_engineering", "senior_engineer"): 0.6,
        ("vp_engineering", "marketing_manager"): 0.3,
        # Product
        ("head_product", "junior_pm"): 0.5,
        ("head_product", "sales_director"): 0.4,
        # Investor
        ("lead_investor", "cto"): 0.4,
        ("lead_investor", "cmo"): 0.3,
        # CS/support
        ("head_cs", "support_team_lead"): 0.6,
    }

    for (src, dst), weight in RELATIONSHIP_MAP.items():
        if src in roles and dst in roles:
            edges.append({"source": src, "target": dst, "weight": weight})

    # Executive coach has weak advisory influence on all non-coach roles
    if "executive_coach" in roles:
        for role in roles:
            if role != "executive_coach":
                edges.append({"source": "executive_coach", "target": role, "weight": 0.15})

    logger.info(
        "Built %d default influence edges for %d echoes",
        len(edges),
        len(echo_configs),
    )
    return edges
