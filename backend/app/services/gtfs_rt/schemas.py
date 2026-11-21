"""
schemas.py
Pydantic schemas and enums for GTFS-RT streaming ingestion, matching, kinematics,
segment accumulation, and live traffic estimation.
"""

from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class VehicleStopStatus(str, Enum):
    INCOMING_AT = "INCOMING_AT"
    STOPPED_AT = "STOPPED_AT"
    IN_TRANSIT_TO = "IN_TRANSIT_TO"
    UNKNOWN = "UNKNOWN"


class MatchingOutcome(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    VALIDATED_INFERRED_MATCH = "VALIDATED_INFERRED_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    TRIP_SHAPE_CONTRADICTION_UNMATCHED = "TRIP_SHAPE_CONTRADICTION_UNMATCHED"
    UNMATCHED_OFF_ROUTE = "UNMATCHED_OFF_ROUTE"
    UNMATCHED_UNKNOWN_ROUTE = "UNMATCHED_UNKNOWN_ROUTE"
    UNMATCHED_INVALID_COORDINATES = "UNMATCHED_INVALID_COORDINATES"


class KinematicState(str, Enum):
    TIMESTAMP_NON_MONOTONIC = "TIMESTAMP_NON_MONOTONIC"
    SUB_SECOND_DUPLICATE = "SUB_SECOND_DUPLICATE"
    HIGH_FREQ_VALID_PROGRESS = "HIGH_FREQ_VALID_PROGRESS"
    SUB_SECOND_SPEED_ANOMALY = "SUB_SECOND_SPEED_ANOMALY"
    REVERSE_DIRECTION_ANOMALY = "REVERSE_DIRECTION_ANOMALY"
    GPS_LOCAL_NOISE_JITTER = "GPS_LOCAL_NOISE_JITTER"
    INFERRED_TERMINAL_LAYOVER = "INFERRED_TERMINAL_LAYOVER"
    OBSERVED_STATIONARY_NEAR_STOP = "OBSERVED_STATIONARY_NEAR_STOP"
    OBSERVED_STATIONARY_MID_LINK = "OBSERVED_STATIONARY_MID_LINK"
    MEASURED_ULTRA_SLOW_CRAWL = "MEASURED_ULTRA_SLOW_CRAWL"
    VALID_SLOW_MOVEMENT = "VALID_SLOW_MOVEMENT"
    NORMAL_RUNNING = "NORMAL_RUNNING"
    SPEED_TOO_HIGH = "SPEED_TOO_HIGH"
    OFF_ROUTE_OUTSIDE_RADIUS = "OFF_ROUTE_OUTSIDE_RADIUS"
    PROJECTION_GEOMETRY_FAILURE = "PROJECTION_GEOMETRY_FAILURE"
    INVALID_GPS_COORDINATES = "INVALID_GPS_COORDINATES"


class ProvenanceTier(str, Enum):
    DIRECT_BOUNDARY_OBSERVATION = "DIRECT_BOUNDARY_OBSERVATION"
    INTERPOLATED_CROSSING = "INTERPOLATED_CROSSING"
    MULTI_SEGMENT_INTERPOLATION = "MULTI_SEGMENT_INTERPOLATION"


class NormalizedPing(BaseModel):
    feed_entity_id: str
    ingestion_timestamp_utc: float
    ingestion_datetime_ist: str

    vehicle_id: Optional[str] = None
    trip_id: Optional[str] = None
    route_id: Optional[str] = None
    start_date: Optional[str] = None
    start_time: Optional[str] = None

    latitude: float
    longitude: float
    bearing: Optional[float] = None
    speed_mps: Optional[float] = None

    timestamp_utc: Optional[float] = None
    timestamp_ist: Optional[str] = None

    current_stop_sequence: Optional[int] = None
    current_status: VehicleStopStatus = VehicleStopStatus.UNKNOWN


class MatchedObservation(BaseModel):
    ping: NormalizedPing
    matching_outcome: MatchingOutcome
    candidate_shape_id: Optional[str] = None
    cross_track_distance_m: Optional[float] = None
    along_track_distance_m: Optional[float] = None
    bearing_angular_difference_deg: Optional[float] = None
    confidence_score: float = 0.0


class SegmentTraversalRecord(BaseModel):
    segment_id: str
    shape_id: str
    seq: int
    from_stop_id: str
    to_stop_id: str
    length_m: float

    vehicle_id: Optional[str] = None
    is_anonymous: bool = False
    matching_outcome: MatchingOutcome

    entry_time_utc: float
    exit_time_utc: float
    traversal_duration_sec: float
    implied_speed_mps: float

    stationary_delay_sec: float = 0.0
    dwell_duration_sec: float = 0.0

    provenance_tier: ProvenanceTier
    provenance_weight: float
    bounding_ping_interval_sec: float
    crossed_segments_count: int


class LiveSegmentEstimate(BaseModel):
    segment_id: str
    sample_traversals_count: int
    independent_vehicles_count: int
    newest_observation_age_sec: float
    live_traversal_duration_sec: float
    live_speed_mps: float
    observation_provenance_weights: List[float] = []
    mean_matching_confidence: float = 0.85
    mean_provenance_weight: float = 0.80


class BlendedSegmentTravelTime(BaseModel):
    segment_id: str
    blended_duration_sec: float
    historical_duration_sec: float
    live_duration_sec: Optional[float] = None
    effective_live_weight: float
    freshness_factor: float
    sample_support_factor: float
    provenance_factor: float
    matching_factor: float
    gps_quality_factor: float
    is_fallback: bool


class ArchiveHealthStatus(BaseModel):
    status: str
    successful_chunks_written: int
    failed_chunk_writes: int
    spill_buffer_used_bytes: int
    spill_buffer_capacity_bytes: int
    dropped_chunks_count: int
    dropped_bytes_count: int
    archive_loss_interval: Optional[Dict[str, str]] = None


class TelemetryHealthMetrics(BaseModel):
    collector_timestamp_utc: str
    raw_entities_received: int
    corrupt_gps_coordinates_count: int = 0
    outside_study_area_count: int = 0
    invalid_coordinates_count: int = 0
    valid_dedup_pings_count: int
    matching_counts: Dict[str, int]
    matching_percentages: Dict[str, float]
    kinematic_counts: Dict[str, int]
    active_buffer_count: int
    completed_traversals_count: int
    archive_health: Optional[ArchiveHealthStatus] = None
