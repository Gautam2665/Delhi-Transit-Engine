import heapq
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree

IST = timezone(timedelta(hours=5, minutes=30))
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)

WALK_SPEED_MPS = 1.2
MAX_ACCESS_WALK_M = 650.0
MAX_TRANSFER_WALK_M = 350.0
TRANSFER_PENALTY_SEC = 210.0  # 3.5 min boarding/transfer friction


@dataclass(slots=True)
class TransitEdge:
    segment_id: str
    shape_id: str
    route_id: str
    from_stop_id: str
    to_stop_id: str
    length_m: float


class DelhiBusRouter:
    def __init__(
        self,
        stops_df: pd.DataFrame,
        routes_df: pd.DataFrame,
        route_shapes_df: pd.DataFrame,
        segments_df: pd.DataFrame,
        speed_df: pd.DataFrame,
        telemetry_store,
    ):
        self.telemetry = telemetry_store
        self.stops = stops_df.set_index("stop_id").to_dict("index")
        self.stop_ids = stops_df["stop_id"].tolist()
        self.stop_xy = stops_df[["x", "y"]].values
        self.kdtree = cKDTree(self.stop_xy)

        self.route_names = {}
        for r in routes_df.itertuples():
            rid = str(r.route_id)
            short_n = str(getattr(r, "route_short_name", "") or "").strip()
            long_n = str(getattr(r, "route_long_name", "") or "").strip()
            self.route_names[rid] = short_n or long_n or rid

        # Map shape_id -> primary route_id
        shape_to_route = (
            route_shapes_df.drop_duplicates("shape_id")
            .set_index("shape_id")["route_id"]
            .to_dict()
        )

        # Speed lookup: (segment_id, is_weekend, hour) -> (hist_sec, quiet_sec)
        self.speed_map = {
            (r.segment_id, int(r.is_weekend), int(r.hour)): (float(r.hist_sec), float(r.quiet_sec))
            for r in speed_df.itertuples()
        }

        # Build adjacency list: from_stop_id -> list[TransitEdge]
        self.adj: dict[str, list[TransitEdge]] = {sid: [] for sid in self.stop_ids}
        for r in segments_df.itertuples():
            rid = shape_to_route.get(r.shape_id)
            if not rid or r.from_stop_id not in self.adj:
                continue
            self.adj[r.from_stop_id].append(
                TransitEdge(
                    segment_id=r.segment_id,
                    shape_id=r.shape_id,
                    route_id=rid,
                    from_stop_id=r.from_stop_id,
                    to_stop_id=r.to_stop_id,
                    length_m=float(r.length_m),
                )
            )

        # Precompute footpath transfers (< 350m) between stops
        self.transfers: dict[str, list[tuple[str, float, float]]] = {sid: [] for sid in self.stop_ids}
        pairs = self.kdtree.query_pairs(r=MAX_TRANSFER_WALK_M)
        for i, j in pairs:
            dist_m = float(np.linalg.norm(self.stop_xy[i] - self.stop_xy[j])) * 1.25  # Manhattan detour factor
            walk_sec = dist_m / WALK_SPEED_MPS
            s1, s2 = self.stop_ids[i], self.stop_ids[j]
            self.transfers[s1].append((s2, walk_sec, dist_m))
            self.transfers[s2].append((s1, walk_sec, dist_m))

    def resolve_location(self, query: str) -> dict:
        """Parses 'lat,lon' string or fuzzy stop_name substring into {name, lat, lng, x, y}."""
        q = query.strip()
        if "," in q:
            parts = [p.strip() for p in q.split(",")]
            try:
                lat, lng = float(parts[0]), float(parts[1])
                x, y = TO_UTM.transform(lng, lat)
                return {"name": f"{lat:.4f}, {lng:.4f}", "lat": lat, "lng": lng, "x": x, "y": y}
            except ValueError:
                pass

        q_low = q.lower()
        exact = [sid for sid, s in self.stops.items() if str(s["stop_name"]).lower() == q_low]
        matches = exact or [sid for sid, s in self.stops.items() if q_low in str(s["stop_name"]).lower()]
        if not matches:
            raise ValueError(f"Location not found: '{query}'")

        best = self.stops[matches[0]]
        return {
            "name": best["stop_name"],
            "lat": float(best["stop_lat"]),
            "lng": float(best["stop_lon"]),
            "x": float(best["x"]),
            "y": float(best["y"]),
        }

    def _nearby_stops(self, x: float, y: float, radius_m: float = MAX_ACCESS_WALK_M) -> list[tuple[str, float, float]]:
        idxs = self.kdtree.query_ball_point([x, y], r=radius_m)
        if not idxs:
            # Fallback to single nearest stop within 2 km
            dist, idx = self.kdtree.query([x, y], k=1)
            if dist <= 2000.0:
                idxs = [int(idx)]
        out = []
        for idx in idxs:
            dist_m = float(math.hypot(self.stop_xy[idx][0] - x, self.stop_xy[idx][1] - y)) * 1.25
            out.append((self.stop_ids[idx], dist_m / WALK_SPEED_MPS, dist_m))
        return out

    def _edge_cost(self, edge: TransitEdge, arr_ts: float) -> tuple[float, float, float]:
        dt_ist = datetime.fromtimestamp(arr_ts, tz=IST)
        is_wknd = 1 if dt_ist.weekday() >= 5 else 0
        hr = dt_ist.hour
        hist_sec, quiet_sec = self.speed_map.get((edge.segment_id, is_wknd, hr), (45.0, 35.0))
        if hasattr(self.telemetry, "traversals_by_segment"):
            # Fast-path: if this segment has no recorded traversals, fallback to historical duration immediately
            if edge.segment_id in self.telemetry.traversals_by_segment:
                blended = self.telemetry.get_blended_travel_time(edge.segment_id, is_wknd, hr, now_utc=arr_ts)
                return blended.blended_duration_sec, quiet_sec, blended.effective_live_weight
            return hist_sec, quiet_sec, 0.0
        elif hasattr(self.telemetry, "get_blended_travel_time"):
            blended = self.telemetry.get_blended_travel_time(edge.segment_id, is_wknd, hr, now_utc=arr_ts)
            return blended.blended_duration_sec, quiet_sec, blended.effective_live_weight
        elif hasattr(self.telemetry, "get_live_multiplier"):
            mult, conf = self.telemetry.get_live_multiplier(edge.from_stop_id, edge.to_stop_id, arr_ts)
            return hist_sec * mult, quiet_sec, conf
        return hist_sec, quiet_sec, 0.0

    def plan(self, origin_q: str, dest_q: str, depart_iso: str | None = None, k: int = 3) -> dict:
        orig = self.resolve_location(origin_q)
        dest = self.resolve_location(dest_q)

        if depart_iso:
            # Handle URL query unencoded '+' decoded to space
            clean_iso = depart_iso.strip().replace(" ", "+")
            dt = datetime.fromisoformat(clean_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
        else:
            dt = datetime.now(tz=IST)
        depart_ts = dt.timestamp()

        start_stops = self._nearby_stops(orig["x"], orig["y"])
        end_stops = {sid: (w_sec, d_m) for sid, w_sec, d_m in self._nearby_stops(dest["x"], dest["y"])}

        if not start_stops or not end_stops:
            return {"origin": orig, "destination": dest, "departure_time": dt.isoformat(), "journeys": []}

        penalized_routes: set[str] = set()
        journeys = []

        for _ in range(k):
            itinerary = self._dijkstra_single(orig, dest, depart_ts, start_stops, end_stops, penalized_routes)
            if not itinerary:
                break
            journeys.append(itinerary)
            # Penalize routes used in this itinerary to surface diverse alternatives on next pass
            for leg in itinerary["legs"]:
                if leg["mode"] == "BUS":
                    penalized_routes.add(leg["route_id"])

        for idx, j in enumerate(journeys, start=1):
            j["option_id"] = idx

        return {
            "origin": {"name": orig["name"], "lat": orig["lat"], "lng": orig["lng"]},
            "destination": {"name": dest["name"], "lat": dest["lat"], "lng": dest["lng"]},
            "departure_time": dt.isoformat(),
            "journeys": journeys,
        }

    def _dijkstra_single(self, orig, dest, depart_ts, start_stops, end_stops, penalized_routes):
        # State: (stop_id, active_route_id) -> best_arrival_ts
        dist: dict[tuple[str, str | None], float] = {}
        parent: dict[tuple[str, str | None], tuple] = {}
        pq = []

        for sid, walk_sec, walk_m in start_stops:
            t0 = depart_ts + walk_sec
            state = (sid, None)
            if t0 < dist.get(state, float("inf")):
                dist[state] = t0
                parent[state] = ("ORIGIN", orig["name"], walk_sec, walk_m)
                heapq.heappush(pq, (t0, sid, None))

        best_goal = None
        best_goal_ts = float("inf")

        while pq:
            cur_ts, sid, cur_route = heapq.heappop(pq)
            if cur_ts > dist.get((sid, cur_route), float("inf")) or cur_ts >= best_goal_ts:
                continue

            # Check if we can walk to destination from current stop
            if sid in end_stops:
                egress_sec, egress_m = end_stops[sid]
                fin_ts = cur_ts + egress_sec
                if fin_ts < best_goal_ts:
                    best_goal_ts = fin_ts
                    best_goal = (sid, cur_route, egress_sec, egress_m)

            dt_ist = datetime.fromtimestamp(cur_ts, tz=IST)
            is_wknd = 1 if dt_ist.weekday() >= 5 else 0
            hr = dt_ist.hour

            # 1. Explore bus segments departing from sid
            for edge in self.adj.get(sid, ()):
                same_route = cur_route == edge.route_id
                wait_sec = 0.0 if same_route else (self.telemetry.estimate_wait_sec(edge.route_id, is_wknd, hr) + TRANSFER_PENALTY_SEC)
                diversity_pen = 600.0 if (not same_route and edge.route_id in penalized_routes) else 0.0

                ride_sec, quiet_sec, conf = self._edge_cost(edge, cur_ts + wait_sec)
                nxt_ts = cur_ts + wait_sec + ride_sec + diversity_pen
                nxt_state = (edge.to_stop_id, edge.route_id)

                if nxt_ts < dist.get(nxt_state, float("inf")):
                    dist[nxt_state] = nxt_ts
                    parent[nxt_state] = ("BUS", (sid, cur_route), edge, wait_sec, ride_sec, quiet_sec, conf)
                    heapq.heappush(pq, (nxt_ts, edge.to_stop_id, edge.route_id))

            # 2. Explore footpath transfers to nearby stops
            for nxt_sid, walk_sec, walk_m in self.transfers.get(sid, ()):
                nxt_ts = cur_ts + walk_sec
                nxt_state = (nxt_sid, None)
                if nxt_ts < dist.get(nxt_state, float("inf")):
                    dist[nxt_state] = nxt_ts
                    parent[nxt_state] = ("WALK_XFER", (sid, cur_route), nxt_sid, walk_sec, walk_m)
                    heapq.heappush(pq, (nxt_ts, nxt_sid, None))

        if not best_goal:
            return None

        return self._reconstruct(orig, dest, depart_ts, best_goal, parent)

    def _reconstruct(self, orig, dest, depart_ts, best_goal, parent):
        goal_sid, goal_route, egress_sec, egress_m = best_goal
        steps = []
        cur = (goal_sid, goal_route)

        while cur in parent:
            rec = parent[cur]
            kind = rec[0]
            if kind == "ORIGIN":
                steps.append(("ACCESS_WALK", orig["name"], cur[0], rec[2], rec[3]))
                break
            elif kind == "WALK_XFER":
                _, prev_state, to_sid, w_sec, w_m = rec
                steps.append(("TRANSFER_WALK", prev_state[0], to_sid, w_sec, w_m))
                cur = prev_state
            elif kind == "BUS":
                _, prev_state, edge, wait_sec, ride_sec, quiet_sec, conf = rec
                steps.append(("BUS", edge, wait_sec, ride_sec, quiet_sec, conf))
                cur = prev_state

        steps.reverse()

        # Merge consecutive BUS edges on the same route_id into clean legs
        legs = []
        total_sec = 0.0
        quiet_total_sec = 0.0
        bus_legs_count = 0

        for st in steps:
            if st[0] == "ACCESS_WALK":
                _, from_name, to_sid, w_sec, w_m = st
                legs.append({
                    "mode": "WALK",
                    "from": from_name,
                    "to": self.stops[to_sid]["stop_name"],
                    "to_stop_id": to_sid,
                    "duration_min": round(w_sec / 60.0, 1),
                    "distance_m": round(w_m),
                })
                total_sec += w_sec
                quiet_total_sec += w_sec
            elif st[0] == "TRANSFER_WALK":
                _, from_sid, to_sid, w_sec, w_m = st
                legs.append({
                    "mode": "WALK",
                    "from": self.stops[from_sid]["stop_name"],
                    "to": self.stops[to_sid]["stop_name"],
                    "duration_min": round(w_sec / 60.0, 1),
                    "distance_m": round(w_m),
                })
                total_sec += w_sec
                quiet_total_sec += w_sec
            elif st[0] == "BUS":
                _, edge, wait_sec, ride_sec, quiet_sec, conf = st
                total_sec += wait_sec + ride_sec
                quiet_total_sec += wait_sec + quiet_sec

                if legs and legs[-1]["mode"] == "BUS" and legs[-1]["route_id"] == edge.route_id:
                    legs[-1]["to_stop"] = self.stops[edge.to_stop_id]["stop_name"]
                    legs[-1]["to_stop_id"] = edge.to_stop_id
                    legs[-1]["stops_count"] += 1
                    legs[-1]["_ride_sec"] += ride_sec
                    legs[-1]["_confs"].append(conf)
                    legs[-1]["segment_ids"].append(edge.segment_id)
                else:
                    bus_legs_count += 1
                    legs.append({
                        "mode": "BUS",
                        "route_id": edge.route_id,
                        "route_name": self.route_names.get(edge.route_id, edge.route_id),
                        "from_stop": self.stops[edge.from_stop_id]["stop_name"],
                        "from_stop_id": edge.from_stop_id,
                        "to_stop": self.stops[edge.to_stop_id]["stop_name"],
                        "to_stop_id": edge.to_stop_id,
                        "stops_count": 1,
                        "wait_min": round(wait_sec / 60.0, 1),
                        "_ride_sec": ride_sec,
                        "_confs": [conf],
                        "segment_ids": [edge.segment_id],
                    })

        # Final egress walk to destination
        legs.append({
            "mode": "WALK",
            "from": self.stops[goal_sid]["stop_name"],
            "to": dest["name"],
            "duration_min": round(egress_sec / 60.0, 1),
            "distance_m": round(egress_m),
        })
        total_sec += egress_sec
        quiet_total_sec += egress_sec

        # Finalize bus leg summary metrics
        for leg in legs:
            if leg["mode"] == "BUS":
                leg["ride_min"] = round(leg.pop("_ride_sec") / 60.0, 1)
                confs = leg.pop("_confs")
                leg["live_data_confidence"] = round(float(np.mean(confs)), 2)

        est_min = round(total_sec / 60.0, 1)
        quiet_min = round(quiet_total_sec / 60.0, 1)
        arr_dt = datetime.fromtimestamp(depart_ts + total_sec, tz=IST)

        return {
            "total_duration_min": est_min,
            "quiet_hour_duration_min": quiet_min,
            "traffic_delay_min": round(max(0.0, est_min - quiet_min), 1),
            "estimated_arrival": arr_dt.isoformat(),
            "transfers": max(0, bus_legs_count - 1),
            "legs": legs,
        }
