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

"""Dynamic Consensus Navigation System (DCNS) — Mathematical Engine.

Mirrors the Marathon route engine (agents/planner/skills/gis-spatial-engineering/
scripts/tools.py) but operates on organizational influence graphs instead of
road networks.  Outputs GeoJSON compatible with the existing 3D viewport via
hud:importPath.

Dependencies: Python stdlib only (heapq, math, random, json).
"""

from __future__ import annotations

import copy
import json
import logging
import math
import random
from heapq import heappop, heappush
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONSENSUS_TARGETS: Dict[str, float] = {
    "crisis": 0.70,
    "transformation": 0.85,
    "merger": 0.92,
}

_INFLUENCE_RESERVE = 0.15  # Budget reserved for final consensus lock-in

LAS_VEGAS_BOUNDS = {
    "lon_min": -115.185,
    "lon_max": -115.135,
    "lat_min": 36.080,
    "lat_max": 36.145,
}

# Scalarization weights for consensus pathway (time, capital, morale)
_PATHWAY_WEIGHTS: Tuple[float, float, float] = (1.0, 1.0, 1.0)

CostVector = Tuple[float, float, float]

# ---------------------------------------------------------------------------
# Leadership Landmark Semantic Mapping
# ---------------------------------------------------------------------------
_LEADERSHIP_LANDMARKS = {
    "Las Vegas Sign": "Founding Vision",
    "Treasure Island": "Innovation Hub",
    "Michelob Ultra Arena": "Consensus Arena",
    "Bellagio": "Executive Boardroom",
    "Caesars Palace": "Governance Chamber",
    "MGM Grand": "Operations Center",
    "Flamingo": "Stakeholder Lounge",
    "Sphere": "Data Intelligence",
    "The Venetian": "Coalition Forum",
    "Mandalay Bay": "Resource Treasury",
    "Luxor": "Ethics Observatory",
    "Excalibur": "Crisis Command",
    "New York": "Change Laboratory",
    "High Roller": "Strategic Outlook",
    "The STRAT": "Resilience Tower",
    "Eiffel Tower": "Cultural Bridge",
    "Allegiant Stadium": "Workforce Assembly",
}

# ---------------------------------------------------------------------------
# Organizational Terrain: Venue Type Factors (Places API)
# ---------------------------------------------------------------------------
# Maps venue categories (from Places API `types` field) to cost modifiers.
# See concepts/organizational_terrain_maps_apis.md for rationale.

_VENUE_TIME_FACTORS: Dict[str, float] = {
    "government": 1.3,
    "courthouse": 1.3,
    "university": 0.9,
    "research": 0.9,
    "corporate_tower": 1.0,
    "convention_center": 1.0,
    "resort_corporate": 0.95,
    "startup": 0.85,
    "incubator": 0.85,
    "tech_hub": 0.85,
    "hospital": 0.7,
    "emergency": 0.7,
    "union_hall": 1.1,
    "community_center": 1.1,
}

_VENUE_MORALE_FACTORS: Dict[str, float] = {
    "government": 1.0,
    "courthouse": 1.0,
    "university": 0.85,
    "research": 0.85,
    "corporate_tower": 1.0,
    "convention_center": 1.0,
    "resort_corporate": 0.9,
    "startup": 0.8,
    "incubator": 0.8,
    "tech_hub": 0.8,
    "hospital": 1.2,
    "emergency": 1.2,
    "union_hall": 0.9,
    "community_center": 0.9,
}

# Organizational Terrain: Engagement Friction (Routes API)
_FRICTION_THRESHOLDS: List[Tuple[float, float]] = [
    (5.0, 0.85),  # co-located: natural coalition
    (10.0, 1.0),  # accessible: baseline
    (20.0, 1.15),  # deliberate effort required
    (30.0, 1.35),  # different worlds
]
_FRICTION_MAX = 1.5  # organizational silo (30+ min)

# Default weather modifier (no environmental pressure)
_DEFAULT_WEATHER: Dict[str, Any] = {
    "time_factor": 1.0,
    "capital_factor": 1.0,
    "morale_factor": 1.0,
    "resistance_delta": 0.0,
    "distance_factor": 1.0,
    "perturbation_range": (0.7, 1.5),
}


def _friction_multiplier(travel_minutes: Optional[float]) -> float:
    """Convert travel duration to an engagement friction multiplier.

    Short travel times indicate co-located stakeholders who form natural
    coalitions (multiplier < 1).  Long travel times indicate organizational
    silos requiring deliberate bridging effort (multiplier > 1).
    """
    if travel_minutes is None:
        return 1.0
    for threshold, factor in _FRICTION_THRESHOLDS:
        if travel_minutes <= threshold:
            return factor
    return _FRICTION_MAX


# ---------------------------------------------------------------------------
# 1. Influence Distance  (replaces _haversine)
# ---------------------------------------------------------------------------


def _influence_distance(s1: Dict, s2: Dict) -> float:
    """Compute influence distance in [0, 1] between two stakeholders."""
    a1 = s1["affinity"]
    a2 = s2["affinity"]
    dot = sum(x * y for x, y in zip(a1, a2))
    n1 = math.sqrt(sum(x * x for x in a1))
    n2 = math.sqrt(sum(y * y for y in a2))
    cosine = dot / (n1 * n2) if n1 * n2 > 0 else 0.0

    p1 = set(s1.get("domain_principles", []))
    p2 = set(s2.get("domain_principles", []))
    denom = max(len(p1), len(p2), 1)
    overlap = len(p1 & p2) / denom

    return max(0.0, min(1.0, 1.0 - (cosine * 0.7 + overlap * 0.3)))


# ---------------------------------------------------------------------------
# 2. Resource-Calibrated Edge Cost  (replaces scalar distance weight)
# ---------------------------------------------------------------------------


def _edge_cost(
    source: Dict,
    target: Dict,
    resources: Dict[str, Dict[str, Any]],
    venue_types: Optional[Dict[str, str]] = None,
    travel_matrix: Optional[Dict[str, float]] = None,
    weather: Optional[Dict[str, Any]] = None,
) -> CostVector:
    """Compute 3-D cost vector [time, capital, morale] for an edge.

    Optional Organizational Terrain modifiers (all default to neutral):
      *venue_types*  — Places API: venue category affects time/morale factors
      *travel_matrix* — Routes API: travel duration adds engagement friction
      *weather*       — Weather API: environmental pressure on all dimensions
    """
    base = _influence_distance(source, target)

    # Apply weather distance factor (fog amplifies perceived gaps)
    w = weather or _DEFAULT_WEATHER
    base *= w.get("distance_factor", 1.0)

    owb = resources.get("OWB", {})
    owb_crit = owb.get("critical_threshold", 25)
    owb_init = owb.get("initial", 70)
    owb_mult = 1.5 if owb_init < owb_crit else 1.0

    resist = target.get("initial_resistance", 0.3)
    influence = target.get("initial_influence", 0.5)
    sentiment = target.get("initial_sentiment", 0.5)

    time_cost = base * (1 + resist) * owb_mult
    capital_cost = base * resist * (1 - influence) * owb_mult
    morale_cost = base * (1 - sentiment) * owb_mult

    # --- Organizational Terrain: Venue Type Factors (Places API) ---
    if venue_types:
        tgt_type = venue_types.get(target.get("role", ""), "")
        time_cost *= _VENUE_TIME_FACTORS.get(tgt_type, 1.0)
        morale_cost *= _VENUE_MORALE_FACTORS.get(tgt_type, 1.0)

    # --- Organizational Terrain: Engagement Friction (Routes API) ---
    if travel_matrix:
        src_role = source.get("role", "")
        tgt_role = target.get("role", "")
        key = f"{src_role}→{tgt_role}"
        reverse_key = f"{tgt_role}→{src_role}"
        minutes = travel_matrix.get(key) or travel_matrix.get(reverse_key)
        friction = _friction_multiplier(minutes)
        time_cost *= friction
        capital_cost *= friction
        morale_cost *= friction

    # --- Organizational Terrain: Weather Pressure (Weather API) ---
    time_cost *= w.get("time_factor", 1.0)
    capital_cost *= w.get("capital_factor", 1.0)
    morale_cost *= w.get("morale_factor", 1.0)

    return (time_cost, capital_cost, morale_cost)


# ---------------------------------------------------------------------------
# 3. Build Influence Graph  (replaces _build_graph)
# ---------------------------------------------------------------------------


def _build_influence_graph(
    stakeholders: List[Dict],
    resources: Dict[str, Dict[str, Any]],
    venue_types: Optional[Dict[str, str]] = None,
    travel_matrix: Optional[Dict[str, float]] = None,
    weather: Optional[Dict[str, Any]] = None,
) -> Dict[int, List[Tuple[int, CostVector]]]:
    """Return adjacency list  {node_idx: [(neighbor_idx, cost_vector), ...]}."""
    n = len(stakeholders)
    adj: Dict[int, List[Tuple[int, CostVector]]] = {i: [] for i in range(n)}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            cost = _edge_cost(
                stakeholders[i],
                stakeholders[j],
                resources,
                venue_types=venue_types,
                travel_matrix=travel_matrix,
                weather=weather,
            )
            adj[i].append((j, cost))
    return adj


# ---------------------------------------------------------------------------
# 4. Scale Influence Graph  (Q2 — deliberate Marathon divergence)
# ---------------------------------------------------------------------------


def _interpolate_stakeholder(
    parent: Dict,
    tier: int,
    rng: random.Random,
    index: int,
) -> Dict:
    """Create a synthetic intermediate stakeholder derived from *parent*."""
    noise = [rng.gauss(0, 0.08) for _ in parent["affinity"]]
    affinity = [max(0.0, min(1.0, a + n)) for a, n in zip(parent["affinity"], noise)]
    return {
        "role": f"synthetic_{parent['role']}_{index}",
        "tier": tier,
        "persona": f"Synthetic intermediary derived from {parent['role']}.",
        "domain_principles": list(parent.get("domain_principles", [])),
        "affinity": affinity,
        "initial_sentiment": max(0.0, min(1.0, parent.get("initial_sentiment", 0.5) + rng.gauss(0, 0.1))),
        "initial_resistance": max(0.0, min(1.0, parent.get("initial_resistance", 0.3) + rng.gauss(0, 0.1))),
        "initial_influence": max(0.0, min(1.0, parent.get("initial_influence", 0.5) * 0.7)),
    }


def _scale_influence_graph(
    base_templates: List[Dict],
    runner_count: int,
    rng: random.Random,
) -> List[Dict]:
    """Generate synthetic intermediate nodes for large organizations."""
    if runner_count <= 50:
        return copy.deepcopy(base_templates)

    scaled = copy.deepcopy(base_templates)
    tier1 = [t for t in base_templates if t.get("tier") == 1]
    if not tier1:
        tier1 = base_templates[:2]

    num_intermediaries = min(runner_count // 50, 20)
    for i in range(num_intermediaries):
        parent = tier1[i % len(tier1)]
        scaled.append(_interpolate_stakeholder(parent, tier=2, rng=rng, index=i))

    if runner_count > 500:
        num_frontline = min(runner_count // 200, 30)
        base_intermed = len(base_templates)
        for i in range(num_frontline):
            parent = scaled[base_intermed + i % num_intermediaries]
            scaled.append(_interpolate_stakeholder(parent, tier=3, rng=rng, index=i))

    return scaled


# ---------------------------------------------------------------------------
# 5. Geographic Projection  (PCA → Las Vegas lon/lat)
# ---------------------------------------------------------------------------


def _power_iteration(matrix: List[List[float]], iterations: int = 50) -> List[float]:
    """Find dominant eigenvector of a square matrix via power iteration."""
    n = len(matrix)
    v = [1.0 / math.sqrt(n)] * n
    for _ in range(iterations):
        mv = [sum(matrix[i][j] * v[j] for j in range(n)) for i in range(n)]
        norm = math.sqrt(sum(x * x for x in mv)) or 1.0
        v = [x / norm for x in mv]
    return v


def _deflate(matrix: List[List[float]], eigvec: List[float]) -> List[List[float]]:
    """Deflate matrix by removing component along eigvec."""
    n = len(matrix)
    mv = [sum(matrix[i][j] * eigvec[j] for j in range(n)) for i in range(n)]
    eigenvalue = sum(mv[i] * eigvec[i] for i in range(n))
    return [[matrix[i][j] - eigenvalue * eigvec[i] * eigvec[j] for j in range(n)] for i in range(n)]


def _project_to_geo(stakeholders: List[Dict]) -> List[Tuple[float, float]]:
    """Map stakeholder affinity vectors to [lon, lat] within Las Vegas bounds."""
    affinities = [s["affinity"] for s in stakeholders]
    n = len(affinities)
    d = len(affinities[0])

    mean = [sum(a[j] for a in affinities) / n for j in range(d)]
    centered = [[a[j] - mean[j] for j in range(d)] for a in affinities]

    # Covariance matrix (d×d)
    cov = [[sum(centered[i][j] * centered[i][k] for i in range(n)) / max(n, 1) for k in range(d)] for j in range(d)]

    v1 = _power_iteration(cov)
    cov2 = _deflate(cov, v1)
    v2 = _power_iteration(cov2)

    coords = [(sum(c[j] * v1[j] for j in range(d)), sum(c[j] * v2[j] for j in range(d))) for c in centered]

    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    x_range = max(xs) - min(xs) or 1.0
    y_range = max(ys) - min(ys) or 1.0

    b = LAS_VEGAS_BOUNDS
    return [
        (
            b["lon_min"] + (x - min(xs)) / x_range * (b["lon_max"] - b["lon_min"]),
            b["lat_min"] + (y - min(ys)) / y_range * (b["lat_max"] - b["lat_min"]),
        )
        for x, y in coords
    ]


def _project_to_real_venues(
    stakeholders: List[Dict],
    venue_coords: Dict[str, Tuple[float, float]],
) -> List[Tuple[float, float]]:
    """Map stakeholders to real-world venue coordinates when available.

    Each stakeholder is anchored to a venue whose key matches the
    stakeholder's ``role``.  Synthetic intermediary stakeholders
    (created by ``_scale_influence_graph``) are jittered around the
    venue of their parent role so they cluster nearby without
    overlapping.

    Falls back to the Las Vegas bounding-box center when a role has no
    matching venue entry.
    """
    rng = random.Random(hash(tuple(sorted(venue_coords.keys()))))
    b = LAS_VEGAS_BOUNDS
    center = (
        (b["lon_min"] + b["lon_max"]) / 2,
        (b["lat_min"] + b["lat_max"]) / 2,
    )
    coords: List[Tuple[float, float]] = []
    for s in stakeholders:
        role = s["role"]
        if role in venue_coords:
            coords.append(venue_coords[role])
        else:
            # Synthetic stakeholders: derive parent role and jitter.
            # Synthetic roles follow the pattern "synthetic_<parent>_<index>".
            parts = role.split("_")
            parent_role = parts[1] if len(parts) >= 3 and parts[0] == "synthetic" else role
            base = venue_coords.get(parent_role, center)
            jitter = (rng.gauss(0, 0.002), rng.gauss(0, 0.002))
            coords.append((base[0] + jitter[0], base[1] + jitter[1]))
    return coords


# ---------------------------------------------------------------------------
# 6. Executive Corridor  (replaces _build_strip_corridor)
# ---------------------------------------------------------------------------


def _build_executive_corridor(stakeholders: List[Dict]) -> List[int]:
    """Return indices of Tier 1 stakeholders sorted by descending influence."""
    tier1 = [(i, s) for i, s in enumerate(stakeholders) if s.get("tier") == 1]
    if not tier1:
        tier1 = [(i, s) for i, s in enumerate(stakeholders)][:3]
    tier1.sort(key=lambda x: x[1].get("initial_influence", 0), reverse=True)
    return [i for i, _ in tier1]


# ---------------------------------------------------------------------------
# 7. Coalition Waypoints  (replaces _build_serpentine_waypoints)
# ---------------------------------------------------------------------------


def _build_coalition_waypoints(
    stakeholders: List[Dict],
    corridor_indices: List[int],
    rng: random.Random,
) -> List[int]:
    """Zigzag through non-corridor stakeholders: alternate allies/resistors."""
    remaining = [(i, s) for i, s in enumerate(stakeholders) if i not in corridor_indices]
    remaining.sort(key=lambda x: x[1].get("initial_resistance", 0.5))

    mid = len(remaining) // 2
    allies = remaining[:mid]
    resistors = remaining[mid:]

    waypoints: List[int] = []
    for idx in range(max(len(allies), len(resistors))):
        if idx < len(allies):
            waypoints.append(allies[idx][0])
        if idx < len(resistors):
            waypoints.append(resistors[idx][0])
    return waypoints


# ---------------------------------------------------------------------------
# 8. Multi-Objective Dijkstra  (replaces _get_path_dijkstra_perturbed)
# ---------------------------------------------------------------------------


def _scalarize(cost: CostVector, weights: Tuple[float, float, float]) -> float:
    return sum(c * w for c, w in zip(cost, weights))


def _get_path_pareto_perturbed(
    start: int,
    end: int,
    adj: Dict[int, List[Tuple[int, CostVector]]],
    weights: Tuple[float, float, float],
    rng: random.Random,
    visited: Optional[set] = None,
    perturbation_range: Optional[Tuple[float, float]] = None,
) -> Tuple[List[int], CostVector]:
    """Weighted-scalarization Dijkstra with perturbation for variety.

    *perturbation_range* controls outcome variability.  High-wind weather
    widens this to (0.5, 2.0), making pathways more unpredictable.
    """
    p_lo, p_hi = perturbation_range or (0.7, 1.5)
    visited = visited or set()
    queue: list = [(0.0, start, [start], (0.0, 0.0, 0.0))]
    best: Dict[int, float] = {start: 0.0}

    while queue:
        sc, curr, path, curr_cost = heappop(queue)
        if curr == end:
            return path, curr_cost
        for neighbor, edge_cost in adj[curr]:
            if neighbor in visited or neighbor in path:
                continue
            factor = rng.uniform(p_lo, p_hi)
            new_cost = tuple(c + e * factor for c, e in zip(curr_cost, edge_cost))
            scalar = _scalarize(new_cost, weights)  # type: ignore[arg-type]
            if scalar < best.get(neighbor, float("inf")):
                best[neighbor] = scalar
                heappush(queue, (scalar, neighbor, path + [neighbor], new_cost))

    return [], (float("inf"), float("inf"), float("inf"))


# ---------------------------------------------------------------------------
# 9. Generate Consensus Pathway  (replaces _generate_zone_sweep_route)
# ---------------------------------------------------------------------------


def _consensus_score(path: List[int], stakeholders: List[Dict]) -> float:
    """Estimate consensus achieved: weighted sum of aligned stakeholders."""
    if not path:
        return 0.0
    total_influence = sum(s.get("initial_influence", 0.5) for s in stakeholders)
    if total_influence == 0:
        return 0.0
    aligned = sum(
        stakeholders[i].get("initial_influence", 0.5) * (1 - stakeholders[i].get("initial_resistance", 0.5) * 0.5)
        for i in path
    )
    return min(1.0, aligned / total_influence)


def _validate_pathway(
    path: List[int],
    stakeholders: List[Dict],
    target: float,
) -> bool:
    """Check: no loops, non-empty, consensus ≥ target."""
    if not path or len(path) != len(set(path)):
        return False
    return _consensus_score(path, stakeholders) >= target * 0.9  # 90% tolerance


def _generate_consensus_pathway(
    stakeholders: List[Dict],
    resources: Dict[str, Dict[str, Any]],
    weights: Tuple[float, float, float],
    rng: random.Random,
    target_consensus: float,
    venue_types: Optional[Dict[str, str]] = None,
    travel_matrix: Optional[Dict[str, float]] = None,
    weather: Optional[Dict[str, Any]] = None,
) -> Tuple[List[int], CostVector]:
    """Build a full consensus pathway: corridor → coalition → anchor."""
    adj = _build_influence_graph(
        stakeholders,
        resources,
        venue_types=venue_types,
        travel_matrix=travel_matrix,
        weather=weather,
    )
    corridor = _build_executive_corridor(stakeholders)
    coalition = _build_coalition_waypoints(stakeholders, corridor, rng)

    # Full ordered path: corridor nodes first, then coalition zigzag
    ordered = corridor + coalition
    # Ensure all nodes are included
    all_indices = set(range(len(stakeholders)))
    remaining = list(all_indices - set(ordered))
    rng.shuffle(remaining)
    ordered += remaining

    # Perturbation range from weather (high wind → wider variance)
    w = weather or _DEFAULT_WEATHER
    p_range = tuple(w.get("perturbation_range", (0.7, 1.5)))

    # Build path greedily through ordered waypoints using Dijkstra segments
    full_path: List[int] = [ordered[0]]
    total_cost: CostVector = (0.0, 0.0, 0.0)
    visited: set = {ordered[0]}

    for wp in ordered[1:]:
        if wp in visited:
            continue
        segment, seg_cost = _get_path_pareto_perturbed(
            full_path[-1],
            wp,
            adj,
            weights,
            rng,
            visited,
            perturbation_range=p_range,
        )
        if not segment:
            full_path.append(wp)
            ec = _edge_cost(
                stakeholders[full_path[-2]],
                stakeholders[wp],
                resources,
                venue_types=venue_types,
                travel_matrix=travel_matrix,
                weather=weather,
            )
            total_cost = tuple(a + b for a, b in zip(total_cost, ec))  # type: ignore[assignment]
        else:
            for node in segment[1:]:
                if node not in visited:
                    full_path.append(node)
                    visited.add(node)
            total_cost = tuple(a + b for a, b in zip(total_cost, seg_cost))  # type: ignore[assignment]
        visited.add(wp)

    return full_path, total_cost  # type: ignore[return-value]


def _generate_best_pathway(
    stakeholders: List[Dict],
    resources: Dict[str, Dict[str, Any]],
    weights: Tuple[float, float, float],
    target_consensus: float,
    num_candidates: int = 10,
    base_seed: Optional[int] = None,
    venue_types: Optional[Dict[str, str]] = None,
    travel_matrix: Optional[Dict[str, float]] = None,
    weather: Optional[Dict[str, Any]] = None,
) -> Tuple[List[int], CostVector]:
    """Tournament: generate multiple candidates, pick best valid one."""
    if base_seed is None:
        base_seed = random.randrange(2**32)
    best_path: List[int] = []
    best_cost: CostVector = (float("inf"), float("inf"), float("inf"))
    best_score = -1.0

    for i in range(num_candidates):
        seed = base_seed + i
        rng = random.Random(seed)
        path, cost = _generate_consensus_pathway(
            stakeholders,
            resources,
            weights,
            rng,
            target_consensus,
            venue_types=venue_types,
            travel_matrix=travel_matrix,
            weather=weather,
        )
        score = _consensus_score(path, stakeholders)
        scalar = _scalarize(cost, weights)
        if score > best_score or (score == best_score and scalar < _scalarize(best_cost, weights)):
            best_path = path
            best_cost = cost
            best_score = score

    return best_path, best_cost


# ---------------------------------------------------------------------------
# 10. Station Placement  (replaces hydration / medical / cheer)
# ---------------------------------------------------------------------------


def _place_alignment_checkpoints(
    path: List[int],
    stakeholders: List[Dict],
    geo_coords: List[Tuple[float, float]],
    race_distance_mi: float = 26.2,
) -> List[Dict]:
    """Place alignment checkpoints at Tier 1 stakeholder positions."""
    points = []
    for idx, node in enumerate(path):
        s = stakeholders[node]
        if s.get("tier") == 1:
            points.append(
                {
                    "type": "water_station",
                    "mi": round((idx / max(len(path) - 1, 1)) * race_distance_mi, 2),
                    "coords": list(geo_coords[node]),
                    "stakeholder": s["role"],
                }
            )
    return points


def _place_risk_nodes(
    path: List[int],
    stakeholders: List[Dict],
    geo_coords: List[Tuple[float, float]],
    race_distance_mi: float = 26.2,
) -> List[Dict]:
    """Place risk nodes at high-resistance stakeholder positions."""
    points = []
    for idx, node in enumerate(path):
        s = stakeholders[node]
        if s.get("initial_resistance", 0) >= 0.5:
            points.append(
                {
                    "type": "medical_tent",
                    "mi": round((idx / max(len(path) - 1, 1)) * race_distance_mi, 2),
                    "coords": list(geo_coords[node]),
                    "stakeholder": s["role"],
                }
            )
    return points


def _place_coalition_zones(
    path: List[int],
    stakeholders: List[Dict],
    geo_coords: List[Tuple[float, float]],
    race_distance_mi: float = 26.2,
) -> List[Dict]:
    """Place coalition zones where multiple aligned stakeholders cluster."""
    points = []
    for idx, node in enumerate(path):
        s = stakeholders[node]
        if s.get("initial_sentiment", 0) >= 0.6 and s.get("initial_resistance", 1) < 0.3:
            points.append(
                {
                    "type": "crowd_zone",
                    "mi": round((idx / max(len(path) - 1, 1)) * race_distance_mi, 2),
                    "coords": list(geo_coords[node]),
                    "stakeholder": s["role"],
                }
            )
    return points


# ---------------------------------------------------------------------------
# 11. GeoJSON Output  (replaces _split_route_by_road + markers)
# ---------------------------------------------------------------------------


def _pathway_to_geojson(
    path: List[int],
    cost: CostVector,
    stakeholders: List[Dict],
    geo_coords: List[Tuple[float, float]],
    scenario_type: str,
    race_distance_mi: float = 26.2,
) -> Dict[str, Any]:
    """Convert pathway to GeoJSON FeatureCollection for hud:importPath."""
    features: List[Dict] = []

    # Start marker
    if path:
        start_s = stakeholders[path[0]]
        features.append(
            {
                "type": "Feature",
                "properties": {"marker-type": "start", "name": start_s["role"]},
                "geometry": {"type": "Point", "coordinates": list(geo_coords[path[0]])},
            }
        )

    # Finish marker
    if len(path) > 1:
        end_s = stakeholders[path[-1]]
        features.append(
            {
                "type": "Feature",
                "properties": {"marker-type": "finish", "name": end_s["role"]},
                "geometry": {"type": "Point", "coordinates": list(geo_coords[path[-1]])},
            }
        )

    # Pathway LineString
    line_coords = [list(geo_coords[node]) for node in path]
    if line_coords:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": "DCNS Pathway",
                    "route_type": "consensus",
                    "consensus_score": round(_consensus_score(path, stakeholders), 3),
                },
                "geometry": {"type": "LineString", "coordinates": line_coords},
            }
        )

    # Station points
    for pt in _place_alignment_checkpoints(path, stakeholders, geo_coords, race_distance_mi):
        features.append(
            {
                "type": "Feature",
                "properties": {"type": pt["type"], "mi": pt["mi"], "stakeholder": pt["stakeholder"]},
                "geometry": {"type": "Point", "coordinates": pt["coords"]},
            }
        )
    for pt in _place_risk_nodes(path, stakeholders, geo_coords, race_distance_mi):
        features.append(
            {
                "type": "Feature",
                "properties": {"type": pt["type"], "mi": pt["mi"], "stakeholder": pt["stakeholder"]},
                "geometry": {"type": "Point", "coordinates": pt["coords"]},
            }
        )
    for pt in _place_coalition_zones(path, stakeholders, geo_coords, race_distance_mi):
        features.append(
            {
                "type": "Feature",
                "properties": {"type": pt["type"], "mi": pt["mi"], "stakeholder": pt["stakeholder"]},
                "geometry": {"type": "Point", "coordinates": pt["coords"]},
            }
        )

    return {
        "type": "FeatureCollection",
        "dcns_metadata": {
            "consensus_score": round(_consensus_score(path, stakeholders), 3),
            "cost_vector": {"time": round(cost[0], 2), "capital": round(cost[1], 2), "morale": round(cost[2], 2)},
            "scenario_type": scenario_type,
            "stakeholder_count": len(stakeholders),
            "path_length": len(path),
        },
        "features": features,
    }


# ---------------------------------------------------------------------------
# 12. Generate Single Consensus Pathway
# ---------------------------------------------------------------------------


def generate_consensus_pathway(
    stakeholders: List[Dict],
    resources: Dict[str, Dict[str, float]],
    scenario_type: str = "transformation",
    runner_count: int = 100,
    seed: Optional[int] = None,
    venue_coords: Optional[Dict[str, Tuple[float, float]]] = None,
    network_path: Optional[str] = None,
    venue_types: Optional[Dict[str, str]] = None,
    travel_matrix: Optional[Dict[str, float]] = None,
    weather_modifier: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate the consensus pathway, using real road network if available.

    Organizational Terrain modifiers (all optional, gracefully degrade):
      *venue_types*       — Places API venue categories for cost modifiers
      *travel_matrix*     — Routes API travel durations for friction
      *weather_modifier*  — Weather API environmental pressure on costs
    """
    # Mirror Marathon's _generate_best_route: generate a random seed if not
    # provided, so every session produces a unique pathway.
    if seed is None:
        seed = random.randrange(2**32)
    # 1. Fallback to PCA if no network path
    import os

    if not network_path or not os.path.exists(network_path):
        logger.warning("DCNS: network.json not found, falling back to PCA projection")
        return _generate_consensus_pathway_pca(
            stakeholders,
            resources,
            scenario_type,
            runner_count,
            seed,
            venue_coords,
            venue_types=venue_types,
            travel_matrix=travel_matrix,
            weather_modifier=weather_modifier,
        )

    # 2. Use Marathon Road Network Routing
    logger.info("DCNS: Using marathon road network routing for consensus pathway")

    # Import marathon tools dynamically
    import importlib.util as _ilu
    import pathlib as _pl

    _marathon_path = (
        _pl.Path(__file__).resolve().parent.parent.parent.parent.parent
        / "planner"
        / "skills"
        / "gis-spatial-engineering"
        / "scripts"
        / "tools.py"
    )
    _spec = _ilu.spec_from_file_location("marathon_tools", _marathon_path)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Failed to load marathon_tools from {_marathon_path}")
    marathon_tools = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(marathon_tools)

    with open(network_path, "r") as f:
        geojson_data = f.read()

    data = json.loads(geojson_data)
    adj, landmarks, road_names, strip_nodes = marathon_tools._build_graph(data)
    nodes = set(adj.keys())

    # Route generation - zone-sweep algorithm
    route_coords, final_dist = marathon_tools._generate_best_route(
        adj,
        nodes,
        landmarks,
        strip_nodes,
        road_names,
        seed=seed,
        max_candidates=10,
    )

    if not route_coords:
        logger.warning("DCNS: Road routing failed, falling back to PCA")
        return _generate_consensus_pathway_pca(
            stakeholders,
            resources,
            scenario_type,
            runner_count,
            seed,
            venue_coords,
            venue_types=venue_types,
            travel_matrix=travel_matrix,
            weather_modifier=weather_modifier,
        )

    # Calculate logical path and score using DCNS mathematical engine
    target = _CONSENSUS_TARGETS.get(scenario_type, 0.85)
    rng = random.Random(seed)
    scaled = _scale_influence_graph(stakeholders, runner_count, rng)

    # Apply weather resistance delta globally (storm makes everyone defensive)
    w = weather_modifier or _DEFAULT_WEATHER
    r_delta = w.get("resistance_delta", 0.0)
    if r_delta:
        for s in scaled:
            s["initial_resistance"] = min(1.0, s.get("initial_resistance", 0.3) + r_delta)

    best_path, best_cost = _generate_best_pathway(
        scaled,
        resources,
        _PATHWAY_WEIGHTS,
        target,
        num_candidates=8,
        base_seed=seed,
        venue_types=venue_types,
        travel_matrix=travel_matrix,
        weather=weather_modifier,
    )
    score = _consensus_score(best_path, scaled)

    # Map landmarks
    mapped_features = []

    # Add the continuous route LineString
    mapped_features.append(
        {
            "type": "Feature",
            "properties": {
                "name": "DCNS Pathway",
                "route_type": "consensus",
                "consensus_score": round(score, 3),
            },
            "geometry": {"type": "LineString", "coordinates": [list(c) for c in route_coords]},
        }
    )

    # Associate landmarks with leadership semantics
    for name, coord in landmarks.items():
        if name in _LEADERSHIP_LANDMARKS:
            # Check if landmark is near the route
            min_dist = min(marathon_tools._haversine(coord, rc) for rc in route_coords)
            if min_dist < 0.2:  # Only show landmarks near the pathway
                mapped_features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "type": "station",
                            "name": _LEADERSHIP_LANDMARKS[name],
                            "original_name": name,
                            "marker-type": "start"
                            if name == "Las Vegas Sign"
                            else "finish"
                            if name == "Michelob Ultra Arena"
                            else "waypoint",
                        },
                        "geometry": {"type": "Point", "coordinates": list(coord)},
                    }
                )

    geojson = {
        "type": "FeatureCollection",
        "dcns_metadata": {
            "consensus_score": round(score, 3),
            "cost_vector": {
                "time": round(best_cost[0], 2),
                "capital": round(best_cost[1], 2),
                "morale": round(best_cost[2], 2),
            },
            "scenario_type": scenario_type,
            "stakeholder_count": len(scaled),
            "runner_count": runner_count,
            "seed": seed,
            "projection": "road_network",
        },
        "features": mapped_features,
    }

    return {
        "pathway": [
            {
                "role": scaled[n]["role"],
                "tier": scaled[n]["tier"],
                "coordinates": list(
                    route_coords[int(i * (len(route_coords) - 1) / max(1, len(best_path) - 1))]
                ),
            }
            for i, n in enumerate(best_path)
        ],
        "cost": {"time": round(best_cost[0], 2), "capital": round(best_cost[1], 2), "morale": round(best_cost[2], 2)},
        "consensus_score": score,
        "geojson": geojson,
    }


def _generate_consensus_pathway_pca(
    stakeholders: List[Dict],
    resources: Dict[str, Dict[str, Any]],
    scenario_type: str = "transformation",
    runner_count: int = 100,
    seed: Optional[int] = None,
    venue_coords: Optional[Dict[str, Tuple[float, float]]] = None,
    venue_types: Optional[Dict[str, str]] = None,
    travel_matrix: Optional[Dict[str, float]] = None,
    weather_modifier: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate a single consensus pathway (pragmatist strategy).

    Returns a dict containing:
      - geojson: FeatureCollection for hud:importPath
      - path: list of node indices
      - cost: {time, capital, morale} dict
      - consensus_score: float

    When *venue_coords* is provided (a dict mapping stakeholder role names
    to (lon, lat) tuples obtained from Google Maps ``search_places``),
    stakeholders are anchored to real-world Las Vegas venues instead of
    synthetic PCA-projected coordinates.  If *venue_coords* is ``None``
    the engine falls back to the default PCA projection.

    Organizational Terrain modifiers (all optional, gracefully degrade):
      *venue_types*       — Places API venue categories for cost modifiers
      *travel_matrix*     — Routes API travel durations for friction
      *weather_modifier*  — Weather API environmental pressure on costs
    """
    resolved_seed = seed if seed is not None else random.randrange(2**32)
    rng = random.Random(resolved_seed)
    target = _CONSENSUS_TARGETS.get(scenario_type, 0.85)

    # Scale graph if needed
    scaled = _scale_influence_graph(stakeholders, runner_count, rng)

    # Apply weather resistance delta globally (storm makes everyone defensive)
    w = weather_modifier or _DEFAULT_WEATHER
    r_delta = w.get("resistance_delta", 0.0)
    if r_delta:
        for s in scaled:
            s["initial_resistance"] = min(1.0, s.get("initial_resistance", 0.3) + r_delta)

    # Geo projection: real venues when available, PCA fallback otherwise
    geo_coords = _project_to_real_venues(scaled, venue_coords) if venue_coords else _project_to_geo(scaled)

    path, cost = _generate_best_pathway(
        scaled,
        resources,
        _PATHWAY_WEIGHTS,
        target,
        num_candidates=8,
        base_seed=resolved_seed,
        venue_types=venue_types,
        travel_matrix=travel_matrix,
        weather=weather_modifier,
    )
    geojson = _pathway_to_geojson(
        path,
        cost,
        scaled,
        geo_coords,
        scenario_type,
    )

    logger.info(
        "DCNS: generated consensus pathway for %s (stakeholders=%d, runner_count=%d, terrain=%s)",
        scenario_type,
        len(scaled),
        runner_count,
        "active" if any([venue_types, travel_matrix, weather_modifier]) else "baseline",
    )
    return {
        "geojson": geojson,
        "path": path,
        "cost": {"time": cost[0], "capital": cost[1], "morale": cost[2]},
        "consensus_score": _consensus_score(path, scaled),
    }
