"""
test_gtfs_rt_pipeline.py
Unit and Integration Test Suite for GTFS-RT Streaming Telemetry Architecture.
Contains:
  Part 1: Synthetic Test Harness Simulations (Edge Cases, Boundaries, State Machines)
  Part 2: Real-Data Ingestion Test on actual VehiclePositions.pb feed
"""

import os
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from shapely.geometry import LineString

from backend.app.services.gtfs_rt.schemas import (
    NormalizedPing,
    MatchedObservation,
    MatchingOutcome,
    KinematicState,
    ProvenanceTier,
    SegmentTraversalRecord,
)
from backend.app.services.gtfs_rt.ingest import GTFSRTIngestor, RawArchiveManager
from backend.app.services.gtfs_rt.matcher import MapMatcher, circular_angular_diff
from backend.app.services.gtfs_rt.projector import KinematicProjector
from backend.app.services.gtfs_rt.accumulator import SegmentAccumulator
from backend.app.services.gtfs_rt.blender import SpeedBlender
from backend.app.services.gtfs_rt.pipeline import GTFSRTPipeline


class TestSyntheticKinematicsAndMatching(unittest.TestCase):
    """Part 1: Synthetic Test Harness Simulations."""

    def test_s01_sub_second_valid_progress(self):
        """S01: 0 < Δt < 1s, Δd > 0.5m, v <= 25 m/s -> HIGH_FREQ_VALID_PROGRESS."""
        p1 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.0)
        p2 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.5, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.5)
        m1 = MatchedObservation(ping=p1, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=10.0, cross_track_distance_m=5.0)
        m2 = MatchedObservation(ping=p2, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=11.0, cross_track_distance_m=5.0)
        state, dd, dt, v = KinematicProjector.classify_transition(m1, m2)
        self.assertEqual(state, KinematicState.HIGH_FREQ_VALID_PROGRESS)
        self.assertAlmostEqual(dd, 1.0)
        self.assertAlmostEqual(dt, 0.5)

    def test_s02_boundary_exact_0_5m(self):
        """S02: 0 < Δt < 1s, Δd = 0.5m -> SUB_SECOND_DUPLICATE boundary."""
        p1 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.0)
        p2 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.8, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.8)
        m1 = MatchedObservation(ping=p1, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=10.0, cross_track_distance_m=5.0)
        m2 = MatchedObservation(ping=p2, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=10.5, cross_track_distance_m=5.0)
        state, _, _, _ = KinematicProjector.classify_transition(m1, m2)
        self.assertEqual(state, KinematicState.SUB_SECOND_DUPLICATE)

    def test_s03_boundary_exact_5_0m(self):
        """S03: Δt >= 1s, Δd = 5.0m, D_stop = 80m -> OBSERVED_STATIONARY_MID_LINK."""
        p1 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.0)
        p2 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=120.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=120.0)
        m1 = MatchedObservation(ping=p1, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=100.0, cross_track_distance_m=5.0)
        m2 = MatchedObservation(ping=p2, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=105.0, cross_track_distance_m=5.0)
        state, _, _, _ = KinematicProjector.classify_transition(m1, m2, dist_to_nearest_stop_m=80.0)
        self.assertEqual(state, KinematicState.OBSERVED_STATIONARY_MID_LINK)

    def test_s04_boundary_exact_15_0m_reverse(self):
        """S04: Δd = -15.0m -> GPS_LOCAL_NOISE_JITTER boundary."""
        p1 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.0)
        p2 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=110.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=110.0)
        m1 = MatchedObservation(ping=p1, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=100.0, cross_track_distance_m=5.0)
        m2 = MatchedObservation(ping=p2, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=85.0, cross_track_distance_m=5.0)
        state, _, _, _ = KinematicProjector.classify_transition(m1, m2)
        self.assertEqual(state, KinematicState.GPS_LOCAL_NOISE_JITTER)

    def test_s05_boundary_exact_25_0_mps(self):
        """S05: Δd = 250m, Δt = 10s (v = 25.0 m/s) -> NORMAL_RUNNING boundary."""
        p1 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.0)
        p2 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=110.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=110.0)
        m1 = MatchedObservation(ping=p1, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=100.0, cross_track_distance_m=5.0)
        m2 = MatchedObservation(ping=p2, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=350.0, cross_track_distance_m=5.0)
        state, _, _, v = KinematicProjector.classify_transition(m1, m2)
        self.assertEqual(state, KinematicState.NORMAL_RUNNING)
        self.assertAlmostEqual(v, 25.0)

    def test_s07_boundary_exact_30_0m_stop(self):
        """S07: D_stop = 30.0m -> OBSERVED_STATIONARY_NEAR_STOP boundary."""
        p1 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=100.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=100.0)
        p2 = NormalizedPing(feed_entity_id="1", ingestion_timestamp_utc=120.0, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=120.0)
        m1 = MatchedObservation(ping=p1, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=100.0, cross_track_distance_m=5.0)
        m2 = MatchedObservation(ping=p2, matching_outcome=MatchingOutcome.VALIDATED_INFERRED_MATCH, along_track_distance_m=100.0, cross_track_distance_m=5.0)
        state, _, _, _ = KinematicProjector.classify_transition(m1, m2, dist_to_nearest_stop_m=30.0)
        self.assertEqual(state, KinematicState.OBSERVED_STATIONARY_NEAR_STOP)

    def test_s12_red_light_90s_worked_example(self):
        """S12: Numerical worked example (5-ping setup: 200m link, 90s red light delay)."""
        segs_by_shape = {
            "shp_test": [
                {"segment_id": "seg_k", "shape_id": "shp_test", "seq": 1, "from_stop_id": "A", "to_stop_id": "B", "start_m": 1000.0, "end_m": 1200.0, "length_m": 200.0}
            ]
        }
        accum = SegmentAccumulator(segments_by_shape=segs_by_shape)

        pings = [
            (100.0, 980.0),   # p0
            (110.0, 1040.0),  # p1: enters segment
            (140.0, 1041.0),  # p2: stopped at light (30s)
            (200.0, 1041.5),  # p3: stopped at light (60s -> total 90s)
            (220.0, 1210.0),  # p4: green light, exits segment
        ]

        prev = None
        emitted_all = []
        for t, d in pings:
            ping = NormalizedPing(feed_entity_id="bus_1", vehicle_id="bus_1", ingestion_timestamp_utc=t, ingestion_datetime_ist="", latitude=28.6, longitude=77.2, timestamp_utc=t)
            curr = MatchedObservation(ping=ping, matching_outcome=MatchingOutcome.EXACT_MATCH, candidate_shape_id="shp_test", along_track_distance_m=d, cross_track_distance_m=5.0)
            res = accum.process_vehicle_ping("bus_1", prev, curr)
            emitted_all.extend(res)
            prev = curr

        self.assertEqual(len(emitted_all), 1, "Should emit exactly 1 completed traversal")
        tr = emitted_all[0]
        self.assertAlmostEqual(tr.entry_time_utc, 103.3333333, places=2)
        self.assertAlmostEqual(tr.exit_time_utc, 218.813056, places=2)
        self.assertAlmostEqual(tr.traversal_duration_sec, 115.48, places=1)
        self.assertEqual(tr.stationary_delay_sec, 90.0, "Stationary delay must be exactly 90.0s")

    def test_s17_a14_explicit_weights_weighted_median(self):
        """S17: Test A14 with explicit weights (40s @ w=1.0, 42s @ w=1.0, 210s @ w=0.5)."""
        blender = SpeedBlender(historical_speed_lookup={})
        now = 1000.0
        # Create 3 traversals from 3 independent vehicles
        t1 = SegmentTraversalRecord(segment_id="s1", shape_id="sh1", seq=1, from_stop_id="A", to_stop_id="B", length_m=500.0, vehicle_id="v1", matching_outcome=MatchingOutcome.EXACT_MATCH, entry_time_utc=now-40.0, exit_time_utc=now, traversal_duration_sec=40.0, implied_speed_mps=12.5, provenance_tier=ProvenanceTier.DIRECT_BOUNDARY_OBSERVATION, provenance_weight=1.0, bounding_ping_interval_sec=20.0, crossed_segments_count=1)
        t2 = SegmentTraversalRecord(segment_id="s1", shape_id="sh1", seq=1, from_stop_id="A", to_stop_id="B", length_m=500.0, vehicle_id="v2", matching_outcome=MatchingOutcome.EXACT_MATCH, entry_time_utc=now-42.0, exit_time_utc=now, traversal_duration_sec=42.0, implied_speed_mps=11.9, provenance_tier=ProvenanceTier.DIRECT_BOUNDARY_OBSERVATION, provenance_weight=1.0, bounding_ping_interval_sec=20.0, crossed_segments_count=1)
        t3 = SegmentTraversalRecord(segment_id="s1", shape_id="sh1", seq=1, from_stop_id="A", to_stop_id="B", length_m=500.0, vehicle_id="v3", matching_outcome=MatchingOutcome.EXACT_MATCH, entry_time_utc=now-210.0, exit_time_utc=now, traversal_duration_sec=210.0, implied_speed_mps=2.38, provenance_tier=ProvenanceTier.MULTI_SEGMENT_INTERPOLATION, provenance_weight=0.5, bounding_ping_interval_sec=200.0, crossed_segments_count=3)

        est = blender.estimate_live_segment("s1", [t1, t2, t3], now_utc=now)
        self.assertIsNotNone(est)
        self.assertEqual(est.independent_vehicles_count, 3)
        self.assertAlmostEqual(est.live_traversal_duration_sec, 42.0, places=1)


class TestRealDataIngestionAndPipeline(unittest.TestCase):
    """Part 2: Real-Data Integration on downloaded VehiclePositions.pb."""

    @classmethod
    def setUpClass(cls):
        cls.pb_file = Path("VehiclePositions.pb")
        cls.assertTrue(cls.pb_file.exists(), "VehiclePositions.pb must exist in workspace root")

        # Load GTFS static tables
        cls.shapes_df = pd.read_parquet("data/processed/shapes_utm.parquet")
        cls.route_shapes_df = pd.read_parquet("data/processed/route_shapes.parquet")
        cls.segments_df = pd.read_parquet("data/processed/segments.parquet")
        cls.speed_df = pd.read_parquet("data/processed/speed_profile.parquet")
        cls.trips_df = pd.read_csv("data/gtfs_static/trips.txt", dtype=str)

        # Initialize pipeline
        cls.pipeline = GTFSRTPipeline(
            shapes_utm_df=cls.shapes_df,
            route_shapes_df=cls.route_shapes_df,
            segments_df=cls.segments_df,
            speed_profile_df=cls.speed_df,
            trips_df=cls.trips_df,
        )

    def test_real_feed_ingest_and_matching(self):
        """End-to-end processing of actual VehiclePositions.pb feed."""
        raw_bytes = self.pb_file.read_bytes()
        matched_obs, traversals = self.pipeline.process_feed_bytes(raw_bytes)

        # Verify entity count matches raw feed: 5,961 valid coordinates + 32 out-of-bbox = 5,993
        self.assertEqual(len(matched_obs), 5961)
        metrics = self.pipeline.get_health_metrics()
        self.assertEqual(metrics.raw_entities_received, 5993)
        self.assertEqual(metrics.invalid_coordinates_count, 32)
        self.assertEqual(metrics.valid_dedup_pings_count, 5961)

        # Print detailed report
        print("\n================ REAL FEED DIAGNOSTIC & MATCHING REPORT ================")
        print(f"Total Raw Entities Ingested: {metrics.raw_entities_received:,}")
        print(f"Valid Deduplicated Pings:    {metrics.valid_dedup_pings_count:,}")
        print("\nMatching Outcomes Breakdown:")
        for outcome, cnt in metrics.matching_counts.items():
            pct = metrics.matching_percentages.get(outcome, 0.0)
            print(f"  {outcome:<35}: {cnt:>5} ({pct:>6.2f}%)")

        print("\nKinematic Classification Breakdown:")
        for state, cnt in metrics.kinematic_counts.items():
            print(f"  {state:<35}: {cnt:>5}")

        print("\nAccumulator Buffers:")
        print(f"  Active Segment Buffers Opened: {metrics.active_buffer_count:,}")
        print(f"  Completed Traversals Emitted:  {metrics.completed_traversals_count:,}")
        print(f"  Archive Status:                {metrics.archive_health.status}")
        print("========================================================================\n")

        # Core assertions matching empirical feed breakdown
        self.assertEqual(metrics.matching_counts.get("VALIDATED_INFERRED_MATCH", 0), 3307)
        self.assertEqual(metrics.matching_counts.get("AMBIGUOUS_MATCH", 0), 1466)
        self.assertEqual(metrics.matching_counts.get("UNMATCHED_OFF_ROUTE", 0), 1188)
        self.assertEqual(sum(metrics.matching_counts.values()), 5961)
        self.assertEqual(metrics.active_buffer_count, 3091)
        self.assertEqual(metrics.completed_traversals_count, 0)  # Single snapshot produces 0 completed crossings
        self.assertEqual(metrics.archive_health.status, "HEALTHY")


if __name__ == "__main__":
    unittest.main()
