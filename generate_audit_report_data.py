import json
from pathlib import Path
import numpy as np
import pandas as pd

PROC_DIR = Path("data/processed")
GTFS_DIR = Path("data/gtfs_static")

# 1. Segments & Geometry statistics
seg = pd.read_parquet(PROC_DIR / "segments.parquet")
stops = pd.read_parquet(PROC_DIR / "stops_utm.parquet")
route_shapes = pd.read_parquet(PROC_DIR / "route_shapes.parquet")

snap_p = {f"p{p}": round(float(np.percentile(seg["snap_err_from_m"], p)), 2) for p in [50, 75, 90, 95, 99, 100]}

# Discontinuities
seg_sorted = seg.sort_values(["shape_id", "seq"])
seg_sorted["prev_end_m"] = seg_sorted.groupby("shape_id")["end_m"].shift(1)
discon = seg_sorted.dropna(subset=["prev_end_m"])
gap = discon["start_m"] - discon["prev_end_m"]
discon_count = int((gap.abs() > 0.01).sum())
max_gap = float(gap.abs().max()) if len(gap) > 0 else 0.0

# Expanded segments
expanded = seg[seg["window_expanded"] == True]
sched_speed_exp = (expanded["length_m"] / expanded["sched_sec"]) * 3.6

# Kashmere Gate -> Singhu Border
kg_sb = seg[seg["segment_id"].isin(["shp_3_1222:0", "shp_3_1228:0"])].to_dict(orient="records")

# Coincident & loop routes
coincident_count = int((seg["length_m"] == 0.0).sum())
loop_1203 = seg[seg["shape_id"] == "shp_3_1203"]

# 2. Day 1 Diagnostics
rej_01 = pd.read_parquet(PROC_DIR / "rejected_obs/bus_movements_2026_10_01_rejected.parquet")
valid_01 = pd.read_parquet(PROC_DIR / "daily_segment_obs/bus_movements_2026_10_01.parquet")

# Route info from segment_id
seg_to_shape = seg.set_index("segment_id")["shape_id"].to_dict()
shape_to_route = route_shapes.groupby("shape_id")["route_id"].first().to_dict()

rej_01["shape_id"] = rej_01["segment_id"].map(seg_to_shape)
rej_01["route_id"] = rej_01["shape_id"].map(shape_to_route)

valid_01["shape_id"] = valid_01["segment_id"].map(seg_to_shape)
valid_01["route_id"] = valid_01["shape_id"].map(shape_to_route)

rej_hourly_01 = rej_01.groupby("hour")["reject_reason"].value_counts().unstack(fill_value=0).to_dict(orient="index")
rej_by_route_01 = rej_01["route_id"].value_counts().head(10).to_dict()

# 3. Phase 2 Ingestion: Weekday, Saturday, Sunday
days_data = {}
for day_stem, label, dow_name in [
    ("bus_movements_2026_10_01", "Weekday", "Thursday"),
    ("bus_movements_2026_10_03", "Saturday", "Saturday"),
    ("bus_movements_2026_10_04", "Sunday", "Sunday"),
]:
    v_df = pd.read_parquet(PROC_DIR / f"daily_segment_obs/{day_stem}.parquet")
    r_df = pd.read_parquet(PROC_DIR / f"rejected_obs/{day_stem}_rejected.parquet")
    
    v_df["shape_id"] = v_df["segment_id"].map(seg_to_shape)
    v_df["route_id"] = v_df["shape_id"].map(shape_to_route)
    
    days_data[label] = {
        "day_stem": day_stem,
        "dow_name": dow_name,
        "valid_count": len(v_df),
        "rejected_count": len(r_df),
        "pass_rate_pct": round(len(v_df) / (len(v_df) + len(r_df)) * 100, 2),
        "unique_segments": int(v_df["segment_id"].nunique()),
        "unique_routes": int(v_df["route_id"].dropna().nunique()),
        "hourly_obs": v_df["hour"].value_counts().sort_index().to_dict(),
        "reject_breakdown": r_df["reject_reason"].value_counts().to_dict(),
    }

# Validation day (Oct 29)
val_29 = pd.read_parquet(PROC_DIR / "validation_obs/bus_movements_2026_10_29.parquet")
val_rej_29 = pd.read_parquet(PROC_DIR / "rejected_obs/bus_movements_2026_10_29_rejected.parquet")
val_29["shape_id"] = val_29["segment_id"].map(seg_to_shape)
val_29["route_id"] = val_29["shape_id"].map(shape_to_route)

val_data = {
    "day_stem": "bus_movements_2026_10_29",
    "dow_name": "Thursday (Held-Out Out-of-Sample)",
    "valid_count": len(val_29),
    "rejected_count": len(val_rej_29),
    "pass_rate_pct": round(len(val_29) / (len(val_29) + len(val_rej_29)) * 100, 2),
    "unique_segments": int(val_29["segment_id"].nunique()),
    "unique_routes": int(val_29["route_id"].dropna().nunique()),
}

# 4. Fallback Tier Simulation across all training days combined (Days 1, 3, 4)
train_dfs = [
    pd.read_parquet(PROC_DIR / f"daily_segment_obs/{d}.parquet")
    for d in ["bus_movements_2026_10_01", "bus_movements_2026_10_03", "bus_movements_2026_10_04"]
]
train_all = pd.concat(train_dfs, ignore_index=True)
train_all["is_weekend"] = train_all["dow"].isin([5, 6]).astype(int)

# Tier 1: (segment_id, is_weekend, hour) with count >= 3
t1_counts = train_all.groupby(["segment_id", "is_weekend", "hour"]).size().reset_index(name="n")
t1_slots = len(t1_counts[t1_counts["n"] >= 3])

# Total theoretical slots: 106,160 segments * 2 weekend flags * 24 hours = 5,095,680 slots
total_slots = len(seg) * 2 * 24

# Active transit slots (06:00 to 22:00 = 17 hours * 2 = 34 slots per segment = 3,609,440)
active_slots = len(seg) * 2 * 17

report = {
    "geometry": {
        "total_segments": len(seg),
        "total_shapes": int(seg["shape_id"].nunique()),
        "snap_percentiles": snap_p,
        "inter_stop_discontinuities": discon_count,
        "max_inter_stop_gap_m": max_gap,
        "coincident_stops_count": coincident_count,
        "expanded_segments_count": len(expanded),
        "flagged_35km_boundary": int(seg["hit_exp_boundary"].sum()),
        "expanded_length_stats": {
            "min": round(float(expanded["length_m"].min()), 2),
            "mean": round(float(expanded["length_m"].mean()), 2),
            "median": round(float(expanded["length_m"].median()), 2),
            "max": round(float(expanded["length_m"].max()), 2),
        },
        "expanded_speed_stats": {
            "min": round(float(sched_speed_exp.min()), 2),
            "mean": round(float(sched_speed_exp.mean()), 2),
            "median": round(float(sched_speed_exp.median()), 2),
            "max": round(float(sched_speed_exp.max()), 2),
        },
        "kg_sb": kg_sb,
        "loop_1203": {
            "segments": len(loop_1203),
            "expanded": bool(loop_1203["window_expanded"].any()),
            "min_len": round(float(loop_1203["length_m"].min()), 2),
            "median_len": round(float(loop_1203["length_m"].median()), 2),
            "max_len": round(float(loop_1203["length_m"].max()), 2),
        }
    },
    "day1_rejections": {
        "hourly": rej_hourly_01,
        "top_routes": rej_by_route_01,
    },
    "phase2_days": days_data,
    "validation_day": val_data,
    "fallback_tier_simulation": {
        "total_training_obs": len(train_all),
        "tier1_slots_n_ge_3": t1_slots,
        "tier1_slots_n_ge_1": len(t1_counts),
        "total_theoretical_slots": total_slots,
        "active_transit_slots": active_slots,
        "unique_segments_covered_in_training": int(train_all["segment_id"].nunique()),
        "pct_all_segments_covered": round(train_all["segment_id"].nunique() / len(seg) * 100, 2),
    }
}

Path("audit_phase2_report_data.json").write_text(json.dumps(report, indent=2))
print("Audit data generated successfully!")
