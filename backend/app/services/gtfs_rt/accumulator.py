"""
accumulator.py
Active Segment Accumulation Buffer. Tracks entry/exit crossing events, clips boundary
intervals, and captures stationary delays (red lights) exactly once.
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from backend.app.services.gtfs_rt.schemas import (
    MatchedObservation,
    SegmentTraversalRecord,
    ProvenanceTier,
    MatchingOutcome,
)


@dataclass
class ActiveSegmentBuffer:
    vehicle_key: str
    shape_id: str
    segment_id: str
    seq: int
    from_stop_id: str
    to_stop_id: str
    start_m: float
    end_m: float
    length_m: float

    entry_time_utc: float
    last_ping_time_utc: float
    last_along_track_m: float

    stationary_delay_sec: float = 0.0
    is_entry_interpolated: bool = True
    bounding_pings: List[MatchedObservation] = field(default_factory=list)


class SegmentAccumulator:
    """Manages active occupancy buffers and emits completed SegmentTraversalRecord items."""

    def __init__(self, segments_by_shape: Dict[str, list], max_ping_gap_sec: float = 300.0):
        self.segments_by_shape = segments_by_shape
        self.max_ping_gap_sec = max_ping_gap_sec
        # Active buffer key: (vehicle_key, shape_id) -> ActiveSegmentBuffer
        self.active_buffers: Dict[Tuple[str, str], ActiveSegmentBuffer] = {}
        self.completed_traversals: List[SegmentTraversalRecord] = []

    def process_vehicle_ping(
        self,
        vehicle_key: str,
        prev_obs: Optional[MatchedObservation],
        curr_obs: MatchedObservation,
    ) -> List[SegmentTraversalRecord]:
        """
        Processes a vehicle ping transition.
        Emits completed traversals when segment exit boundaries are crossed.
        """
        emitted: List[SegmentTraversalRecord] = []

        if curr_obs.matching_outcome not in (
            MatchingOutcome.EXACT_MATCH,
            MatchingOutcome.VALIDATED_INFERRED_MATCH,
        ):
            # If vehicle becomes unmatched/ambiguous, abort any active buffer cleanly
            self.active_buffers.pop((vehicle_key, curr_obs.candidate_shape_id or ""), None)
            return emitted

        shape_id = curr_obs.candidate_shape_id
        if not shape_id or shape_id not in self.segments_by_shape:
            return emitted

        segs = self.segments_by_shape[shape_id]
        curr_t = curr_obs.ping.timestamp_utc or curr_obs.ping.ingestion_timestamp_utc
        curr_d = curr_obs.along_track_distance_m

        if curr_d is None:
            return emitted

        buf_key = (vehicle_key, shape_id)
        active_buf = self.active_buffers.get(buf_key)

        # First observation for this vehicle on this shape
        if prev_obs is None or prev_obs.along_track_distance_m is None:
            # Find which segment curr_d falls in
            for seg in segs:
                if seg["start_m"] <= curr_d < seg["end_m"]:
                    # Bus first observed mid-segment: open buffer but flag entry estimated
                    self.active_buffers[buf_key] = ActiveSegmentBuffer(
                        vehicle_key=vehicle_key,
                        shape_id=shape_id,
                        segment_id=seg["segment_id"],
                        seq=seg["seq"],
                        from_stop_id=seg["from_stop_id"],
                        to_stop_id=seg["to_stop_id"],
                        start_m=seg["start_m"],
                        end_m=seg["end_m"],
                        length_m=seg["length_m"],
                        entry_time_utc=curr_t,
                        last_ping_time_utc=curr_t,
                        last_along_track_m=curr_d,
                        is_entry_interpolated=False,
                        bounding_pings=[curr_obs],
                    )
                    break
            return emitted

        prev_t = prev_obs.ping.timestamp_utc or prev_obs.ping.ingestion_timestamp_utc
        prev_d = prev_obs.along_track_distance_m
        delta_t = curr_t - prev_t
        delta_d = curr_d - prev_d

        # Check maximum ping continuity gap
        if delta_t > self.max_ping_gap_sec or delta_d < -15.0:
            # Abort active buffer due to blackout or reverse jump
            self.active_buffers.pop(buf_key, None)
            return emitted

        # Stationary interval: delta_d <= 5.0m
        if abs(delta_d) <= 5.0:
            if active_buf:
                active_buf.stationary_delay_sec += delta_t
                active_buf.last_ping_time_utc = curr_t
                active_buf.bounding_pings.append(curr_obs)
            return emitted

        # Forward movement: delta_d > 5.0m
        # Iterate over all segments that overlap [prev_d, curr_d]
        for seg in segs:
            s_start = seg["start_m"]
            s_end = seg["end_m"]

            # Check if this segment was entered in this transition
            entered = prev_d < s_start <= curr_d
            # Check if this segment was exited in this transition
            exited = prev_d < s_end <= curr_d
            # Check if segment is fully inside [prev_d, curr_d] (multi-segment span)
            enclosed = prev_d <= s_start and curr_d >= s_end

            # Calculate interpolated boundary times
            t_entry = (
                prev_t + delta_t * ((s_start - prev_d) / delta_d)
                if delta_d > 0 and entered
                else (active_buf.entry_time_utc if active_buf else prev_t)
            )
            t_exit = (
                prev_t + delta_t * ((s_end - prev_d) / delta_d)
                if delta_d > 0 and exited
                else curr_t
            )

            # Case A: Exit boundary crossed for active segment
            if active_buf and active_buf.segment_id == seg["segment_id"] and exited:
                dur = t_exit - active_buf.entry_time_utc
                if dur > 1.0:
                    speed = seg["length_m"] / dur
                    rec = SegmentTraversalRecord(
                        segment_id=seg["segment_id"],
                        shape_id=shape_id,
                        seq=seg["seq"],
                        from_stop_id=seg["from_stop_id"],
                        to_stop_id=seg["to_stop_id"],
                        length_m=seg["length_m"],
                        vehicle_id=curr_obs.ping.vehicle_id,
                        is_anonymous=curr_obs.ping.vehicle_id is None,
                        matching_outcome=curr_obs.matching_outcome,
                        entry_time_utc=active_buf.entry_time_utc,
                        exit_time_utc=t_exit,
                        traversal_duration_sec=dur,
                        implied_speed_mps=speed,
                        stationary_delay_sec=active_buf.stationary_delay_sec,
                        provenance_tier=ProvenanceTier.INTERPOLATED_CROSSING,
                        provenance_weight=0.80,
                        bounding_ping_interval_sec=delta_t,
                        crossed_segments_count=1,
                    )
                    emitted.append(rec)
                    self.completed_traversals.append(rec)
                active_buf = None
                self.active_buffers.pop(buf_key, None)

            # Case B: Multi-segment jump (segment started and completed entirely between pings)
            elif enclosed and not (active_buf and active_buf.segment_id == seg["segment_id"]):
                dur = t_exit - t_entry
                if dur > 0.5:
                    speed = seg["length_m"] / dur
                    rec = SegmentTraversalRecord(
                        segment_id=seg["segment_id"],
                        shape_id=shape_id,
                        seq=seg["seq"],
                        from_stop_id=seg["from_stop_id"],
                        to_stop_id=seg["to_stop_id"],
                        length_m=seg["length_m"],
                        vehicle_id=curr_obs.ping.vehicle_id,
                        is_anonymous=curr_obs.ping.vehicle_id is None,
                        matching_outcome=curr_obs.matching_outcome,
                        entry_time_utc=t_entry,
                        exit_time_utc=t_exit,
                        traversal_duration_sec=dur,
                        implied_speed_mps=speed,
                        stationary_delay_sec=0.0,
                        provenance_tier=ProvenanceTier.MULTI_SEGMENT_INTERPOLATION,
                        provenance_weight=0.25,
                        bounding_ping_interval_sec=delta_t,
                        crossed_segments_count=2,
                    )
                    emitted.append(rec)
                    self.completed_traversals.append(rec)

            # Case C: Segment entered but not yet exited (currently occupied)
            elif entered and not exited:
                self.active_buffers[buf_key] = ActiveSegmentBuffer(
                    vehicle_key=vehicle_key,
                    shape_id=shape_id,
                    segment_id=seg["segment_id"],
                    seq=seg["seq"],
                    from_stop_id=seg["from_stop_id"],
                    to_stop_id=seg["to_stop_id"],
                    start_m=s_start,
                    end_m=s_end,
                    length_m=seg["length_m"],
                    entry_time_utc=t_entry,
                    last_ping_time_utc=curr_t,
                    last_along_track_m=curr_d,
                    is_entry_interpolated=True,
                    bounding_pings=[curr_obs],
                )

        return emitted
