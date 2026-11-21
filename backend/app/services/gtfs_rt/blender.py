"""
blender.py
Unified weighted-median live segment speed estimator and external reliability blender
with 20-minute hard freshness cutoff and frozen historical fallback.
"""

from typing import List, Optional, Dict
import numpy as np

from backend.app.services.gtfs_rt.schemas import (
    SegmentTraversalRecord,
    LiveSegmentEstimate,
    BlendedSegmentTravelTime,
    MatchingOutcome,
)


class SpeedBlender:
    """Estimates robust live segment traversal times and blends them with historical priors."""

    def __init__(self, historical_speed_lookup: Dict[tuple, float]):
        """
        historical_speed_lookup: (segment_id, is_weekend, hour) -> hist_sec
        """
        self.hist_lookup = historical_speed_lookup

    def estimate_live_segment(
        self,
        segment_id: str,
        traversals: List[SegmentTraversalRecord],
        now_utc: float,
        window_sec: float = 1200.0,
    ) -> Optional[LiveSegmentEstimate]:
        """
        Calculates robust live segment traversal time using unified weighted median.
        Enforces one contribution per independent physical vehicle.
        """
        # Filter traversals within the 20-minute support window
        valid_pool = [t for t in traversals if (now_utc - t.exit_time_utc) <= window_sec]
        if not valid_pool:
            return None

        # Group by physical vehicle; take the most recent traversal per vehicle
        by_vehicle: Dict[str, SegmentTraversalRecord] = {}
        anon_traversals: List[SegmentTraversalRecord] = []

        for t in valid_pool:
            if t.vehicle_id and not t.is_anonymous:
                if t.vehicle_id not in by_vehicle or t.exit_time_utc > by_vehicle[t.vehicle_id].exit_time_utc:
                    by_vehicle[t.vehicle_id] = t
            else:
                anon_traversals.append(t)

        candidate_traversals: List[SegmentTraversalRecord] = list(by_vehicle.values())
        indep_vehicles_count = len(by_vehicle)

        # Pool anonymous traversals into at most one representative traversal
        # Ensure anonymous observations cannot inflate independent vehicle support
        if anon_traversals:
            anon_traversals.sort(key=lambda item: item.traversal_duration_sec)
            median_anon = anon_traversals[len(anon_traversals) // 2]
            candidate_traversals.append(median_anon)
            if indep_vehicles_count == 0:
                indep_vehicles_count = 1

        if not candidate_traversals:
            return None

        # Calculate observation weights: ω_j = ω_prov * exp(-age / 600)
        items = []
        newest_age = float("inf")

        for t in candidate_traversals:
            age = max(0.0, now_utc - t.exit_time_utc)
            if age < newest_age:
                newest_age = age
            w_age = float(np.exp(-age / 600.0))
            w = float(t.provenance_weight * w_age)
            items.append((t.traversal_duration_sec, w, t))

        # Sort by duration ascending
        items.sort(key=lambda x: x[0])
        durations = [x[0] for x in items]
        weights = [x[1] for x in items]
        total_w = sum(weights)

        if total_w <= 0.0:
            return None

        # Cumulative normalized weights
        cum_w = 0.0
        live_dur = durations[0]

        for i, (d, w, _) in enumerate(items):
            cum_w += w
            norm_cum = cum_w / total_w
            if norm_cum >= 0.5:
                # Exact tie handling at 50% boundary with next item
                if abs(norm_cum - 0.5) < 1e-6 and (i + 1) < len(items):
                    live_dur = (d + items[i + 1][0]) / 2.0
                else:
                    live_dur = d
                break

        length_m = candidate_traversals[0].length_m
        live_speed = length_m / max(0.1, live_dur)

        # Compute unrounded physical factor means across candidate observations
        match_scores = [
            1.00 if t.matching_outcome == MatchingOutcome.EXACT_MATCH else 0.70
            for t in candidate_traversals
        ]
        mean_m = float(np.mean(match_scores)) if match_scores else 0.70
        prov_weights = [t.provenance_weight for t in candidate_traversals]
        mean_p = float(np.mean(prov_weights)) if prov_weights else 0.80

        return LiveSegmentEstimate(
            segment_id=segment_id,
            sample_traversals_count=len(candidate_traversals),
            independent_vehicles_count=indep_vehicles_count,
            newest_observation_age_sec=newest_age,
            live_traversal_duration_sec=live_dur,
            live_speed_mps=live_speed,
            observation_provenance_weights=weights,
            mean_matching_confidence=mean_m,
            mean_provenance_weight=mean_p,
        )

    def blend_travel_time(
        self,
        segment_id: str,
        is_weekend: int,
        hour: int,
        live_est: Optional[LiveSegmentEstimate],
        now_utc: float,
        cross_track_m: float = 0.0,
    ) -> BlendedSegmentTravelTime:
        """
        Blends live estimate with frozen historical baseline.
        Falls back seamlessly to historical profile when live data is missing or stale.
        """
        hist_sec = self.hist_lookup.get((segment_id, is_weekend, hour), 60.0)

        # Baseline fallback state
        if live_est is None or live_est.newest_observation_age_sec >= 1200.0:
            return BlendedSegmentTravelTime(
                segment_id=segment_id,
                blended_duration_sec=hist_sec,
                historical_duration_sec=hist_sec,
                live_duration_sec=None,
                effective_live_weight=0.0,
                freshness_factor=0.0,
                sample_support_factor=0.0,
                provenance_factor=0.0,
                matching_factor=0.0,
                gps_quality_factor=0.0,
                is_fallback=True,
            )

        age = live_est.newest_observation_age_sec

        # 1. Freshness Factor (linear decay 600-1200s, hard 0 at >=1200s)
        if age <= 600.0:
            phi_freshness = 1.0
        elif age < 1200.0:
            phi_freshness = float((1200.0 - age) / 600.0)
        else:
            phi_freshness = 0.0

        # 2. Sample Support Confidence (does NOT suppress N < 3)
        n_veh = live_est.independent_vehicles_count
        if n_veh <= 1:
            psi_support = 0.40
        elif n_veh == 2:
            psi_support = 0.75
        else:
            psi_support = 1.00

        # 3. GPS Spatial Quality
        q_gps = float(max(0.0, 1.0 - cross_track_m / 65.0))

        # 4. Matching Confidence Factor from candidate observations
        m_match = float(np.clip(live_est.mean_matching_confidence, 0.0, 1.0))

        # 5. Provenance Factor from candidate observations
        p_prov = float(np.clip(live_est.mean_provenance_weight, 0.1, 1.0))

        # Effective live weight W_eff = phi * psi * m * q * p
        w_eff = float(phi_freshness * psi_support * m_match * q_gps * p_prov)
        w_eff = float(np.clip(w_eff, 0.0, 1.0))

        blended_sec = float(w_eff * live_est.live_traversal_duration_sec + (1.0 - w_eff) * hist_sec)

        return BlendedSegmentTravelTime(
            segment_id=segment_id,
            blended_duration_sec=blended_sec,
            historical_duration_sec=hist_sec,
            live_duration_sec=live_est.live_traversal_duration_sec,
            effective_live_weight=round(w_eff, 4),
            freshness_factor=round(phi_freshness, 4),
            sample_support_factor=round(psi_support, 4),
            provenance_factor=round(p_prov, 4),
            matching_factor=round(m_match, 4),
            gps_quality_factor=round(q_gps, 4),
            is_fallback=w_eff <= 0.01,
        )
