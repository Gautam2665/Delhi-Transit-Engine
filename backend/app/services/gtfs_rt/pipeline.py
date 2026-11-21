"""
pipeline.py
Unified GTFS-RT Streaming Pipeline orchestrating ingestion, matching, kinematics,
accumulation, robust blending, and closed-cohort health metric collection.
"""

import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from shapely import wkb

from backend.app.services.gtfs_rt.schemas import (
    NormalizedPing,
    MatchedObservation,
    SegmentTraversalRecord,
    BlendedSegmentTravelTime,
    TelemetryHealthMetrics,
    MatchingOutcome,
    KinematicState,
)
from backend.app.services.gtfs_rt.ingest import GTFSRTIngestor, RawArchiveManager
from backend.app.services.gtfs_rt.matcher import MapMatcher
from backend.app.services.gtfs_rt.projector import KinematicProjector
from backend.app.services.gtfs_rt.accumulator import SegmentAccumulator
from backend.app.services.gtfs_rt.blender import SpeedBlender


class GTFSRTPipeline:
    """Full streaming GTFS-RT pipeline orchestrator."""

    def __init__(
        self,
        shapes_utm_df: pd.DataFrame,
        route_shapes_df: pd.DataFrame,
        segments_df: pd.DataFrame,
        speed_profile_df: pd.DataFrame,
        trips_df: Optional[pd.DataFrame] = None,
        archive_dir: Optional[Path] = None,
    ):
        # 1. Initialize Archive & Ingestor
        archive_mgr = (
            RawArchiveManager(archive_dir)
            if archive_dir
            else RawArchiveManager(Path("data/raw_rt"))
        )
        self.ingestor = GTFSRTIngestor(archive_manager=archive_mgr)

        # 2. Initialize Matcher
        shapes_dict = {r.shape_id: wkb.loads(r.wkb) for r in shapes_utm_df.itertuples()}
        route_to_shapes = (
            route_shapes_df.groupby("route_id")["shape_id"].apply(list).to_dict()
        )
        self.matcher = MapMatcher(
            shapes_dict=shapes_dict,
            route_to_shapes=route_to_shapes,
            trips_df=trips_df,
            max_snap_dist_m=65.0,
        )

        # 3. Initialize Accumulator
        segs_by_shape = defaultdict(list)
        for r in segments_df.itertuples():
            segs_by_shape[r.shape_id].append(
                {
                    "segment_id": r.segment_id,
                    "shape_id": r.shape_id,
                    "seq": int(r.seq),
                    "from_stop_id": str(r.from_stop_id),
                    "to_stop_id": str(r.to_stop_id),
                    "start_m": float(r.start_m),
                    "end_m": float(r.end_m),
                    "length_m": float(r.length_m),
                }
            )
        for sid in segs_by_shape:
            segs_by_shape[sid].sort(key=lambda s: s["seq"])
        self.accumulator = SegmentAccumulator(segments_by_shape=segs_by_shape)

        # 4. Initialize Blender
        hist_lookup = {
            (r.segment_id, int(r.is_weekend), int(r.hour)): float(r.hist_sec)
            for r in speed_profile_df.itertuples()
        }
        self.blender = SpeedBlender(historical_speed_lookup=hist_lookup)

        # 5. Tracking State Buffers
        self.prev_pings_by_vehicle: Dict[str, MatchedObservation] = {}
        self.traversals_by_segment: Dict[str, List[SegmentTraversalRecord]] = defaultdict(list)
        self.live_vehicles: Dict[str, dict] = {}
        self.active_route_vehicles: Dict[str, set] = defaultdict(set)
        self.last_poll_ts: float = 0.0
        self.poll_status: str = "uninitialized"

        # 6. Observability Accounting Counters (Cohort Grain)
        self.total_raw_entities = 0
        self.total_corrupt_gps = 0
        self.total_outside_study_area = 0
        self.total_invalid_coords = 0
        self.total_sub_sec_duplicates = 0
        self.matching_counts: Dict[str, int] = defaultdict(int)
        self.kinematic_counts: Dict[str, int] = defaultdict(int)

    def process_feed_bytes(
        self, raw_bytes: bytes, now_utc: Optional[float] = None
    ) -> Tuple[List[MatchedObservation], List[SegmentTraversalRecord]]:
        """Processes one feed snapshot end-to-end."""
        if now_utc is None:
            now_utc = time.time()

        # Step 1: Ingestion & Normalization
        ingest_res = self.ingestor.ingest_bytes(raw_bytes, now_utc=now_utc)
        pings = ingest_res[0]
        invalid_coords = ingest_res[1]
        sub_sec_dups = ingest_res[2]
        corrupt_coords = ingest_res[3] if len(ingest_res) > 3 else 0
        outside_area = ingest_res[4] if len(ingest_res) > 4 else invalid_coords

        self.total_raw_entities += len(pings) + invalid_coords + sub_sec_dups
        self.total_invalid_coords += invalid_coords
        self.total_corrupt_gps += corrupt_coords
        self.total_outside_study_area += outside_area
        self.total_sub_sec_duplicates += sub_sec_dups

        matched_obs_list: List[MatchedObservation] = []
        emitted_traversals: List[SegmentTraversalRecord] = []

        # Step 2: Map-Matching & Kinematics
        for ping in pings:
            matched_obs = self.matcher.match_ping(ping)
            matched_obs_list.append(matched_obs)
            self.matching_counts[matched_obs.matching_outcome.value] += 1

            v_key = ping.vehicle_id or f"anon_{ping.feed_entity_id}"
            prev_obs = self.prev_pings_by_vehicle.get(v_key)

            # Kinematic Classification
            kin_state, _, _, _ = KinematicProjector.classify_transition(
                prev_obs=prev_obs,
                curr_obs=matched_obs,
            )
            self.kinematic_counts[kin_state.value] += 1

            # Step 3: Segment Traversal Accumulation
            new_traversals = self.accumulator.process_vehicle_ping(
                vehicle_key=v_key,
                prev_obs=prev_obs,
                curr_obs=matched_obs,
            )
            for tr in new_traversals:
                emitted_traversals.append(tr)
                self.traversals_by_segment[tr.segment_id].append(tr)

            # Update vehicle tracking buffer
            self.prev_pings_by_vehicle[v_key] = matched_obs

            # Update live vehicles map and active fleet count
            self.live_vehicles[v_key] = {
                "vehicle_id": v_key,
                "route_id": ping.route_id or "",
                "lat": round(ping.latitude, 6),
                "lng": round(ping.longitude, 6),
                "timestamp": int(ping.timestamp_utc or ping.ingestion_timestamp_utc),
                "matching_outcome": matched_obs.matching_outcome.value,
                "shape_id": matched_obs.candidate_shape_id,
                "dist_to_shape_m": (
                    round(matched_obs.cross_track_distance_m, 2)
                    if matched_obs.cross_track_distance_m is not None
                    else None
                ),
                "progress_m": (
                    round(matched_obs.along_track_distance_m, 2)
                    if matched_obs.along_track_distance_m is not None
                    else None
                ),
            }
            if ping.route_id:
                self.active_route_vehicles[ping.route_id].add(v_key)

        self.last_poll_ts = now_utc
        self.poll_status = f"ok ({len(self.live_vehicles)} vehicles)"
        return matched_obs_list, emitted_traversals

    def estimate_wait_sec(self, route_id: str, is_weekend: int, hour: int) -> float:
        """Estimates expected half-headway wait time using active live fleet count with time-of-day fallback."""
        active_buses = len(self.active_route_vehicles.get(route_id, ()))
        if active_buses >= 2:
            est_wait_min = max(2.0, min(18.0, (75.0 / active_buses) * 0.5))
            return float(est_wait_min * 60.0)
        if 7 <= hour <= 10 or 17 <= hour <= 20:
            return 360.0 if not is_weekend else 480.0
        if 22 <= hour or hour <= 5:
            return 900.0
        return 480.0

    def get_live_multiplier(
        self, from_stop_id: str, to_stop_id: str, query_ts: float
    ) -> Tuple[float, float]:
        """Backward compatibility helper for legacy corridor ratio queries."""
        return 1.0, 0.0

    def get_coverage_stats(
        self, total_canonical_segments: int = 106160, now_utc: Optional[float] = None
    ) -> dict:
        """Returns coverage statistics of fresh segment traversals within the 1200s window."""
        if now_utc is None:
            now_utc = time.time()

        fresh_count = 0
        for sid, tr_list in self.traversals_by_segment.items():
            if any((now_utc - tr.exit_time_utc) <= 1200.0 for tr in tr_list):
                fresh_count += 1

        return {
            "total_canonical_segments": total_canonical_segments,
            "fresh_segments_count": fresh_count,
            "active_buffers_count": len(self.accumulator.active_buffers),
            "completed_traversals_count": len(self.accumulator.completed_traversals),
            "coverage_percentage": round((fresh_count / total_canonical_segments) * 100.0, 4),
            "observation_window_sec": 1200.0,
        }

    def get_blended_travel_time(
        self,
        segment_id: str,
        is_weekend: int,
        hour: int,
        now_utc: Optional[float] = None,
    ) -> BlendedSegmentTravelTime:
        """Queries the live speed blender for a specific canonical segment."""
        if now_utc is None:
            now_utc = time.time()

        traversals = self.traversals_by_segment.get(segment_id, [])
        live_est = self.blender.estimate_live_segment(
            segment_id=segment_id,
            traversals=traversals,
            now_utc=now_utc,
            window_sec=1200.0,
        )
        return self.blender.blend_travel_time(
            segment_id=segment_id,
            is_weekend=is_weekend,
            hour=hour,
            live_est=live_est,
            now_utc=now_utc,
        )

    def get_health_metrics(self) -> TelemetryHealthMetrics:
        """Emits reconciled observability metrics with exact closed-cohort denominators."""
        n_valid_dedup = sum(self.matching_counts.values())
        matching_pcts = {}
        for outcome_name, count in self.matching_counts.items():
            matching_pcts[outcome_name] = (
                round((count / n_valid_dedup) * 100.0, 2) if n_valid_dedup > 0 else 0.0
            )

        arch_health = (
            self.ingestor.archive_manager.get_health_status()
            if self.ingestor.archive_manager
            else None
        )

        return TelemetryHealthMetrics(
            collector_timestamp_utc=datetime.now(timezone.utc).isoformat(),
            raw_entities_received=self.total_raw_entities,
            corrupt_gps_coordinates_count=self.total_corrupt_gps,
            outside_study_area_count=self.total_outside_study_area,
            invalid_coordinates_count=self.total_invalid_coords,
            valid_dedup_pings_count=n_valid_dedup,
            matching_counts=dict(self.matching_counts),
            matching_percentages=matching_pcts,
            kinematic_counts=dict(self.kinematic_counts),
            active_buffer_count=len(self.accumulator.active_buffers),
            completed_traversals_count=len(self.accumulator.completed_traversals),
            archive_health=arch_health,
        )
