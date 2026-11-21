"""
telemetry.py
Router exposing GTFS-RT Telemetry Health, Fleet Snapshot, and Network Coverage endpoints.
"""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/telemetry", tags=["Telemetry"])


@router.get("/health")
def get_telemetry_health(request: Request):
    """Emits TelemetryHealthMetrics with exact closed-cohort denominators and matching breakdowns."""
    pipeline = getattr(request.app.state, "gtfs_rt_pipeline", None)
    if pipeline is None:
        raise HTTPException(
            status_code=503,
            detail="GTFS-RT pipeline is uninitialized.",
        )
    return pipeline.get_health_metrics()


@router.get("/snapshot")
def get_telemetry_snapshot(request: Request):
    """Returns summarized fleet state: active buffers, snapped count, off-route count, archive status."""
    pipeline = getattr(request.app.state, "gtfs_rt_pipeline", None)
    if pipeline is None:
        return {
            "status": "uninitialized",
            "total_vehicles": 0,
            "active_buffers": 0,
            "completed_traversals": 0,
            "matching_summary": {},
            "archive_status": None,
        }

    health_metrics = pipeline.get_health_metrics()
    return {
        "status": pipeline.poll_status,
        "total_vehicles": len(pipeline.live_vehicles),
        "last_poll_ts": pipeline.last_poll_ts,
        "active_buffers": len(pipeline.accumulator.active_buffers),
        "completed_traversals": len(pipeline.accumulator.completed_traversals),
        "matching_summary": dict(pipeline.matching_counts),
        "archive_status": (
            health_metrics.archive_health.model_dump()
            if health_metrics.archive_health
            else None
        ),
    }


@router.get("/coverage")
def get_telemetry_coverage(request: Request):
    """Returns canonical segment coverage statistics (N_seg_fresh / 106,160)."""
    pipeline = getattr(request.app.state, "gtfs_rt_pipeline", None)
    if pipeline is None:
        return {
            "total_canonical_segments": 106160,
            "fresh_segments_count": 0,
            "active_buffers_count": 0,
            "completed_traversals_count": 0,
            "coverage_percentage": 0.0,
            "observation_window_sec": 1200.0,
        }

    return pipeline.get_coverage_stats(total_canonical_segments=106160)
