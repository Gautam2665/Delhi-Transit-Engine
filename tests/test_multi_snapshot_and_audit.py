"""
test_multi_snapshot_and_audit.py
Rigorous verification suite for:
1. Multi-snapshot sequential processing, vehicle continuity, boundary interpolation,
   traversal emission, positive reliability weight, and journey router edge cost changes.
2. Stale-feed zero-freshness handling (Nov 16 feed queried on Nov 20).
3. Raw archive manager storage, bounded spill buffering, explicit drop accounting,
   and post-restart persistence/recovery semantics.
4. Separation of geographically corrupt GPS from valid coordinates outside Delhi study area.
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.transit import gtfs_realtime_pb2
import pandas as pd
from shapely import wkb
from pyproj import Transformer

from backend.app.services.gtfs_rt.schemas import (
    MatchingOutcome,
    ProvenanceTier,
)
from backend.app.services.gtfs_rt.ingest import (
    GTFSRTIngestor,
    RawArchiveManager,
    DELHI_BBOX,
)
from backend.app.services.gtfs_rt.pipeline import GTFSRTPipeline
from backend.app.services.router import DelhiBusRouter, TransitEdge

TO_WGS84 = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)


class TestMultiSnapshotAndAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load real network tables
        cls.speed_df = pd.read_parquet("data/processed/speed_profile.parquet")
        cls.shapes_df = pd.read_parquet("data/processed/shapes_utm.parquet")
        cls.route_shapes_df = pd.read_parquet("data/processed/route_shapes.parquet")
        cls.segments_df = pd.read_parquet("data/processed/segments.parquet")
        cls.stops_df = pd.read_parquet("data/processed/stops_utm.parquet")
        cls.routes_df = pd.read_csv("data/gtfs_static/routes.txt", dtype={"route_id": str})

        # Target real shape and segment for multi-snapshot test
        cls.target_shape_id = "shp_3_1"
        cls.target_route_id = "4022"
        cls.target_seg_id = "shp_3_1:0"

        shape_row = cls.shapes_df[cls.shapes_df["shape_id"] == cls.target_shape_id].iloc[0]
        cls.line = wkb.loads(shape_row["wkb"])

        seg_row = cls.segments_df[cls.segments_df["segment_id"] == cls.target_seg_id].iloc[0]
        cls.seg_start_m = float(seg_row["start_m"])
        cls.seg_end_m = float(seg_row["end_m"])
        cls.seg_length_m = float(seg_row["length_m"])
        cls.from_stop_id = str(seg_row["from_stop_id"])
        cls.to_stop_id = str(seg_row["to_stop_id"])

        # P1: at 50m (inside segment)
        p1 = cls.line.interpolate(50.0)
        cls.lon1, cls.lat1 = TO_WGS84.transform(p1.x, p1.y)

        # P2: at 1402.5m (past exit boundary 1352.51m)
        p2 = cls.line.interpolate(min(cls.line.length - 1.0, cls.seg_end_m + 50.0))
        cls.lon2, cls.lat2 = TO_WGS84.transform(p2.x, p2.y)

    def _build_synthetic_feed(self, entity_id: str, vehicle_id: str, route_id: str,
                               lat: float, lon: float, timestamp_utc: float) -> bytes:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = int(timestamp_utc)
        e = feed.entity.add()
        e.id = entity_id
        v = e.vehicle
        v.vehicle.id = vehicle_id
        v.trip.route_id = route_id
        v.position.latitude = lat
        v.position.longitude = lon
        v.timestamp = int(timestamp_utc)
        return feed.SerializeToString()

    def test_01_multi_snapshot_lifecycle_and_edge_cost_change(self):
        """Demonstrates end-to-end multi-snapshot traversal, positive reliability weight, and routing cost change."""
        pipeline = GTFSRTPipeline(
            shapes_utm_df=self.shapes_df,
            route_shapes_df=self.route_shapes_df,
            segments_df=self.segments_df,
            speed_profile_df=self.speed_df,
        )

        t1 = 1795150000.0  # Nov 20, 2026, 10:00:00 UTC
        t2 = 1795150150.0  # Nov 20, 2026, 10:02:30 UTC (dt = 150s)

        # 1. Snapshot 1: Vehicle enters mid-segment
        raw1 = self._build_synthetic_feed("e1", "DL1PB9999", self.target_route_id, self.lat1, self.lon1, t1)
        obs1, trav1 = pipeline.process_feed_bytes(raw1, now_utc=t1)

        self.assertEqual(len(obs1), 1)
        self.assertEqual(obs1[0].matching_outcome, MatchingOutcome.VALIDATED_INFERRED_MATCH)
        self.assertEqual(len(trav1), 0, "Snapshot 1 alone must produce 0 completed traversals")
        self.assertEqual(len(pipeline.accumulator.active_buffers), 1, "Active buffer must be opened")

        # Check edge cost before Snapshot 2 (pure historical fallback)
        edge = TransitEdge(
            segment_id=self.target_seg_id,
            shape_id=self.target_shape_id,
            route_id=self.target_route_id,
            from_stop_id=self.from_stop_id,
            to_stop_id=self.to_stop_id,
            length_m=self.seg_length_m,
        )
        router = DelhiBusRouter(
            self.stops_df, self.routes_df, self.route_shapes_df, self.segments_df, self.speed_df, pipeline
        )

        cost_before, quiet_sec, conf_before = router._edge_cost(edge, arr_ts=t1)
        self.assertEqual(conf_before, 0.0, "Confidence must be 0.0 before any completed traversal")

        # 2. Snapshot 2: Vehicle crosses exit boundary
        raw2 = self._build_synthetic_feed("e1", "DL1PB9999", self.target_route_id, self.lat2, self.lon2, t2)
        obs2, trav2 = pipeline.process_feed_bytes(raw2, now_utc=t2)

        self.assertEqual(len(obs2), 1)
        self.assertEqual(obs2[0].matching_outcome, MatchingOutcome.VALIDATED_INFERRED_MATCH)
        self.assertEqual(len(trav2), 1, "Snapshot 2 must emit exactly 1 completed traversal")

        tr = trav2[0]
        self.assertEqual(tr.segment_id, self.target_seg_id)
        self.assertEqual(tr.vehicle_id, "DL1PB9999")
        self.assertAlmostEqual(tr.traversal_duration_sec, 144.45, delta=0.5)
        self.assertEqual(tr.provenance_tier, ProvenanceTier.INTERPOLATED_CROSSING)

        # 3. Query blended travel time shortly after exit (t_query = t2 + 10s)
        t_query = t2 + 10.0
        blended = pipeline.get_blended_travel_time(self.target_seg_id, is_weekend=0, hour=10, now_utc=t_query)

        self.assertFalse(blended.is_fallback)
        self.assertIsNotNone(blended.live_duration_sec)
        self.assertAlmostEqual(blended.live_duration_sec, 144.45, delta=0.5)

        # Exact reconciliation of multiplicative reliability factors:
        # phi_freshness (age=15.55s <= 600s) = 1.00
        # psi_support (N=1 indep vehicle)     = 0.40
        # m_match (VALIDATED_INFERRED_MATCH)  = 0.70
        # q_gps (on-track cross_track=0.0m)   = 1.00
        # p_prov (INTERPOLATED_CROSSING)      = 0.80
        # W_eff = 1.00 * 0.40 * 0.70 * 1.00 * 0.80 = 0.2240
        self.assertEqual(blended.freshness_factor, 1.00)
        self.assertEqual(blended.sample_support_factor, 0.40)
        self.assertEqual(blended.matching_factor, 0.70)
        self.assertEqual(blended.gps_quality_factor, 1.00)
        self.assertEqual(blended.provenance_factor, 0.80)
        self.assertEqual(blended.effective_live_weight, 0.2240)

        expected_blended = 0.2240 * blended.live_duration_sec + (1.0 - 0.2240) * blended.historical_duration_sec
        self.assertAlmostEqual(blended.blended_duration_sec, expected_blended, delta=0.01)

        # 4. Check edge cost after Snapshot 2
        cost_after, _, conf_after = router._edge_cost(edge, arr_ts=t_query)
        self.assertEqual(conf_after, 0.2240, "Router edge confidence must match effective live weight")
        self.assertNotEqual(cost_after, cost_before, "Edge cost must change when live observation is blended")
        self.assertAlmostEqual(cost_after, blended.blended_duration_sec, delta=0.01)

    def test_02_stale_feed_zero_freshness_enforcement(self):
        """Verifies that an observation from Nov 16 queried on Nov 20 has zero freshness and falls back 100%."""
        pipeline = GTFSRTPipeline(
            shapes_utm_df=self.shapes_df,
            route_shapes_df=self.route_shapes_df,
            segments_df=self.segments_df,
            speed_profile_df=self.speed_df,
        )

        t_snap1 = 1794803575.0  # Nov 16, 2026 10:02:55 IST
        t_snap2 = 1794803700.0  # Nov 16, 2026 10:05:00 IST (emits traversal)

        raw1 = self._build_synthetic_feed("e1", "DL1PB9999", self.target_route_id, self.lat1, self.lon1, t_snap1)
        pipeline.process_feed_bytes(raw1, now_utc=t_snap1)

        raw2 = self._build_synthetic_feed("e1", "DL1PB9999", self.target_route_id, self.lat2, self.lon2, t_snap2)
        _, emitted = pipeline.process_feed_bytes(raw2, now_utc=t_snap2)
        self.assertEqual(len(emitted), 1)

        # Query on Nov 20, 2026 (age > 80 hours >> 1,200s cutoff)
        t_nov20 = 1795150000.0
        blended = pipeline.get_blended_travel_time(self.target_seg_id, is_weekend=0, hour=10, now_utc=t_nov20)

        self.assertTrue(blended.is_fallback, "Stale observation must trigger fallback")
        self.assertEqual(blended.effective_live_weight, 0.0, "W_eff must be 0.0 for age > 1200s")
        self.assertEqual(blended.freshness_factor, 0.0, "Freshness factor must be 0.0")
        self.assertEqual(blended.blended_duration_sec, blended.historical_duration_sec)

    def test_03_archival_persistence_spill_and_loss_accounting(self):
        """Inspects RawArchiveManager: atomic disk writes, bounded memory spill, drop tracking, and restart semantics."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="test_archive_"))
        try:
            # 1. Normal healthy writing to disk
            mgr = RawArchiveManager(archive_dir=tmp_dir, max_buffer_bytes=1000)
            sample_data = b"GTFS_RT_SAMPLE_PAYLOAD_12345"
            t0 = 1795150000.0

            success = mgr.archive_payload(sample_data, now_utc=t0)
            self.assertTrue(success)
            self.assertEqual(mgr.successful_writes, 1)
            self.assertEqual(mgr.get_health_status().status, "HEALTHY")

            # Check that file exists on disk
            written_files = list(tmp_dir.glob("**/*.pb"))
            self.assertEqual(len(written_files), 1)
            self.assertEqual(written_files[0].read_bytes(), sample_data)

            # 2. Simulate disk failure (spill buffer behavior)
            # Point to an un-creatable path to force disk write exception
            mgr.archive_dir = Path("Z:\\InvalidDrive\\NonExistentPath\\Archive")
            large_chunk = b"X" * 400

            # Write chunk 1 to spill buffer
            mgr.archive_payload(large_chunk, now_utc=t0 + 10)
            self.assertEqual(mgr.failed_writes, 1)
            self.assertEqual(mgr.spill_bytes_used, 400)
            self.assertEqual(mgr.get_health_status().status, "DEGRADED_BUFFERING")

            # Write chunk 2 to spill buffer
            mgr.archive_payload(large_chunk, now_utc=t0 + 20)
            self.assertEqual(mgr.failed_writes, 2)
            self.assertEqual(mgr.spill_bytes_used, 800)

            # Write chunk 3 (exceeds 1000 bytes max_buffer_bytes -> forces drop of chunk 1)
            mgr.archive_payload(large_chunk, now_utc=t0 + 30)
            self.assertEqual(mgr.dropped_chunks, 1)
            self.assertEqual(mgr.dropped_bytes, 400)
            self.assertEqual(mgr.get_health_status().status, "PERMANENT_DATA_LOSS")
            self.assertIsNotNone(mgr.loss_start_utc)

            # 3. Process restart verification:
            # After restart, a new RawArchiveManager reading tmp_dir can see chunks written to disk
            mgr_after_restart = RawArchiveManager(archive_dir=tmp_dir)
            persisted_files = list(tmp_dir.glob("**/*.pb"))
            self.assertEqual(len(persisted_files), 1)
            self.assertEqual(persisted_files[0].read_bytes(), sample_data)

            # In-memory spill chunks that failed to write to disk do NOT survive restart
            self.assertEqual(mgr_after_restart.spill_bytes_used, 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_04_study_area_vs_corrupt_gps_classification(self):
        """Verifies separation of corrupt GPS sensor failure from valid NCR coordinates outside Delhi study area."""
        ingestor = GTFSRTIngestor()

        # Build feed with 3 distinct categories:
        # Entity 1: Valid inside Delhi (lat=28.65, lon=77.20)
        # Entity 2: Valid NCR outside Delhi BBox (lat=28.5364, lon=77.4204 in Noida)
        # Entity 3: Corrupt GPS (lat=0.0, lon=0.0)
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 1795150000

        # E1
        e1 = feed.entity.add()
        e1.id = "e1"
        e1.vehicle.vehicle.id = "DL1"
        e1.vehicle.trip.route_id = "101"
        e1.vehicle.position.latitude = 28.65
        e1.vehicle.position.longitude = 77.20

        # E2: Valid Noida NCR
        e2 = feed.entity.add()
        e2.id = "e2"
        e2.vehicle.vehicle.id = "DL2"
        e2.vehicle.trip.route_id = "102"
        e2.vehicle.position.latitude = 28.5364
        e2.vehicle.position.longitude = 77.4204

        # E3: Corrupt GPS (0, 0)
        e3 = feed.entity.add()
        e3.id = "e3"
        e3.vehicle.vehicle.id = "DL3"
        e3.vehicle.trip.route_id = "103"
        e3.vehicle.position.latitude = 0.0
        e3.vehicle.position.longitude = 0.0

        raw_bytes = feed.SerializeToString()
        valid, invalid_total, dups, corrupt, outside = ingestor.ingest_bytes(raw_bytes, now_utc=1795150000.0)

        self.assertEqual(len(valid), 1, "Only Delhi in-boundary ping is accepted into valid stream")
        self.assertEqual(corrupt, 1, "Exactly 1 ping must be classified as corrupt GPS")
        self.assertEqual(outside, 1, "Exactly 1 ping must be classified as valid NCR outside study boundary")
        self.assertEqual(invalid_total, 2, "invalid_total must equal corrupt + outside")

    def test_05_freshness_piecewise_decay_boundary_points(self):
        """Tests exact piecewise freshness decay across ages 599, 600, 900, 1199, 1200, and 1201 seconds."""
        from backend.app.services.gtfs_rt.blender import SpeedBlender
        from backend.app.services.gtfs_rt.schemas import LiveSegmentEstimate

        blender = SpeedBlender(historical_speed_lookup={("test_seg", 0, 10): 100.0})

        # Test point 1: age = 599.0s (<= 600s -> full freshness 1.0)
        est_599 = LiveSegmentEstimate(
            segment_id="test_seg", sample_traversals_count=1, independent_vehicles_count=1,
            newest_observation_age_sec=599.0, live_traversal_duration_sec=80.0, live_speed_mps=10.0,
            mean_matching_confidence=1.0, mean_provenance_weight=1.0
        )
        res_599 = blender.blend_travel_time("test_seg", 0, 10, est_599, now_utc=10000.0)
        self.assertEqual(res_599.freshness_factor, 1.0)
        self.assertFalse(res_599.is_fallback)

        # Test point 2: age = 600.0s (exact boundary -> full freshness 1.0)
        est_600 = LiveSegmentEstimate(
            segment_id="test_seg", sample_traversals_count=1, independent_vehicles_count=1,
            newest_observation_age_sec=600.0, live_traversal_duration_sec=80.0, live_speed_mps=10.0,
            mean_matching_confidence=1.0, mean_provenance_weight=1.0
        )
        res_600 = blender.blend_travel_time("test_seg", 0, 10, est_600, now_utc=10000.0)
        self.assertEqual(res_600.freshness_factor, 1.0)
        self.assertFalse(res_600.is_fallback)

        # Test point 3: age = 900.0s (midpoint linear decay -> (1200 - 900) / 600 = 0.50)
        est_900 = LiveSegmentEstimate(
            segment_id="test_seg", sample_traversals_count=1, independent_vehicles_count=1,
            newest_observation_age_sec=900.0, live_traversal_duration_sec=80.0, live_speed_mps=10.0,
            mean_matching_confidence=1.0, mean_provenance_weight=1.0
        )
        res_900 = blender.blend_travel_time("test_seg", 0, 10, est_900, now_utc=10000.0)
        self.assertAlmostEqual(res_900.freshness_factor, 0.50, delta=0.0001)
        self.assertFalse(res_900.is_fallback)

        # Test point 4: age = 1199.0s (tail of linear decay -> (1200 - 1199) / 600 = 1/600 ~ 0.001667)
        est_1199 = LiveSegmentEstimate(
            segment_id="test_seg", sample_traversals_count=1, independent_vehicles_count=1,
            newest_observation_age_sec=1199.0, live_traversal_duration_sec=80.0, live_speed_mps=10.0,
            mean_matching_confidence=1.0, mean_provenance_weight=1.0
        )
        res_1199 = blender.blend_travel_time("test_seg", 0, 10, est_1199, now_utc=10000.0)
        self.assertAlmostEqual(res_1199.freshness_factor, 1.0 / 600.0, delta=0.0001)

        # Test point 5: age = 1200.0s (exact cutoff boundary -> zero freshness 0.0, is_fallback=True)
        est_1200 = LiveSegmentEstimate(
            segment_id="test_seg", sample_traversals_count=1, independent_vehicles_count=1,
            newest_observation_age_sec=1200.0, live_traversal_duration_sec=80.0, live_speed_mps=10.0,
            mean_matching_confidence=1.0, mean_provenance_weight=1.0
        )
        res_1200 = blender.blend_travel_time("test_seg", 0, 10, est_1200, now_utc=10000.0)
        self.assertEqual(res_1200.freshness_factor, 0.0)
        self.assertEqual(res_1200.effective_live_weight, 0.0)
        self.assertTrue(res_1200.is_fallback)

        # Test point 6: age = 1201.0s (past cutoff boundary -> zero freshness 0.0, is_fallback=True)
        est_1201 = LiveSegmentEstimate(
            segment_id="test_seg", sample_traversals_count=1, independent_vehicles_count=1,
            newest_observation_age_sec=1201.0, live_traversal_duration_sec=80.0, live_speed_mps=10.0,
            mean_matching_confidence=1.0, mean_provenance_weight=1.0
        )
        res_1201 = blender.blend_travel_time("test_seg", 0, 10, est_1201, now_utc=10000.0)
        self.assertEqual(res_1201.freshness_factor, 0.0)
        self.assertEqual(res_1201.effective_live_weight, 0.0)
        self.assertTrue(res_1201.is_fallback)

    def test_06_anonymous_observations_cannot_inflate_independent_support(self):
        """Verifies that anonymous observations cannot inflate independent vehicle support beyond verified count."""
        from backend.app.services.gtfs_rt.blender import SpeedBlender
        from backend.app.services.gtfs_rt.schemas import SegmentTraversalRecord

        blender = SpeedBlender(historical_speed_lookup={})

        t_known = SegmentTraversalRecord(
            segment_id="s1", shape_id="shp1", seq=0, from_stop_id="A", to_stop_id="B", length_m=100.0,
            vehicle_id="DL1_VERIFIED", is_anonymous=False, matching_outcome=MatchingOutcome.EXACT_MATCH,
            entry_time_utc=1000.0, exit_time_utc=1050.0, traversal_duration_sec=50.0, implied_speed_mps=2.0,
            provenance_tier=ProvenanceTier.DIRECT_BOUNDARY_OBSERVATION, provenance_weight=1.0,
            bounding_ping_interval_sec=50.0, crossed_segments_count=1
        )
        t_anon1 = SegmentTraversalRecord(
            segment_id="s1", shape_id="shp1", seq=0, from_stop_id="A", to_stop_id="B", length_m=100.0,
            vehicle_id=None, is_anonymous=True, matching_outcome=MatchingOutcome.EXACT_MATCH,
            entry_time_utc=1000.0, exit_time_utc=1060.0, traversal_duration_sec=60.0, implied_speed_mps=1.67,
            provenance_tier=ProvenanceTier.DIRECT_BOUNDARY_OBSERVATION, provenance_weight=1.0,
            bounding_ping_interval_sec=60.0, crossed_segments_count=1
        )
        t_anon2 = SegmentTraversalRecord(
            segment_id="s1", shape_id="shp1", seq=0, from_stop_id="A", to_stop_id="B", length_m=100.0,
            vehicle_id=None, is_anonymous=True, matching_outcome=MatchingOutcome.EXACT_MATCH,
            entry_time_utc=1000.0, exit_time_utc=1070.0, traversal_duration_sec=70.0, implied_speed_mps=1.43,
            provenance_tier=ProvenanceTier.DIRECT_BOUNDARY_OBSERVATION, provenance_weight=1.0,
            bounding_ping_interval_sec=70.0, crossed_segments_count=1
        )

        # 1 known + 2 anon -> independent_vehicles_count must remain 1
        est = blender.estimate_live_segment("s1", [t_known, t_anon1, t_anon2], now_utc=1100.0)
        self.assertIsNotNone(est)
        self.assertEqual(est.independent_vehicles_count, 1, "Anonymous pings must not increment verified vehicle count")
        self.assertEqual(est.sample_traversals_count, 2, "1 known + 1 pooled anonymous representative = 2 samples")

        # 0 known + 3 anon -> independent_vehicles_count must be 1 at most (capped)
        est_anon_only = blender.estimate_live_segment("s1", [t_anon1, t_anon2], now_utc=1100.0)
        self.assertIsNotNone(est_anon_only)
        self.assertEqual(est_anon_only.independent_vehicles_count, 1, "Anonymous-only observations must cap at 1")
        self.assertEqual(est_anon_only.sample_traversals_count, 1, "Pooled anonymous traversals produce 1 sample")


if __name__ == "__main__":
    unittest.main()
