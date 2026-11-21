"""
projector.py
Exhaustive 16-state kinematic classifier and along-track progress projector (K01-K16).
"""

from typing import Tuple, Optional
from backend.app.services.gtfs_rt.schemas import KinematicState, MatchedObservation


class KinematicProjector:
    """Classifies movement across consecutive pings under exhaustive K01-K16 boundaries."""

    @staticmethod
    def classify_transition(
        prev_obs: Optional[MatchedObservation],
        curr_obs: MatchedObservation,
        dist_to_nearest_stop_m: float = 999.0,
        is_terminus_stop: bool = False,
        cumulative_stationary_sec: float = 0.0,
    ) -> Tuple[KinematicState, float, float, float]:
        """
        Classifies transition from prev_obs to curr_obs.
        Returns:
            (KinematicState, delta_d_m, delta_t_sec, velocity_mps)
        """
        # Rule K16: Invalid coordinates
        lat = curr_obs.ping.latitude
        lon = curr_obs.ping.longitude
        if (
            lat == 0.0
            and lon == 0.0
            or not (28.3 <= lat <= 28.9)
            or not (76.8 <= lon <= 77.4)
        ):
            return KinematicState.INVALID_GPS_COORDINATES, 0.0, 0.0, 0.0

        # Rule K14: Valid projection but off-route > 65m
        if (
            curr_obs.cross_track_distance_m is not None
            and curr_obs.cross_track_distance_m > 65.0
        ):
            return KinematicState.OFF_ROUTE_OUTSIDE_RADIUS, 0.0, 0.0, 0.0

        # Rule K15: Geometry projection failure
        if curr_obs.along_track_distance_m is None:
            return KinematicState.PROJECTION_GEOMETRY_FAILURE, 0.0, 0.0, 0.0

        # If no previous ping for this vehicle, return NORMAL_RUNNING as baseline single ping
        if prev_obs is None or prev_obs.along_track_distance_m is None:
            return KinematicState.NORMAL_RUNNING, 0.0, 0.0, curr_obs.ping.speed_mps or 0.0

        t1 = prev_obs.ping.timestamp_utc or prev_obs.ping.ingestion_timestamp_utc
        t2 = curr_obs.ping.timestamp_utc or curr_obs.ping.ingestion_timestamp_utc
        d1 = prev_obs.along_track_distance_m
        d2 = curr_obs.along_track_distance_m

        delta_t = t2 - t1
        delta_d = d2 - d1

        # Rule K01: Non-monotonic timestamp
        if delta_t <= 0.0:
            return KinematicState.TIMESTAMP_NON_MONOTONIC, delta_d, delta_t, 0.0

        v = delta_d / delta_t if delta_t > 0 else 0.0

        # Sub-second intervals: 0 < delta_t < 1.0s
        if delta_t < 1.0:
            if delta_d <= 0.5:
                return KinematicState.SUB_SECOND_DUPLICATE, delta_d, delta_t, v
            if v > 25.0:
                return KinematicState.SUB_SECOND_SPEED_ANOMALY, delta_d, delta_t, v
            return KinematicState.HIGH_FREQ_VALID_PROGRESS, delta_d, delta_t, v

        # delta_t >= 1.0s
        # Rule K05: Major reverse jump < -15m
        if delta_d < -15.0:
            return KinematicState.REVERSE_DIRECTION_ANOMALY, delta_d, delta_t, v

        # Rule K06: Small negative GPS jitter: [-15.0m, 0.0m)
        if -15.0 <= delta_d < 0.0:
            return KinematicState.GPS_LOCAL_NOISE_JITTER, delta_d, delta_t, 0.0

        # Stationary / Minimal progression: [0.0m, 5.0m]
        if 0.0 <= delta_d <= 5.0:
            # Rule K07: Inferred terminal layover (>300s at terminal stop)
            if (
                dist_to_nearest_stop_m <= 30.0
                and is_terminus_stop
                and cumulative_stationary_sec > 300.0
            ):
                return KinematicState.INFERRED_TERMINAL_LAYOVER, delta_d, delta_t, v

            # Rule K08: Stationary near stop (D_stop <= 30m)
            if dist_to_nearest_stop_m <= 30.0:
                return KinematicState.OBSERVED_STATIONARY_NEAR_STOP, delta_d, delta_t, v

            # Rule K09: Stationary mid-link (traffic queue / red light)
            return KinematicState.OBSERVED_STATIONARY_MID_LINK, delta_d, delta_t, v

        # Meaningful progression: delta_d > 5.0m
        if v > 25.0:
            return KinematicState.SPEED_TOO_HIGH, delta_d, delta_t, v

        if v < 0.1:
            return KinematicState.MEASURED_ULTRA_SLOW_CRAWL, delta_d, delta_t, v

        if v < 0.5:
            return KinematicState.VALID_SLOW_MOVEMENT, delta_d, delta_t, v

        # Rule K12: Normal running [0.5, 25.0 m/s]
        return KinematicState.NORMAL_RUNNING, delta_d, delta_t, v
