"""
ingest.py
Raw Protobuf feed ingestion, bounded archival with degraded health reporting,
and timestamp normalization into NormalizedPing data structures.
"""

import time
import math
import hashlib
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from google.transit import gtfs_realtime_pb2

from backend.app.services.gtfs_rt.schemas import (
    NormalizedPing,
    VehicleStopStatus,
    ArchiveHealthStatus,
)

IST = timezone(timedelta(hours=5, minutes=30))
DELHI_BBOX = {"lat_min": 28.3, "lat_max": 28.9, "lon_min": 76.8, "lon_max": 77.4}

STATUS_MAP = {
    0: VehicleStopStatus.INCOMING_AT,
    1: VehicleStopStatus.STOPPED_AT,
    2: VehicleStopStatus.IN_TRANSIT_TO,
}


class RawArchiveManager:
    """Manages raw protobuf payload persistence with bounded volatile spill buffering."""

    def __init__(self, archive_dir: Path, max_buffer_bytes: int = 256 * 1024 * 1024, max_buffer_age_sec: float = 900.0):
        self.archive_dir = archive_dir
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.max_buffer_bytes = max_buffer_bytes
        self.max_buffer_age_sec = max_buffer_age_sec

        self.successful_writes = 0
        self.failed_writes = 0
        self.dropped_chunks = 0
        self.dropped_bytes = 0
        self.loss_start_utc: Optional[str] = None
        self.loss_end_utc: Optional[str] = None

        # Volatile in-memory spill buffer: deque of (timestamp_utc, sha256, payload_bytes)
        self.spill_buffer: deque = deque()
        self.spill_bytes_used = 0

    def archive_payload(self, raw_bytes: bytes, now_utc: float) -> bool:
        """Attempts writing chunk to disk. Spills to bounded memory buffer upon failure."""
        h = hashlib.sha256(raw_bytes).hexdigest()[:16]
        dt = datetime.fromtimestamp(now_utc, tz=timezone.utc)
        sub_dir = self.archive_dir / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
        target_file = sub_dir / f"chunk_{int(now_utc)}_{h}.pb"

        try:
            sub_dir.mkdir(parents=True, exist_ok=True)
            # Atomic write via temporary file
            tmp_file = sub_dir / f".tmp_{int(now_utc)}_{h}"
            tmp_file.write_bytes(raw_bytes)
            tmp_file.replace(target_file)
            self.successful_writes += 1

            # If disk write succeeded, flush any pending recoverable spill buffer items
            if self.spill_buffer:
                self._flush_spill_buffer()
            return True

        except Exception:
            self.failed_writes += 1
            payload_len = len(raw_bytes)

            # Check bounded capacity
            while (self.spill_bytes_used + payload_len > self.max_buffer_bytes) and self.spill_buffer:
                dropped = self.spill_buffer.popleft()
                dropped_len = len(dropped[2])
                self.spill_bytes_used -= dropped_len
                self.dropped_chunks += 1
                self.dropped_bytes += dropped_len
                now_iso = datetime.now(timezone.utc).isoformat()
                if not self.loss_start_utc:
                    self.loss_start_utc = now_iso
                self.loss_end_utc = now_iso

            self.spill_buffer.append((now_utc, h, raw_bytes))
            self.spill_bytes_used += payload_len
            return False

    def _flush_spill_buffer(self):
        """Flushes in-memory spill chunks to disk in strict FIFO order."""
        while self.spill_buffer:
            ts_utc, h, data = self.spill_buffer[0]
            dt = datetime.fromtimestamp(ts_utc, tz=timezone.utc)
            sub_dir = self.archive_dir / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
            target_file = sub_dir / f"chunk_{int(ts_utc)}_{h}.pb"
            try:
                sub_dir.mkdir(parents=True, exist_ok=True)
                tmp_file = sub_dir / f".tmp_{int(ts_utc)}_{h}"
                tmp_file.write_bytes(data)
                tmp_file.replace(target_file)
                self.spill_buffer.popleft()
                self.spill_bytes_used -= len(data)
                self.successful_writes += 1
            except Exception:
                break

    def get_health_status(self) -> ArchiveHealthStatus:
        if self.dropped_chunks > 0:
            status = "PERMANENT_DATA_LOSS"
        elif self.failed_writes > 0 or self.spill_bytes_used > 0:
            status = "DEGRADED_BUFFERING"
        else:
            status = "HEALTHY"

        loss_interval = None
        if self.loss_start_utc:
            loss_interval = {"start_loss_utc": self.loss_start_utc, "end_loss_utc": self.loss_end_utc or ""}

        return ArchiveHealthStatus(
            status=status,
            successful_chunks_written=self.successful_writes,
            failed_chunk_writes=self.failed_writes,
            spill_buffer_used_bytes=self.spill_bytes_used,
            spill_buffer_capacity_bytes=self.max_buffer_bytes,
            dropped_chunks_count=self.dropped_chunks,
            dropped_bytes_count=self.dropped_bytes,
            archive_loss_interval=loss_interval,
        )


class GTFSRTIngestor:
    """Parses FeedMessage payloads, validates coordinates, and produces NormalizedPing streams."""

    def __init__(self, archive_manager: Optional[RawArchiveManager] = None):
        self.archive_manager = archive_manager
        self.seen_dedup_keys: set = set()

    def ingest_bytes(self, raw_bytes: bytes, now_utc: Optional[float] = None) -> Tuple[List[NormalizedPing], int, int]:
        """
        Parses raw feed bytes.
        Returns:
            (valid_dedup_pings, invalid_coords_count, sub_sec_duplicates_count)
        """
        if now_utc is None:
            now_utc = time.time()

        if self.archive_manager:
            self.archive_manager.archive_payload(raw_bytes, now_utc)

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(raw_bytes)

        ingest_dt_ist = datetime.fromtimestamp(now_utc, tz=IST).isoformat()

        valid_pings: List[NormalizedPing] = []
        corrupt_gps_count = 0
        outside_study_area_count = 0
        sub_sec_duplicates_count = 0

        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue

            v = entity.vehicle
            lat = float(v.position.latitude)
            lon = float(v.position.longitude)

            # 1. Geographically corrupt GPS (sensor/serialization failure)
            if (
                math.isnan(lat)
                or math.isnan(lon)
                or abs(lat) > 90.0
                or abs(lon) > 180.0
                or (lat == 0.0 and lon == 0.0)
            ):
                corrupt_gps_count += 1
                continue

            # 2. Study-area spatial boundary (valid NCR / interstate coordinates outside model boundary)
            if not (DELHI_BBOX["lat_min"] <= lat <= DELHI_BBOX["lat_max"] and DELHI_BBOX["lon_min"] <= lon <= DELHI_BBOX["lon_max"]):
                outside_study_area_count += 1
                continue

            # Preserved identifiers
            vid = v.vehicle.id.strip() if v.HasField("vehicle") and v.vehicle.id else None
            tid = v.trip.trip_id.strip() if v.HasField("trip") and v.trip.trip_id else None
            rid = v.trip.route_id.strip() if v.HasField("trip") and v.trip.route_id else None
            s_date = v.trip.start_date.strip() if v.HasField("trip") and v.trip.start_date else None
            s_time = v.trip.start_time.strip() if v.HasField("trip") and v.trip.start_time else None

            # Timestamps: never fabricate vehicle timestamp
            ts_utc = float(v.timestamp) if v.timestamp else None
            ts_ist = datetime.fromtimestamp(ts_utc, tz=IST).isoformat() if ts_utc else None

            # Deduplication key for sub-second / duplicate polling
            dedup_key = (entity.id, vid or "", round(lat, 5), round(lon, 5), ts_utc or 0.0)
            if dedup_key in self.seen_dedup_keys:
                sub_sec_duplicates_count += 1
                continue
            self.seen_dedup_keys.add(dedup_key)

            # Bearing and speed
            bearing = float(v.position.bearing) if v.position.HasField("bearing") else None
            speed_mps = float(v.position.speed) if v.position.HasField("speed") else None

            # Operational status
            status = STATUS_MAP.get(v.current_status, VehicleStopStatus.UNKNOWN)
            stop_seq = int(v.current_stop_sequence) if v.current_stop_sequence else None

            ping = NormalizedPing(
                feed_entity_id=entity.id,
                ingestion_timestamp_utc=now_utc,
                ingestion_datetime_ist=ingest_dt_ist,
                vehicle_id=vid,
                trip_id=tid,
                route_id=rid,
                start_date=s_date,
                start_time=s_time,
                latitude=lat,
                longitude=lon,
                bearing=bearing,
                speed_mps=speed_mps,
                timestamp_utc=ts_utc,
                timestamp_ist=ts_ist,
                current_stop_sequence=stop_seq,
                current_status=status,
            )
            valid_pings.append(ping)

        invalid_coords_count = corrupt_gps_count + outside_study_area_count
        return (
            valid_pings,
            invalid_coords_count,
            sub_sec_duplicates_count,
            corrupt_gps_count,
            outside_study_area_count,
        )
