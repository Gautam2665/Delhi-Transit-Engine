import asyncio
import os
import time
from typing import Optional
import httpx
import pandas as pd

from backend.app.services.gtfs_rt.pipeline import GTFSRTPipeline

GTFS_RT_URL = os.getenv(
    "GTFS_RT_URL",
    "https://delhi.transportstack.in/api/dataset/otd/get-file?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb",
)
GTFS_RT_API_KEY = os.getenv("GTFS_RT_API_KEY", "")


class GTFSRTPoller:
    """Asynchronous poller fetching live GTFS-RT Protobuf feeds and processing them via GTFSRTPipeline."""

    def __init__(
        self,
        pipeline: GTFSRTPipeline,
        poll_url: str = GTFS_RT_URL,
        api_key: str = GTFS_RT_API_KEY,
    ):
        self.pipeline = pipeline
        self.poll_url = poll_url
        self.api_key = api_key

    async def run_forever(self, interval_sec: int = 15):
        if not self.api_key:
            if not self.pipeline.poll_status.startswith("seeded"):
                self.pipeline.poll_status = "disabled_no_api_key"
            return

        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                try:
                    await self.poll_once(client)
                except Exception as exc:
                    self.pipeline.poll_status = f"error: {exc}"
                await asyncio.sleep(interval_sec)

    async def poll_once(self, client: httpx.AsyncClient):
        resp = await client.get(self.poll_url, headers={"x-api-key": self.api_key})
        resp.raise_for_status()
        self.pipeline.process_feed_bytes(resp.content)


class LiveTelemetryStore(GTFSRTPipeline):
    """Backwards-compatible wrapper subclassing GTFSRTPipeline."""

    def __init__(
        self,
        shapes_df: pd.DataFrame,
        route_shapes_df: pd.DataFrame,
        segments_df: pd.DataFrame,
        speed_df: pd.DataFrame,
        trips_df: Optional[pd.DataFrame] = None,
    ):
        super().__init__(
            shapes_utm_df=shapes_df,
            route_shapes_df=route_shapes_df,
            segments_df=segments_df,
            speed_profile_df=speed_df,
            trips_df=trips_df,
        )
        self.poller = GTFSRTPoller(self)

    async def run_forever(self, interval_sec: int = 15):
        await self.poller.run_forever(interval_sec=interval_sec)

    async def poll_once(self, client: httpx.AsyncClient):
        await self.poller.poll_once(client)
