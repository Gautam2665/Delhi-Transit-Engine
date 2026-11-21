"""
test_backend_api.py
Integration tests for FastAPI backend endpoints including Telemetry Health,
Fleet Snapshot, Coverage, Live Vehicles, and Journey Planning with Blended Router.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest
from fastapi.testclient import TestClient

from backend.app.main import app


class TestBackendAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Using context manager for TestClient executes lifespan (startup/shutdown)
        cls.client_ctx = TestClient(app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)

    def test_01_root_endpoint(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("telemetry_health", data)
        self.assertIn("telemetry_snapshot", data)
        self.assertIn("telemetry_coverage", data)

    def test_02_health_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "healthy")
        self.assertTrue(data["processed_artifacts"]["speed_profile"])

    def test_03_telemetry_health(self):
        resp = self.client.get("/api/telemetry/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # Exact accounting checks from the real VehiclePositions.pb snapshot
        self.assertEqual(data["raw_entities_received"], 5993)
        self.assertEqual(data["invalid_coordinates_count"], 32)
        self.assertEqual(data["valid_dedup_pings_count"], 5961)

        counts = data["matching_counts"]
        self.assertEqual(counts.get("VALIDATED_INFERRED_MATCH"), 3307)
        self.assertEqual(counts.get("AMBIGUOUS_MATCH"), 1466)
        self.assertEqual(counts.get("UNMATCHED_OFF_ROUTE"), 1188)
        self.assertEqual(
            counts.get("VALIDATED_INFERRED_MATCH") + counts.get("AMBIGUOUS_MATCH") + counts.get("UNMATCHED_OFF_ROUTE"),
            5961,
        )

        pcts = data["matching_percentages"]
        self.assertAlmostEqual(pcts["VALIDATED_INFERRED_MATCH"], 55.48, delta=0.05)
        self.assertAlmostEqual(pcts["AMBIGUOUS_MATCH"], 24.59, delta=0.05)
        self.assertAlmostEqual(pcts["UNMATCHED_OFF_ROUTE"], 19.93, delta=0.05)

        self.assertEqual(data["active_buffer_count"], 3091)
        self.assertEqual(data["completed_traversals_count"], 0)
        self.assertIsNotNone(data["archive_health"])
        self.assertEqual(data["archive_health"]["status"], "HEALTHY")

    def test_04_telemetry_snapshot(self):
        resp = self.client.get("/api/telemetry/snapshot")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data["total_vehicles"], 5961)
        self.assertEqual(data["active_buffers"], 3091)
        self.assertEqual(data["completed_traversals"], 0)
        self.assertTrue(data["status"].startswith("seeded from local archive"))

    def test_05_telemetry_coverage(self):
        resp = self.client.get("/api/telemetry/coverage")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data["total_canonical_segments"], 106160)
        self.assertEqual(data["active_buffers_count"], 3091)
        self.assertEqual(data["completed_traversals_count"], 0)
        self.assertEqual(data["fresh_segments_count"], 0)
        self.assertEqual(data["coverage_percentage"], 0.0)

    def test_06_vehicles_live(self):
        resp = self.client.get("/vehicles/live")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data["count"], 5961)
        self.assertLessEqual(len(data["vehicles"]), 500)  # default limit
        sample = data["vehicles"][0]
        self.assertIn("vehicle_id", sample)
        self.assertIn("lat", sample)
        self.assertIn("lng", sample)
        self.assertIn("matching_outcome", sample)

    def test_07_journeys_plan_with_blended_router(self):
        # Query a representative corridor: Kashmere Gate to Nehru Place
        resp = self.client.get("/journeys?from=Kashmere%20Gate&to=Nehru%20Place&k=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertIn("journeys", data)
        self.assertGreater(len(data["journeys"]), 0)
        first_opt = data["journeys"][0]
        self.assertIn("total_duration_min", first_opt)
        self.assertIn("legs", first_opt)
        self.assertGreater(len(first_opt["legs"]), 0)


if __name__ == "__main__":
    unittest.main()
