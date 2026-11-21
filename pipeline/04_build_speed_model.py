"""
04_build_speed_model.py (Remediated - Phase 2)
Constructs the 4-tier hierarchical speed tensor strictly using approved training days:
Oct 1 (Thu), Oct 3 (Sat), Oct 4 (Sun).
Validation days (Oct 29) are completely isolated in data/processed/validation_obs/
and strictly excluded from training, calibration, and fallback estimation.
"""

from pathlib import Path
import numpy as np
import pandas as pd

PROC_DIR = Path("data/processed")
DAILY_DIR = PROC_DIR / "daily_segment_obs"

# Approved training days: Weekday (Oct 1) + Weekends (Oct 3, Oct 4)
APPROVED_TRAIN_DAYS = {
    "bus_movements_2026_10_01",
    "bus_movements_2026_10_03",
    "bus_movements_2026_10_04",
}


def period_bucket(hour: int) -> str:
    if 7 <= hour <= 10:
        return "AM_PEAK"
    if 11 <= hour <= 16:
        return "MIDDAY"
    if 17 <= hour <= 21:
        return "PM_PEAK"
    return "NIGHT"


def main():
    print("1. Loading canonical segments...")
    segs = pd.read_parquet(PROC_DIR / "segments.parquet")[
        ["segment_id", "shape_id", "from_stop_id", "to_stop_id", "length_m", "sched_sec"]
    ]
    segs["sched_speed_mps"] = segs["length_m"] / segs["sched_sec"].clip(lower=10.0)

    train_dfs = []
    loaded_stems = []

    for pq in sorted(DAILY_DIR.glob("*.parquet")):
        if pq.stem in APPROVED_TRAIN_DAYS:
            df = pd.read_parquet(pq)
            if not df.empty:
                train_dfs.append(df)
                loaded_stems.append(pq.stem)

    if not train_dfs:
        raise RuntimeError("No approved training parquet files found in data/processed/daily_segment_obs/")

    train = pd.concat(train_dfs, ignore_index=True)
    train["is_weekend"] = (train["dow"] >= 5).astype(int)
    train["period"] = train["hour"].map(period_bucket)
    print(f"Loaded {len(train):,} training segment traversals across {len(loaded_stems)} approved days: {loaded_stems}")

    wkday_obs = (train["is_weekend"] == 0).sum()
    wknd_obs = (train["is_weekend"] == 1).sum()
    print(f"  - Weekday observations: {wkday_obs:,}")
    print(f"  - Weekend observations: {wknd_obs:,}")

    # --- Tier 1: Exact (segment_id, is_weekend, hour) ---
    print("\n2. Aggregating 4 Fallback Tiers...")
    t1 = (
        train.groupby(["segment_id", "is_weekend", "hour"], as_index=False)
        .agg(t1_sec=("duration_sec", "median"), t1_n=("duration_sec", "count"))
    )

    # --- Tier 2: Segment + Period Bucket (segment_id, is_weekend, period) ---
    t2 = (
        train.groupby(["segment_id", "is_weekend", "period"], as_index=False)
        .agg(t2_sec=("duration_sec", "median"), t2_n=("duration_sec", "count"))
    )

    # --- Tier 3: Physical Stop-Pair Corridor (from_stop_id, to_stop_id, is_weekend, hour) ---
    train_corr = train.merge(segs[["segment_id", "from_stop_id", "to_stop_id", "length_m"]], on="segment_id", how="inner")
    train_corr["speed_mps"] = train_corr["length_m"] / train_corr["duration_sec"].clip(lower=3.0)
    t3 = (
        train_corr.groupby(["from_stop_id", "to_stop_id", "is_weekend", "hour"], as_index=False)
        .agg(t3_speed_mps=("speed_mps", "median"), t3_n=("speed_mps", "count"))
    )

    # --- Tier 4: City-wide hourly speed ratio vs GTFS schedule ---
    train_city = train.merge(segs[["segment_id", "sched_sec"]], on="segment_id", how="inner")
    train_city["ratio"] = train_city["duration_sec"] / train_city["sched_sec"].clip(lower=10.0)
    t4 = (
        train_city.groupby(["is_weekend", "hour"], as_index=False)
        .agg(city_ratio=("ratio", "median"))
    )

    # Build dense grid: all 106,160 segments x 2 (weekday/weekend) x 24 hours = 5,095,680 slots
    print("3. Building dense 4-tier fallback speed tensor...")
    grid = (
        segs[["segment_id", "from_stop_id", "to_stop_id", "length_m", "sched_sec"]]
        .assign(key=1)
        .merge(pd.DataFrame({"is_weekend": [0, 1], "key": 1}), on="key")
        .merge(pd.DataFrame({"hour": list(range(24)), "key": 1}), on="key")
        .drop(columns=["key"])
    )
    grid["period"] = grid["hour"].map(period_bucket)

    grid = (
        grid.merge(t1, on=["segment_id", "is_weekend", "hour"], how="left")
        .merge(t2, on=["segment_id", "is_weekend", "period"], how="left")
        .merge(t3, on=["from_stop_id", "to_stop_id", "is_weekend", "hour"], how="left")
        .merge(t4, on=["is_weekend", "hour"], how="left")
    )

    grid["city_ratio"] = grid["city_ratio"].fillna(1.0).clip(0.55, 2.2)
    t3_sec = grid["length_m"] / grid["t3_speed_mps"].clip(lower=0.5)
    t4_sec = grid["sched_sec"] * grid["city_ratio"]

    # Hierarchical selection: Tier 1 (n>=5) -> Tier 2 (n>=5) -> Tier 3 (n>=3) -> Tier 4
    pred = np.where(
        grid["t1_n"].fillna(0) >= 5,
        grid["t1_sec"],
        np.where(
            grid["t2_n"].fillna(0) >= 5,
            grid["t2_sec"],
            np.where(grid["t3_n"].fillna(0) >= 3, t3_sec, t4_sec),
        ),
    )

    tier_used = np.where(
        grid["t1_n"].fillna(0) >= 5,
        1,
        np.where(grid["t2_n"].fillna(0) >= 5, 2, np.where(grid["t3_n"].fillna(0) >= 3, 3, 4)),
    )

    max_clip = np.maximum(7200.0, grid["sched_sec"] * 2.5)
    grid["hist_sec"] = np.round(np.clip(pred, 5.0, max_clip), 1)
    grid["fallback_tier"] = tier_used
    grid["obs_count"] = grid["t1_n"].fillna(0).astype(int)

    # Compute quiet-hour baseline (minimum of off-peak hours 01:00, 02:00, 03:00, 04:00 AM)
    print("4. Calibrating off-peak quiet-hour baselines...")
    quiet = (
        grid[grid["hour"].isin([1, 2, 3, 4])]
        .groupby(["segment_id", "is_weekend"], as_index=False)
        .agg(quiet_sec=("hist_sec", "min"))
    )
    grid = grid.merge(quiet, on=["segment_id", "is_weekend"], how="left")
    grid["quiet_sec"] = grid["quiet_sec"].fillna(grid["hist_sec"])

    out_cols = ["segment_id", "is_weekend", "hour", "hist_sec", "quiet_sec", "fallback_tier", "obs_count"]
    out = grid[out_cols].copy()
    out.to_parquet(PROC_DIR / "speed_profile.parquet", index=False)

    print("\nDone! Speed profile summary:")
    print("Tier breakdown across all slots:")
    print(out["fallback_tier"].value_counts().sort_index().rename("slot_count"))
    print("\nWeekday Tier breakdown:")
    print(out[out["is_weekend"] == 0]["fallback_tier"].value_counts().sort_index())
    print("\nWeekend Tier breakdown:")
    print(out[out["is_weekend"] == 1]["fallback_tier"].value_counts().sort_index())
    print(f"\nSaved {len(out):,} segment-time slots to {PROC_DIR / 'speed_profile.parquet'}")


if __name__ == "__main__":
    main()
