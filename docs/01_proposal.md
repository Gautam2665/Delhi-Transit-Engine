# Project Proposal: Real-Time Bus Traffic Model & Journey Planner

## 1. Problem Statement
Delhi has thousands of buses acting as mobile speed sensors. The published timetable assumes static travel times, but real-world travel times vary wildly depending on time of day, day of week, bottlenecks, and live traffic.
The goal is to build an empirical travel-time model from historical GPS traces (October 2026) and real-time GTFS-RT feeds, powering an accurate journey planning engine.

## 2. System Architecture
1. **Offline Pipeline**:
   - Parse static GTFS (stops, routes, shapes, stop_times).
   - Segment generation via PostGIS linear referencing (stop-to-stop route geometries).
   - Streaming 7z telemetry ingestion to compute segment-level hourly and day-of-week speed distributions.
2. **Live Telemetry Worker**:
   - Ingest GTFS-RT `VehiclePositions.pb` feed periodically.
   - Match live bus speeds to active segments with exponential smoothing.
3. **Routing Engine & API**:
   - Multi-criteria router (Raptor / CSA or Graph-based) considering walk legs, transit headways, and traffic-adjusted segment durations.
   - REST API returning routes, transfers, and comparisons against timetable and off-peak hours.
4. **Web Frontend**:
   - Interactive MapLibre GL UI for route queries, transit paths, and live bus tracking.
