import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
from shapely import wkb

from backend.app.config import settings
from backend.app.routers import health, stops, journeys, telemetry
from backend.app.services.gtfs_rt.pipeline import GTFSRTPipeline
from backend.app.services.gtfs_rt_poller import GTFSRTPoller
from backend.app.services.router import DelhiBusRouter

telemetry_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telemetry_task
    print("[*] Starting Delhi Transit Engine Backend...")

    speed_path = settings.PROCESSED_DIR / "speed_profile.parquet"
    shapes_path = settings.PROCESSED_DIR / "shapes_utm.parquet"
    route_shapes_path = settings.PROCESSED_DIR / "route_shapes.parquet"
    segs_path = settings.PROCESSED_DIR / "segments.parquet"
    stops_path = settings.PROCESSED_DIR / "stops_utm.parquet"
    routes_path = settings.GTFS_DIR / "routes.txt"

    required_files = [speed_path, shapes_path, route_shapes_path, segs_path, stops_path, routes_path]
    missing = [str(f.name) for f in required_files if not f.exists()]

    if missing:
        print(f"[!] Warning: Missing artifacts: {missing}. Router will initialize once files are built.")
        app.state.router = None
        app.state.telemetry = None
        app.state.gtfs_rt_pipeline = None
        app.state.segment_meta = {}
        app.state.shapes_utm = {}
    else:
        print("[*] Loading routing & speed tensors into memory...")
        shapes_df = pd.read_parquet(shapes_path)
        route_shapes_df = pd.read_parquet(route_shapes_path)
        segments_df = pd.read_parquet(segs_path)
        stops_df = pd.read_parquet(stops_path)
        speed_df = pd.read_parquet(speed_path)
        routes_df = pd.read_csv(routes_path, dtype={"route_id": str})

        # Store geometric lookups for /journeys/shape
        app.state.segment_meta = {
            r.segment_id: (r.shape_id, float(r.start_m), float(r.end_m))
            for r in segments_df.itertuples()
        }
        app.state.shapes_utm = {
            r.shape_id: wkb.loads(r.wkb)
            for r in shapes_df.itertuples()
        }

        print("[*] Initializing GTFSRTPipeline...")
        archive_dir = Path("data/raw_rt")
        pipeline = GTFSRTPipeline(
            shapes_utm_df=shapes_df,
            route_shapes_df=route_shapes_df,
            segments_df=segments_df,
            speed_profile_df=speed_df,
            trips_df=None,
            archive_dir=archive_dir,
        )

        # Seed pipeline with local snapshot if available
        seed_pb_path = Path("VehiclePositions.pb")
        if seed_pb_path.exists():
            print(f"[*] Seeding GTFSRTPipeline from local feed: {seed_pb_path} ({seed_pb_path.stat().st_size} bytes)...")
            try:
                with open(seed_pb_path, "rb") as f:
                    raw_bytes = f.read()
                pipeline.process_feed_bytes(raw_bytes)
                pipeline.poll_status = f"seeded from local archive ({len(pipeline.live_vehicles)} vehicles)"
                print(
                    f"[✓] Seeding complete: {len(pipeline.live_vehicles)} vehicles matched, "
                    f"{len(pipeline.accumulator.active_buffers)} active buffers opened."
                )
            except Exception as e:
                print(f"[!] Warning: Failed to seed pipeline from {seed_pb_path}: {e}")

        app.state.gtfs_rt_pipeline = pipeline
        app.state.telemetry = pipeline

        poller = GTFSRTPoller(pipeline=pipeline)
        telemetry_task = asyncio.create_task(poller.run_forever(interval_sec=15))

        print("[*] Initializing DelhiBusRouter graph & spatial indices...")
        router = DelhiBusRouter(stops_df, routes_df, route_shapes_df, segments_df, speed_df, pipeline)
        app.state.router = router
        print("[✓] Delhi Transit Engine routing engine is ONLINE and ready.")

    yield

    if telemetry_task:
        telemetry_task.cancel()
    print("[*] Shutting down Delhi Transit Engine Backend.")


app = FastAPI(
    title="Delhi Transit Engine API",
    description="Real-Time Bus Traffic Model and Multi-Modal Transit Journey Planner for Delhi (DTC / DIMTS)",
    version="0.1.0",
    lifespan=lifespan,
)

# Enable CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, tags=["System"])
app.include_router(stops.router, tags=["Stops"])
app.include_router(journeys.router, tags=["Journeys"])
app.include_router(telemetry.router)


@app.get("/")
def root():
    return {
        "message": "Welcome to Delhi Transit Engine API",
        "docs": "/docs",
        "health": "/health",
        "stops": "/stops",
        "journeys": "/journeys?from=Kashmere%20Gate&to=Nehru%20Place",
        "live_vehicles": "/vehicles/live",
        "telemetry_health": "/api/telemetry/health",
        "telemetry_snapshot": "/api/telemetry/snapshot",
        "telemetry_coverage": "/api/telemetry/coverage",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8000, reload=True)
