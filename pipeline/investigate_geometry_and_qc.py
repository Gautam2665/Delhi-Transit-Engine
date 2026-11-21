import pandas as pd
import numpy as np
from shapely import wkb
from shapely.geometry import Point

# Load datasets
segs = pd.read_parquet("data/processed/segments.parquet")
shapes_df = pd.read_parquet("data/processed/shapes_utm.parquet")
stops = pd.read_parquet("data/processed/stops_utm.parquet").set_index("stop_id")
shapes = {r.shape_id: wkb.loads(r.wkb) for r in shapes_df.itertuples()}
rs = pd.read_parquet("data/processed/route_shapes.parquet")
routes = pd.read_csv("data/gtfs_static/routes.txt", dtype={"route_id": str}).set_index("route_id")

print("=================================================================")
print("ITEM 1: INVESTIGATION OF THE 15.0 KM SEGMENTS")
print("=================================================================")
max_segs = segs[segs["length_m"] >= 14900.0]
print(f"Total segments with length >= 14.9 km: {len(max_segs)}")
print(max_segs[["segment_id", "shape_id", "from_stop_id", "to_stop_id", "start_m", "end_m", "length_m", "sched_sec"]])

for _, r in max_segs.iterrows():
    sid = r["shape_id"]
    line = shapes.get(sid)
    f_stop = stops.loc[r["from_stop_id"]]
    t_stop = stops.loc[r["to_stop_id"]]
    p1 = Point(f_stop["x"], f_stop["y"])
    p2 = Point(t_stop["x"], t_stop["y"])
    euclid_dist = p1.distance(p2)
    full_proj_1 = line.project(p1)
    full_proj_2 = line.project(p2)
    print(f"\nShape: {sid} (Total line length: {line.length:.1f} m)")
    print(f"  Segment: {r['segment_id']}")
    print(f"  From: {f_stop['stop_name']} ({f_stop['stop_lat']:.5f}, {f_stop['stop_lon']:.5f})")
    print(f"  To:   {t_stop['stop_name']} ({t_stop['stop_lat']:.5f}, {t_stop['stop_lon']:.5f})")
    print(f"  Euclidean straight-line distance: {euclid_dist:.1f} m")
    print(f"  Full line project p1: {full_proj_1:.1f} m | p2: {full_proj_2:.1f} m | Delta: {full_proj_2 - full_proj_1:.1f} m")
    print(f"  Stored in segments.parquet: start_m={r['start_m']:.1f} m, end_m={r['end_m']:.1f} m, length_m={r['length_m']:.1f} m")

print("\n=================================================================")
print("ITEM 2: RESOLUTION OF THE 74 NON-MONOTONIC SEGMENTS")
print("=================================================================")
non_mono = segs[segs["start_m"] >= segs["end_m"]].copy()
print(f"Found {len(non_mono)} non-monotonic segments (start_m >= end_m).")

categories = {"coincident_stops": 0, "loop_depot_geometry": 0, "data_inconsistency": 0, "projection_anomaly": 0}
details = []

for _, r in non_mono.iterrows():
    f_stop = stops.loc[r["from_stop_id"]]
    t_stop = stops.loc[r["to_stop_id"]]
    p1 = Point(f_stop["x"], f_stop["y"])
    p2 = Point(t_stop["x"], t_stop["y"])
    euclid_dist = p1.distance(p2)
    line = shapes.get(r["shape_id"])
    
    # Classify
    if euclid_dist < 5.0 or r["from_stop_id"] == r["to_stop_id"]:
        cat = "coincident_stops"
    elif "depot" in f_stop["stop_name"].lower() or "depot" in t_stop["stop_name"].lower() or "terminal" in f_stop["stop_name"].lower():
        cat = "loop_depot_geometry"
    elif line and (line.distance(p1) > 50.0 or line.distance(p2) > 50.0):
        cat = "projection_anomaly"
    else:
        cat = "data_inconsistency"
    categories[cat] += 1
    details.append({
        "segment_id": r["segment_id"],
        "shape_id": r["shape_id"],
        "from_stop": f"{f_stop['stop_name']} ({r['from_stop_id']})",
        "to_stop": f"{t_stop['stop_name']} ({r['to_stop_id']})",
        "start_m": r["start_m"],
        "end_m": r["end_m"],
        "length_m": r["length_m"],
        "euclid_m": round(euclid_dist, 1),
        "category": cat
    })

df_non_mono = pd.DataFrame(details)
print("\nClassification Summary:")
for cat, cnt in categories.items():
    print(f"  - {cat}: {cnt} ({cnt/len(non_mono)*100:.1f}%)")

print("\nSample Non-Monotonic Segments (First 15):")
print(df_non_mono[["segment_id", "from_stop", "to_stop", "start_m", "end_m", "euclid_m", "category"]].head(15).to_string())

print("\n=================================================================")
print("ITEM 3: GEOMETRY EXCEPTIONS (64 SHAPES WITH DISCONTINUITIES)")
print("=================================================================")
discont_shapes = []
for sid, grp in segs.groupby("shape_id"):
    starts = grp["start_m"].values[1:]
    ends = grp["end_m"].values[:-1]
    gaps = starts - ends
    max_gap = np.max(np.abs(gaps)) if len(gaps) > 0 else 0.0
    if max_gap > 100.0:
        discont_shapes.append({
            "shape_id": sid,
            "max_gap_m": round(max_gap, 1),
            "num_segments": len(grp),
            "line_length_m": round(shapes[sid].length, 1) if sid in shapes else 0.0
        })

df_discont = pd.DataFrame(discont_shapes).sort_values("max_gap_m", ascending=False)
print(f"Total shapes with inter-stop gap > 100m: {len(df_discont)}")
print("\nTop 15 Largest Discontinuities:")
print(df_discont.head(15).to_string(index=False))

print("\n--- 131.8m Maximum Snap Error Investigation ---")
max_snap_from = segs.loc[segs["snap_err_from_m"].idxmax()]
max_snap_to = segs.loc[segs["snap_err_to_m"].idxmax()]

stop_from = stops.loc[max_snap_from["from_stop_id"]]
stop_to = stops.loc[max_snap_to["to_stop_id"]]
line_snap = shapes.get(max_snap_from["shape_id"])

print(f"Max Snap Error From: {max_snap_from['snap_err_from_m']} m")
print(f"  Segment: {max_snap_from['segment_id']} | Shape: {max_snap_from['shape_id']}")
print(f"  Stop: {stop_from['stop_name']} (ID: {max_snap_from['from_stop_id']})")
print(f"  Coordinates: Lat {stop_from['stop_lat']:.6f}, Lon {stop_from['stop_lon']:.6f}")
print(f"  Point on Line Distance: {line_snap.distance(Point(stop_from['x'], stop_from['y'])):.1f} m")

print(f"\nMax Snap Error To: {max_snap_to['snap_err_to_m']} m")
print(f"  Segment: {max_snap_to['segment_id']} | Shape: {max_snap_to['shape_id']}")
print(f"  Stop: {stop_to['stop_name']} (ID: {max_snap_to['to_stop_id']})")
print(f"  Coordinates: Lat {stop_to['stop_lat']:.6f}, Lon {stop_to['stop_lon']:.6f}")

print("\n=================================================================")
print("ITEM 4: GPS GAP REJECTION BREAKDOWN (DAY 1)")
print("=================================================================")
# Load rejected observations
rej = pd.read_parquet("data/processed/rejected_obs/bus_movements_2026_10_01_rejected.parquet")
print(f"Total rejected observations on Day 1: {len(rej):,}")

# Inspect PING_GAP_TOO_LARGE
pg_rej = rej[rej["reject_reason"] == "PING_GAP_TOO_LARGE"]
print(f"PING_GAP_TOO_LARGE total: {len(pg_rej):,} ({len(pg_rej)/len(rej)*100:.1f}% of all rejections)")

# Join with segments to evaluate route types and express services
rej_with_segs = rej.merge(segs[["segment_id", "shape_id", "length_m", "sched_sec"]], on="segment_id")
rej_with_rs = rej_with_segs.merge(rs[["shape_id", "route_id"]], on="shape_id")
rej_with_rs["route_name"] = rej_with_rs["route_id"].map(routes["route_short_name"])

# Check long segments (> 3 km) vs short segments (<= 3 km)
rej_with_rs["is_long_segment"] = rej_with_rs["length_m"] > 3000.0
print("\nRejection Rate by Segment Length Category:")
long_rej = rej_with_rs[rej_with_rs["is_long_segment"]]
short_rej = rej_with_rs[~rej_with_rs["is_long_segment"]]
print(f"  Long segments (>3km): {len(long_rej):,} rejections ({long_rej['reject_reason'].value_counts().to_dict()})")
print(f"  Standard segments (<=3km): {len(short_rej):,} rejections ({short_rej['reject_reason'].value_counts().to_dict()})")
