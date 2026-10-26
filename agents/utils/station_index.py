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

"""Utilities for extracting infrastructure indexes from routes."""

import logging

logger = logging.getLogger(__name__)


def extract_station_index(route_geojson: dict) -> dict:
    """Extract infrastructure mile markers from route GeoJSON features.

    Args:
        route_geojson: The GeoJSON FeatureCollection dictionary representing the route.

    Returns:
        A dictionary mapping infrastructure types to sorted lists of their mile markers.
        Example: {"water_stations": [3.1, 6.2, ...], "medical_tents": [...], "portable_toilets": [...]}
    """
    index = {"water_stations": [], "medical_tents": [], "portable_toilets": []}

    if not route_geojson or not isinstance(route_geojson, dict):
        return index

    features = route_geojson.get("features", [])
    if not features:
        return index

    for feature in features:
        props = feature.get("properties", {})
        if not props:
            continue

        ftype = props.get("type", "")
        mi = props.get("mi")

        if mi is None:
            continue

        try:
            mi_val = float(mi)
        except (ValueError, TypeError):
            continue

        if ftype == "water_station":
            index["water_stations"].append(mi_val)
        elif ftype == "medical_tent":
            index["medical_tents"].append(mi_val)
        elif ftype == "portable_toilet":
            index["portable_toilets"].append(mi_val)
        elif ftype:
            # L14: Warn on unknown infrastructure types so misconfigurations
            # are visible instead of silently lost.
            logger.debug("extract_station_index: unknown infrastructure type %r at mi=%.2f", ftype, mi_val)

    # Sort for binary search or linear progression
    for k in index:
        index[k].sort()

    return index
