"""
02_build_segments.py (Remediated - Phase 1)
Monotonic Windowed Linear Referencing: Stops -> Route Shapes (EPSG:32643 UTM Zone 43N).
Eliminates loop collapse and artificial 20m segment artifacts while preserving GTFS stop_sequence.
"""

from pathlib import Path
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import substring

GTFS_DIR = Path("data/gtfs_static")
OUT_DIR = Path("data/processed")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# WGS84 -> UTM Zone 43N (Delhi metric coordinates)
to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)


def time_to_sec(t_str: str) -> float:
    if pd.isna(t_str):
        return np.nan
    h, m, s = map(int, str(t_str).strip().split(":"))
    return h * 3600 + m * 60 + s


def main():
    print("1. Loading static GTFS...")
    stops = pd.read_csv(GTFS_DIR / "stops.txt", dtype={"stop_id": str})
    routes = pd.read_csv(GTFS_DIR / "routes.txt", dtype={"route_id": str})
    trips = pd.read_csv(GTFS_DIR / "trips.txt", dtype={"route_id": str, "trip_id": str, "shape_id": str})
    stop_times = pd.read_csv(
        GTFS_DIR / "stop_times.txt",
        usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
        dtype={"trip_id": str, "stop_id": str, "stop_sequence": int},
    )
    shapes = pd.read_csv(
        GTFS_DIR / "shapes.txt",
        dtype={"shape_id": str, "shape_pt_sequence": int},
    )

    # Project stops to UTM meters
    sx, sy = to_utm.transform(stops["stop_lon"].values, stops["stop_lat"].values)
    stops["x"] = sx
    stops["y"] = sy
    stop_coords = stops.set_index("stop_id")[["x", "y"]].to_dict("index")

    print("2. Building UTM LineStrings for shapes...")
    shapes = shapes.sort_values(["shape_id", "shape_pt_sequence"])
    sh_x, sh_y = to_utm.transform(shapes["shape_pt_lon"].values, shapes["shape_pt_lat"].values)
    shapes["x"] = sh_x
    shapes["y"] = sh_y

    shape_geoms = {}
    shape_records = []
    for shape_id, grp in shapes.groupby("shape_id", sort=False):
        coords = list(zip(grp["x"].values, grp["y"].values))
        if len(coords) < 2:
            continue
        line = LineString(coords)
        shape_geoms[shape_id] = line
        shape_records.append({
            "shape_id": shape_id,
            "wkb": line.wkb,
            "total_length_m": line.length,
        })

    pd.DataFrame(shape_records).to_parquet(OUT_DIR / "shapes_utm.parquet", index=False)

    # Map route_id -> list of active shape_ids
    route_shapes = (
        trips.dropna(subset=["shape_id"])
        .groupby(["route_id", "shape_id"])
        .size()
        .reset_index(name="trip_count")
        .sort_values(["route_id", "trip_count"], ascending=[True, False])
    )
    route_shapes.to_parquet(OUT_DIR / "route_shapes.parquet", index=False)

    print("3. Extracting canonical stop-to-stop segments per shape...")
    stop_times = stop_times.merge(trips[["trip_id", "route_id", "shape_id"]], on="trip_id", how="inner")
    stop_times = stop_times.dropna(subset=["shape_id"]).sort_values(["trip_id", "stop_sequence"])

    stop_times["arr_sec"] = stop_times["arrival_time"].map(time_to_sec)
    stop_times["dep_sec"] = stop_times["departure_time"].map(time_to_sec)

    stop_times["next_stop_id"] = stop_times.groupby("trip_id")["stop_id"].shift(-1)
    stop_times["next_arr_sec"] = stop_times.groupby("trip_id")["arr_sec"].shift(-1)
    stop_times["next_seq"] = stop_times.groupby("trip_id")["stop_sequence"].shift(-1)

    pairs = stop_times.dropna(subset=["next_stop_id"]).copy()
    pairs["sched_sec"] = (pairs["next_arr_sec"] - pairs["dep_sec"]).clip(lower=10)

    canon = (
        pairs.groupby(["shape_id", "stop_sequence", "stop_id", "next_stop_id"], as_index=False)
        .agg(sched_sec=("sched_sec", "median"), trip_support=("trip_id", "count"))
        .sort_values(["shape_id", "stop_sequence"])
    )

    print("4. Monotonic windowed linear referencing of stops onto shapes (adaptive)...")
    segment_rows = []
    expanded_segments = []
    flagged_boundary_segments = []

    for shape_id, grp in canon.groupby("shape_id", sort=False):
        line = shape_geoms.get(shape_id)
        if line is None or line.length < 10.0:
            continue

        prev_d2 = None
        prev_to_stop = None

        for row in grp.itertuples():
            c1 = stop_coords.get(row.stop_id)
            c2 = stop_coords.get(row.next_stop_id)
            if not c1 or not c2:
                continue

            p1 = Point(c1["x"], c1["y"])
            p2 = Point(c2["x"], c2["y"])

            # 1. Start distance d1: chained from prior segment's d2 for 100% inter-stop continuity
            if prev_d2 is not None and row.stop_id == prev_to_stop:
                d1 = prev_d2
            else:
                w1_start = max(0.0, (prev_d2 or 0.0) - 40.0)
                w1_end = min(line.length, w1_start + 12000.0)
                sub1 = substring(line, w1_start, w1_end)
                d1 = w1_start + sub1.project(p1)

            # 2. End distance d2
            is_coincident = (row.stop_id == row.next_stop_id)
            window_expanded = False
            hit_exp_boundary = False

            if is_coincident:
                d2 = d1
            else:
                # Primary local window: 15,000m forward
                w2_start = max(0.0, d1 - 10.0)
                w2_len = 15000.0
                w2_end = min(line.length, w2_start + w2_len)
                sub2 = substring(line, w2_start, w2_end)
                proj2 = sub2.project(p2)
                d2_candidate = w2_start + proj2

                # Detect if destination projection is within 1m of the forward window boundary
                hit_boundary = (w2_end < line.length) and (proj2 >= (w2_end - w2_start - 1.0))

                if hit_boundary:
                    # Expand forward window to 35,000m only when boundary condition occurs
                    w2_exp_len = 35000.0
                    w2_exp_end = min(line.length, w2_start + w2_exp_len)
                    sub2_exp = substring(line, w2_start, w2_exp_end)
                    proj2_exp = sub2_exp.project(p2)
                    d2_exp = w2_start + proj2_exp
                    window_expanded = True

                    # Verify stop-sequence order, direction of travel and consistency
                    hit_exp_boundary = (w2_exp_end < line.length) and (proj2_exp >= (w2_exp_end - w2_start - 1.0))
                    d2 = max(d1, d2_exp)
                    expanded_segments.append(f"{shape_id}:{row.stop_sequence}")

                    if hit_exp_boundary:
                        flagged_boundary_segments.append(f"{shape_id}:{row.stop_sequence}")
                else:
                    d2 = max(d1, d2_candidate)

            length_m = round(float(d2 - d1), 2)
            prev_d2 = d2
            prev_to_stop = row.next_stop_id

            segment_rows.append({
                "segment_id": f"{shape_id}:{row.stop_sequence}",
                "shape_id": shape_id,
                "seq": row.stop_sequence,
                "from_stop_id": row.stop_id,
                "to_stop_id": row.next_stop_id,
                "start_m": round(float(d1), 2),
                "end_m": round(float(d2), 2),
                "length_m": length_m,
                "window_expanded": window_expanded,
                "hit_exp_boundary": hit_exp_boundary,
                "snap_err_from_m": round(float(line.distance(p1)), 1),
                "snap_err_to_m": round(float(line.distance(p2)), 1),
                "sched_sec": round(float(row.sched_sec), 1),
                "trip_support": int(row.trip_support),
            })

    seg_df = pd.DataFrame(segment_rows)
    seg_df.to_parquet(OUT_DIR / "segments.parquet", index=False)
    stops.to_parquet(OUT_DIR / "stops_utm.parquet", index=False)

    print(f"Done! Built {len(seg_df):,} canonical stop-to-stop segments across {seg_df['shape_id'].nunique():,} shapes.")
    print(f"Median stop-to-shape snap error: {seg_df['snap_err_from_m'].median():.1f} m")
    print(f"Segments with window expanded to 35km: {len(expanded_segments):,}")
    print(f"Segments hitting 35km boundary (flagged): {len(flagged_boundary_segments):,}")
    print(f"Segments with length == 0.0m (coincident): {(seg_df['length_m'] == 0.0).sum():,}")
    print(f"Max segment length: {seg_df['length_m'].max():.2f} m")


if __name__ == "__main__":
    main()
