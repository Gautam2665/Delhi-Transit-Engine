import pandas as pd
import numpy as np

seg = pd.read_parquet("data/processed/segments.parquet")

print("=== 1. Kashmere Gate -> Singhu Border ===")
kg_sb = seg[seg["segment_id"].isin(["shp_3_1222:0", "shp_3_1228:0"])]
print(kg_sb[["segment_id", "from_stop_id", "to_stop_id", "start_m", "end_m", "length_m", "window_expanded", "sched_sec"]])

print("\n=== 2. The 60 Expanded Segments Summary ===")
expanded = seg[seg["window_expanded"] == True]
print(f"Total expanded: {len(expanded)}")
print(f"Length min: {expanded['length_m'].min():.2f}m, mean: {expanded['length_m'].mean():.2f}m, median: {expanded['length_m'].median():.2f}m, max: {expanded['length_m'].max():.2f}m")
sched_speed = (expanded["length_m"] / expanded["sched_sec"]) * 3.6
print("Scheduled speed (km/h) summary for expanded segments:")
print(sched_speed.describe())

print("\n=== 3. shp_3_1203 Loop Route ===")
loop = seg[seg["shape_id"] == "shp_3_1203"]
print(f"Segments on shp_3_1203: {len(loop)}")
print(f"Any window expanded on shp_3_1203? {loop['window_expanded'].any()}")
print(f"Segments < 20m on shp_3_1203: {(loop['length_m'] < 20.0).sum()}")
print(f"Length min: {loop['length_m'].min():.2f}m, median: {loop['length_m'].median():.2f}m, max: {loop['length_m'].max():.2f}m")

print("\n=== 4. Circular & Coincident Stops ===")
coincident = seg[seg["length_m"] == 0.0]
print(f"Total coincident segments (0.0m): {len(coincident)}")
print(f"Coincident segments where from_stop == to_stop: {(coincident['from_stop_id'] == coincident['to_stop_id']).sum()}")
print(f"Coincident segments where from_stop != to_stop: {(coincident['from_stop_id'] != coincident['to_stop_id']).sum()}")

print("\n=== 5. Normal Urban Route Control (shp_3_1) ===")
normal = seg[seg["shape_id"] == "shp_3_1"]
print(f"Segments on shp_3_1: {len(normal)}")
print(f"Any window expanded on shp_3_1? {normal['window_expanded'].any()}")
print("Length stats on shp_3_1:")
print(normal["length_m"].describe())

print("\n=== Snap Error Percentiles Across All 106,160 Segments ===")
print("snap_err_from_m percentiles (p50, p90, p95, p99, max):")
for p in [50, 90, 95, 99, 100]:
    val = np.percentile(seg["snap_err_from_m"], p)
    print(f"  p{p}: {val:.2f} m")

print("\n=== Inter-Stop Discontinuities (start_m[i+1] - end_m[i]) ===")
seg_sorted = seg.sort_values(["shape_id", "seq"])
seg_sorted["prev_end_m"] = seg_sorted.groupby("shape_id")["end_m"].shift(1)
discon = seg_sorted.dropna(subset=["prev_end_m"])
gap = discon["start_m"] - discon["prev_end_m"]
print(f"Non-zero inter-stop gaps: {(gap.abs() > 0.01).sum()} (out of {len(gap):,} transitions)")
print(f"Max gap: {gap.abs().max():.4f} m")
