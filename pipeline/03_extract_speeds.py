"""
03_extract_speeds.py (Remediated - Phase 1 & 2)
Streaming 1-day-at-a-time 7z GPS speed profiler with Quality Control (QC) filtering
and separate diagnostic rejected observation output.
"""

import os
import sys
import argparse
import gc
import shutil
import subprocess
import sqlite3
import tempfile
from pathlib import Path
import numpy as np
import pandas as pd
import shapely
from shapely import wkb
from pyproj import Transformer

ARCHIVE_PATH = Path("data/raw_archive/delhi-dtc-bus-gtfs-rt-2026-10.7z")
PROC_DIR = Path("data/processed")
DAILY_OUT = PROC_DIR / "daily_segment_obs"
VAL_OUT = PROC_DIR / "validation_obs"
REJECTED_OUT = PROC_DIR / "rejected_obs"

DAILY_OUT.mkdir(parents=True, exist_ok=True)
VAL_OUT.mkdir(parents=True, exist_ok=True)
REJECTED_OUT.mkdir(parents=True, exist_ok=True)

# Detect 7z binary
SEVEN_ZIP_BIN = shutil.which("7z") or (
    r"C:\Program Files\7-Zip\7z.exe" if Path(r"C:\Program Files\7-Zip\7z.exe").exists() else "7z"
)

TEMP_BASE = Path("E:/delhi_engine_tmp") if Path("E:/delhi_engine_tmp").exists() else None

to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)

# Quality Control Thresholds
MAX_SNAP_DIST_M = 65.0      # Reject GPS points > 65m off route shape
MIN_DT_SEC = 3.0            # Phase 1 requirement: reject dt < 3.0s (quantization / glitch)
MIN_SPEED_KMH = 2.0         # Reject parked/depot layovers
MAX_SPEED_KMH = 75.0        # Reject GPS jump teleportation (> 75 km/h for city buses)
MAX_PING_GAP_SEC = 300.0    # Reject if bounding GPS pings > 5 min apart (blackout smearing)
MAX_PING_GAP_DIST_M = 1500.0  # Reject if bounding GPS pings > 1.5 km apart


def list_archive_dbs(archive: Path) -> list[str]:
    res = subprocess.run([SEVEN_ZIP_BIN, "l", "-slt", str(archive)], capture_output=True, text=True, check=True)
    dbs = []
    for line in res.stdout.splitlines():
        if line.startswith("Path = ") and line.endswith(".db"):
            dbs.append(line.split("Path = ", 1)[1].strip())
    return sorted(dbs)


def inspect_sqlite_schema(db_path: Path) -> tuple[str, dict]:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    table = tables[0]
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    conn.close()
    del conn
    gc.collect()

    col_map = {
        "vehicle_id": "vehicle_id",
        "route_id": "route_id",
        "lat": "lat" if "lat" in cols else "latitude",
        "lng": "lng" if "lng" in cols else ("lon" if "lon" in cols else "longitude"),
        "timestamp": "timestamp" if "timestamp" in cols else "ts",
    }
    return table, col_map


def process_single_day(
    db_path: Path,
    shapes_dict: dict,
    route_to_shapes: dict,
    segs_by_shape: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    table, cmap = inspect_sqlite_schema(db_path)
    conn = sqlite3.connect(db_path)
    query = f"""
        SELECT {cmap['vehicle_id']} AS vehicle_id,
               {cmap['route_id']} AS route_id,
               {cmap['lat']} AS lat,
               {cmap['lng']} AS lng,
               {cmap['timestamp']} AS ts
        FROM {table}
        WHERE {cmap['route_id']} IS NOT NULL AND {cmap['lat']} IS NOT NULL
    """
    
    # Chunked ingestion with strict memory-efficient types to prevent RAM spikes
    chunks = []
    chunk_iter = pd.read_sql_query(query, conn, chunksize=2_000_000)
    for ch in chunk_iter:
        ch["lat"] = pd.to_numeric(ch["lat"], errors="coerce")
        ch["lng"] = pd.to_numeric(ch["lng"], errors="coerce")
        ch["ts"] = pd.to_numeric(ch["ts"], errors="coerce")
        ch["route_id"] = pd.to_numeric(ch["route_id"], errors="coerce")
        ch = ch.dropna(subset=["vehicle_id", "route_id", "lat", "lng", "ts"])
        ch = ch[(ch["lat"] > 27.5) & (ch["lat"] < 29.5) & (ch["lng"] > 76.0) & (ch["lng"] < 78.0)]
        ch["route_id"] = ch["route_id"].astype("int32")
        ch["lat"] = ch["lat"].astype("float32")
        ch["lng"] = ch["lng"].astype("float32")
        ch["ts"] = ch["ts"].astype("float64")
        chunks.append(ch)

    conn.close()
    del conn
    gc.collect()

    if not chunks:
        return pd.DataFrame(), pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True)
    del chunks
    gc.collect()

    # Convert vehicle_id to category for compact, fast sorting
    df["vehicle_id"] = df["vehicle_id"].astype("category")
    df = df.sort_values(["vehicle_id", "ts"]).reset_index(drop=True)

    # Project to metric coordinates
    x_arr, y_arr = to_utm.transform(df["lng"].values, df["lat"].values)

    # Vectorized run partitioning using integer codes
    v_codes = df["vehicle_id"].cat.codes.values
    rids = df["route_id"].values
    ts_all = df["ts"].values
    n_rows = len(df)
    del df
    gc.collect()

    run_breaks = np.flatnonzero(
        (v_codes[1:] != v_codes[:-1]) |
        (rids[1:] != rids[:-1]) |
        ((ts_all[1:] - ts_all[:-1]) > 900)
    )
    run_starts = np.r_[0, run_breaks + 1]
    run_ends = np.r_[run_breaks + 1, n_rows]

    valid_observations = []
    rejected_observations = []

    for r_start, r_end in zip(run_starts, run_ends):
        if (r_end - r_start) < 4:
            continue

        route_id = str(rids[r_start])
        candidate_shapes = route_to_shapes.get(route_id)
        if not candidate_shapes:
            continue

        rx = x_arr[r_start:r_end]
        ry = y_arr[r_start:r_end]
        rts = ts_all[r_start:r_end]

        pts = shapely.points(rx, ry)

        best_shape = None
        best_dists = None
        best_score = float("inf")

        for sid in candidate_shapes[:4]:
            line = shapes_dict.get(sid)
            if line is None:
                continue

            perp = shapely.distance(line, pts)
            med_perp = float(np.median(perp))
            if med_perp > MAX_SNAP_DIST_M:
                continue

            along = shapely.line_locate_point(line, pts)
            if (along[-1] - along[0]) < 150.0:
                continue

            if med_perp < best_score:
                best_score = med_perp
                best_shape = sid
                best_dists = along

        if best_shape is None:
            continue

        seg_data = segs_by_shape.get(best_shape)
        if seg_data is None:
            continue

        s_starts, s_ends, s_lens, s_ids = seg_data

        # Keep strictly increasing trajectory progress
        mono_d = [best_dists[0]]
        mono_t = [rts[0]]
        for d, t in zip(best_dists[1:], rts[1:]):
            if d > mono_d[-1] and t > mono_t[-1]:
                mono_d.append(d)
                mono_t.append(t)

        if len(mono_d) < 3:
            continue

        mono_d = np.array(mono_d)
        mono_t = np.array(mono_t)
        min_d, max_d = mono_d[0], mono_d[-1]

        cov_mask = (s_starts >= min_d) & (s_ends <= max_d)
        if not np.any(cov_mask):
            continue

        c_start_m = s_starts[cov_mask]
        c_end_m = s_ends[cov_mask]
        c_length_m = s_lens[cov_mask]
        c_seg_id = s_ids[cov_mask]

        t_starts = np.interp(c_start_m, mono_d, mono_t)
        t_ends = np.interp(c_end_m, mono_d, mono_t)
        dt = t_ends - t_starts

        # Bounding ping gap inspection
        idx_s = np.clip(np.searchsorted(mono_d, c_start_m) - 1, 0, len(mono_d) - 1)
        idx_e = np.clip(np.searchsorted(mono_d, c_end_m), 0, len(mono_d) - 1)
        gap_dist = mono_d[idx_e] - mono_d[idx_s]
        gap_time = mono_t[idx_e] - mono_t[idx_s]

        # Actual unclipped physical speeds
        safe_dt = np.maximum(dt, 1e-4)
        true_speed_kmh = (c_length_m / safe_dt) * 3.6

        # Timestamp conversion to IST hour and day of week
        t_mid = 0.5 * (t_starts + t_ends)
        ist_sec = (t_mid + 19800).astype(np.int64)
        hours = ((ist_sec % 86400) // 3600).astype(int)
        dows = ((ist_sec // 86400 + 3) % 7).astype(int)

        # Vectorized QC Filtering
        non_positive_dist = c_length_m <= 0.0
        dt_too_small = dt < MIN_DT_SEC
        speed_too_high = true_speed_kmh > MAX_SPEED_KMH
        speed_too_low = (true_speed_kmh < MIN_SPEED_KMH) & (~non_positive_dist)
        ping_gap_too_large = (gap_time > MAX_PING_GAP_SEC) | (gap_dist > MAX_PING_GAP_DIST_M)

        valid_mask = (~non_positive_dist) & (~dt_too_small) & (~speed_too_high) & (~speed_too_low) & (~ping_gap_too_large)

        for i in range(len(c_start_m)):
            s_id = c_seg_id[i]
            dow = dows[i]
            hr = hours[i]
            dur = round(float(dt[i]), 1)
            lm = round(float(c_length_m[i]), 1)
            sp = round(float(true_speed_kmh[i]), 1)

            if valid_mask[i]:
                valid_observations.append((s_id, dow, hr, dur))
            else:
                reason = (
                    "NON_POSITIVE_DISTANCE" if non_positive_dist[i] else
                    "DT_TOO_SMALL" if dt_too_small[i] else
                    "SPEED_TOO_HIGH" if speed_too_high[i] else
                    "SPEED_TOO_LOW" if speed_too_low[i] else
                    "PING_GAP_TOO_LARGE"
                )
                rejected_observations.append((s_id, dow, hr, dur, lm, sp, reason))

    valid_df = pd.DataFrame(valid_observations, columns=["segment_id", "dow", "hour", "duration_sec"])
    rejected_df = pd.DataFrame(
        rejected_observations,
        columns=["segment_id", "dow", "hour", "duration_sec", "length_m", "speed_kmh", "reject_reason"]
    )
    return valid_df, rejected_df


def main():
    parser = argparse.ArgumentParser(description="Extract segment speeds with QC filters.")
    parser.add_argument("--days", nargs="+", help="Specific day stems (e.g. bus_movements_2026_10_01)")
    parser.add_argument("--is_val", action="store_true", help="Store in validation_obs instead of daily_segment_obs")
    args = parser.parse_args()

    print(f"Using 7z binary: {SEVEN_ZIP_BIN}")
    print("Loading shapes and segments...")
    shapes_df = pd.read_parquet(PROC_DIR / "shapes_utm.parquet")
    shapes_dict = {r.shape_id: wkb.loads(r.wkb) for r in shapes_df.itertuples()}

    rs_df = pd.read_parquet(PROC_DIR / "route_shapes.parquet")
    route_to_shapes = rs_df.groupby("route_id")["shape_id"].apply(list).to_dict()

    segs_df = pd.read_parquet(PROC_DIR / "segments.parquet")
    # Pre-extract numpy arrays for 50x faster slicing per run
    segs_by_shape = {
        sid: (
            grp["start_m"].values,
            grp["end_m"].values,
            grp["length_m"].values,
            grp["segment_id"].values,
        )
        for sid, grp in segs_df.groupby("shape_id")
    }

    db_entries = list_archive_dbs(ARCHIVE_PATH)
    print(f"Found {len(db_entries)} daily SQLite databases in archive.")

    target_entries = db_entries
    if args.days:
        target_set = set(args.days)
        target_entries = [e for e in db_entries if Path(e).stem in target_set]

    out_dir = VAL_OUT if args.is_val else DAILY_OUT
    print(f"Targeting {len(target_entries)} daily datasets. Destination: {out_dir.name}")

    for entry in target_entries:
        day_stem = Path(entry).stem
        out_file = out_dir / f"{day_stem}.parquet"
        rej_file = REJECTED_OUT / f"{day_stem}_rejected.parquet"

        print(f"\n[EXTRACT & QC] {entry} ...")
        t0 = pd.Timestamp.now()
        with tempfile.TemporaryDirectory(dir=TEMP_BASE, ignore_cleanup_errors=True) as tmpdir:
            subprocess.run([SEVEN_ZIP_BIN, "e", str(ARCHIVE_PATH), f"-o{tmpdir}", entry, "-y"], check=True)
            extracted_db = Path(tmpdir) / Path(entry).name
            valid_df, rejected_df = process_single_day(extracted_db, shapes_dict, route_to_shapes, segs_by_shape)
            
            valid_df.to_parquet(out_file, index=False)
            rejected_df.to_parquet(rej_file, index=False)

            t_elapsed = (pd.Timestamp.now() - t0).total_seconds()
            total_obs = len(valid_df) + len(rejected_df)
            pass_rate = (len(valid_df) / total_obs * 100.0) if total_obs > 0 else 0.0
            print(f"  -> Valid: {len(valid_df):,} | Rejected: {len(rejected_df):,} ({pass_rate:.1f}% pass rate) in {t_elapsed:.1f}s")
            if not rejected_df.empty:
                print("     Reject Breakdown:", rejected_df["reject_reason"].value_counts().to_dict())

            del valid_df, rejected_df
            gc.collect()


if __name__ == "__main__":
    main()
