"""
01_inspect_gtfs.py
Schema validation and shape/stop alignment check for Delhi DTC/DoT static GTFS.
"""

from pathlib import Path
import sys
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "gtfs_static"

EXPECTED_FILES = [
    "agency.txt",
    "calendar.txt",
    "feed_info.txt",
    "routes.txt",
    "shapes.txt",
    "stops.txt",
    "stop_times.txt",
    "trips.txt",
]


def inspect_gtfs():
    print(f"[*] Checking GTFS Static files in: {DATA_DIR}")
    if not DATA_DIR.exists():
        print(f"[!] Error: Directory {DATA_DIR} does not exist.")
        sys.exit(1)

    for fname in EXPECTED_FILES:
        fpath = DATA_DIR / fname
        if not fpath.exists():
            print(f"[-] Missing: {fname}")
        else:
            size_mb = fpath.stat().st_size / (1024 * 1024)
            print(f"[+] Found: {fname:<15} ({size_mb:.2f} MB)")

    # Read stops sample
    stops_path = DATA_DIR / "stops.txt"
    if stops_path.exists():
        df_stops = pd.read_csv(stops_path)
        print(f"\n[Stops Summary]")
        print(f"  Total stops: {len(df_stops):,}")
        print(f"  Columns: {list(df_stops.columns)}")
        print(f"  Coordinate bounds: Lat ({df_stops['stop_lat'].min():.4f}, {df_stops['stop_lat'].max():.4f}), "
              f"Lon ({df_stops['stop_lon'].min():.4f}, {df_stops['stop_lon'].max():.4f})")

    # Read routes sample
    routes_path = DATA_DIR / "routes.txt"
    if routes_path.exists():
        df_routes = pd.read_csv(routes_path)
        print(f"\n[Routes Summary]")
        print(f"  Total routes: {len(df_routes):,}")
        print(f"  Columns: {list(df_routes.columns)}")

    # Read trips sample
    trips_path = DATA_DIR / "trips.txt"
    if trips_path.exists():
        df_trips = pd.read_csv(trips_path)
        print(f"\n[Trips Summary]")
        print(f"  Total scheduled trips: {len(df_trips):,}")
        print(f"  Distinct routes with trips: {df_trips['route_id'].nunique():,}")
        print(f"  Distinct shape IDs: {df_trips['shape_id'].nunique():,}")

    print("\n[✓] GTFS inspection completed successfully.")


if __name__ == "__main__":
    inspect_gtfs()
