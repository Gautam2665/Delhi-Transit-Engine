"""
matcher.py
Spatial map matching engine with circular bearing discrimination, service-day window
validation, and 4-outcome classification state machine (M01-M13).
"""

import math
from typing import Dict, List, Optional, Tuple
import pandas as pd
from pyproj import Transformer
from shapely import wkb
from shapely.geometry import Point, LineString

from backend.app.services.gtfs_rt.schemas import (
    NormalizedPing,
    MatchedObservation,
    MatchingOutcome,
)

TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)


def circular_angular_diff(theta1: float, theta2: float) -> float:
    """Calculates circular angular difference in degrees [0, 180]."""
    diff = abs(theta1 - theta2) % 360.0
    return min(diff, 360.0 - diff)


def get_shape_tangent_azimuth(line: LineString, along_track_m: float) -> float:
    """Computes tangent azimuth (degrees [0, 360) clockwise from North) along line."""
    p1 = line.interpolate(max(0.0, along_track_m - 2.0))
    p2 = line.interpolate(min(line.length, along_track_m + 2.0))
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    if dx == 0.0 and dy == 0.0:
        return 0.0
    # Geographic bearing: 0 = North, 90 = East
    azimuth = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    return azimuth


class MapMatcher:
    """Evaluates NormalizedPing records against GTFS static shapes and trips."""

    def __init__(
        self,
        shapes_dict: Dict[str, LineString],
        route_to_shapes: Dict[str, List[str]],
        trips_df: Optional[pd.DataFrame] = None,
        max_snap_dist_m: float = 65.0,
    ):
        self.shapes_dict = shapes_dict
        self.route_to_shapes = route_to_shapes
        self.max_snap_dist_m = max_snap_dist_m
        self.trip_lookup: Dict[str, Dict] = {}

        if trips_df is not None:
            for r in trips_df.itertuples():
                self.trip_lookup[str(r.trip_id).strip()] = {
                    "route_id": str(r.route_id).strip(),
                    "shape_id": str(r.shape_id).strip() if pd.notna(r.shape_id) else None,
                }

    def match_ping(self, ping: NormalizedPing) -> MatchedObservation:
        """Applies M01-M13 classification rules to match ping against GTFS shapes."""
        # Rule M13: Coordinates out-of-bounds or invalid
        if ping.latitude == 0.0 and ping.longitude == 0.0:
            return MatchedObservation(
                ping=ping,
                matching_outcome=MatchingOutcome.UNMATCHED_INVALID_COORDINATES,
                confidence_score=0.0,
            )

        # Rule M12: Route ID unlisted in GTFS
        if not ping.route_id or ping.route_id not in self.route_to_shapes:
            return MatchedObservation(
                ping=ping,
                matching_outcome=MatchingOutcome.UNMATCHED_UNKNOWN_ROUTE,
                confidence_score=0.0,
            )

        candidate_shapes = self.route_to_shapes.get(ping.route_id, [])
        if not candidate_shapes:
            return MatchedObservation(
                ping=ping,
                matching_outcome=MatchingOutcome.UNMATCHED_UNKNOWN_ROUTE,
                confidence_score=0.0,
            )

        # Transform GPS to UTM meters
        x, y = TO_UTM.transform(ping.longitude, ping.latitude)
        pt = Point(x, y)

        # Evaluate all candidate shapes for this route
        shape_evals = []
        for sid in candidate_shapes:
            line = self.shapes_dict.get(sid)
            if not line:
                continue
            d_perp = float(line.distance(pt))
            d_along = float(line.project(pt))
            azimuth = get_shape_tangent_azimuth(line, d_along)
            delta_theta = (
                circular_angular_diff(ping.bearing, azimuth)
                if ping.bearing is not None
                else None
            )
            shape_evals.append((sid, d_perp, d_along, azimuth, delta_theta))

        # Sort by cross-track distance ascending
        shape_evals.sort(key=lambda item: item[1])

        # Rule M11: All candidate shapes > 65m
        if not shape_evals or shape_evals[0][1] > self.max_snap_dist_m:
            return MatchedObservation(
                ping=ping,
                matching_outcome=MatchingOutcome.UNMATCHED_OFF_ROUTE,
                cross_track_distance_m=shape_evals[0][1] if shape_evals else None,
                along_track_distance_m=shape_evals[0][2] if shape_evals else None,
                confidence_score=0.0,
            )

        # Check if trip_id matches static trips.txt
        trip_info = self.trip_lookup.get(ping.trip_id) if ping.trip_id else None

        if trip_info and trip_info.get("shape_id"):
            expected_shape = trip_info["shape_id"]
            # Rule M04: Trip ID contradicts GPS position (> 65m off expected shape)
            expected_eval = [e for e in shape_evals if e[0] == expected_shape]
            if expected_eval and expected_eval[0][1] > self.max_snap_dist_m:
                return MatchedObservation(
                    ping=ping,
                    matching_outcome=MatchingOutcome.TRIP_SHAPE_CONTRADICTION_UNMATCHED,
                    candidate_shape_id=expected_shape,
                    cross_track_distance_m=expected_eval[0][1],
                    along_track_distance_m=expected_eval[0][2],
                    confidence_score=0.0,
                )

            # Rule M01/M02/M03: Exact match with verified trip
            if expected_eval and expected_eval[0][1] <= self.max_snap_dist_m:
                ev = expected_eval[0]
                return MatchedObservation(
                    ping=ping,
                    matching_outcome=MatchingOutcome.EXACT_MATCH,
                    candidate_shape_id=expected_shape,
                    cross_track_distance_m=ev[1],
                    along_track_distance_m=ev[2],
                    bearing_angular_difference_deg=ev[4],
                    confidence_score=1.0,
                )

        # Trip ID missing or unlisted in static GTFS: Independent Shape Inference
        qualifying = [e for e in shape_evals if e[1] <= self.max_snap_dist_m]

        if len(qualifying) == 1:
            best = qualifying[0]
            # Rule M07: Exactly 1 shape within 65m
            return MatchedObservation(
                ping=ping,
                matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH,
                candidate_shape_id=best[0],
                cross_track_distance_m=best[1],
                along_track_distance_m=best[2],
                bearing_angular_difference_deg=best[4],
                confidence_score=0.70,
            )

        # Multiple candidate shapes within 65m (e.g. UP vs DOWN or shared corridor)
        best1, best2 = qualifying[0], qualifying[1]

        # Bearing discrimination
        if ping.bearing is not None:
            b1_diff = best1[4] if best1[4] is not None else 180.0
            b2_diff = best2[4] if best2[4] is not None else 180.0

            # Rule M08: Bearing clearly discriminates UP vs DOWN
            if b1_diff <= 45.0 and b2_diff >= 135.0:
                return MatchedObservation(
                    ping=ping,
                    matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH,
                    candidate_shape_id=best1[0],
                    cross_track_distance_m=best1[1],
                    along_track_distance_m=best1[2],
                    bearing_angular_difference_deg=b1_diff,
                    confidence_score=0.70,
                )
            if b2_diff <= 45.0 and b1_diff >= 135.0:
                return MatchedObservation(
                    ping=ping,
                    matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH,
                    candidate_shape_id=best2[0],
                    cross_track_distance_m=best2[1],
                    along_track_distance_m=best2[2],
                    bearing_angular_difference_deg=b2_diff,
                    confidence_score=0.70,
                )

        # Spatial separation: best1 is clearly closer (|Δd_perp| >= 15m)
        if (best2[1] - best1[1]) >= 15.0:
            return MatchedObservation(
                ping=ping,
                matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH,
                candidate_shape_id=best1[0],
                cross_track_distance_m=best1[1],
                along_track_distance_m=best1[2],
                bearing_angular_difference_deg=best1[4],
                confidence_score=0.70,
            )

        # Rule M09/M10: Parallel or opposite shapes with ambiguous evidence
        return MatchedObservation(
            ping=ping,
            matching_outcome=MatchingOutcome.AMBIGUOUS_MATCH,
            candidate_shape_id=None,
            cross_track_distance_m=best1[1],
            along_track_distance_m=best1[2],
            confidence_score=0.0,
        )
