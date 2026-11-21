import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from backend.app.services.router import DelhiBusRouter
from backend.app.services.gtfs_rt.pipeline import GTFSRTPipeline

def main():
    speed_df = pd.read_parquet('data/processed/speed_profile.parquet')
    shapes_df = pd.read_parquet('data/processed/shapes_utm.parquet')
    route_shapes_df = pd.read_parquet('data/processed/route_shapes.parquet')
    segments_df = pd.read_parquet('data/processed/segments.parquet')
    stops_df = pd.read_parquet('data/processed/stops_utm.parquet')
    routes_df = pd.read_csv('data/gtfs_static/routes.txt', dtype={'route_id': str})

    pipeline = GTFSRTPipeline(shapes_df, route_shapes_df, segments_df, speed_df)
    router = DelhiBusRouter(stops_df, routes_df, route_shapes_df, segments_df, speed_df, pipeline)

    od_pairs = [
        ('Kashmere Gate', 'Nehru Place'),
        ('Anand Vihar', 'Shivaji Stadium'),
        ('28.7180, 77.0650', '28.5672, 77.2100')
    ]

    results = []

    for orig, dest in od_pairs:
        print(f"\n=======================================================")
        print(f"=== AUDIT OD PAIR: {orig} -> {dest} ===")
        print(f"=======================================================")
        res = router.plan(orig, dest, depart_iso='2026-11-20T08:30:00+05:30', k=3)
        journeys = res.get('journeys', [])
        od_data = {"origin": orig, "destination": dest, "departure": res.get("departure_time"), "options": []}
        
        for j in journeys:
            opt_id = j.get('option_id')
            total_min = j.get('total_duration_min')
            quiet_min = j.get('quiet_hour_duration_min')
            delay_min = j.get('traffic_delay_min')
            transfers = j.get('transfers')
            legs = j.get('legs', [])
            
            # Identify access walk (first leg if walk)
            access_walk = legs[0] if (legs and legs[0]['mode'] == 'WALK') else None
            access_min = access_walk.get('duration_min', 0.0) if access_walk else 0.0
            access_m = access_walk.get('distance_m', 0.0) if access_walk else 0.0
            
            # Identify egress walk (last leg if walk)
            egress_walk = legs[-1] if (len(legs) > 1 and legs[-1]['mode'] == 'WALK') else None
            egress_min = egress_walk.get('duration_min', 0.0) if egress_walk else 0.0
            egress_m = egress_walk.get('distance_m', 0.0) if egress_walk else 0.0
            
            # Intermediate transfer walks
            transfer_walks = [
                leg for idx, leg in enumerate(legs)
                if leg['mode'] == 'WALK' and idx != 0 and idx != len(legs) - 1
            ]
            xfer_walk_min = sum(leg['duration_min'] for leg in transfer_walks)
            xfer_walk_m = sum(leg['distance_m'] for leg in transfer_walks)
            
            # Bus legs
            bus_legs = [leg for leg in legs if leg['mode'] == 'BUS']
            bus_ride_min = sum(leg['ride_min'] for leg in bus_legs)
            bus_wait_min = sum(leg['wait_min'] for leg in bus_legs)
            
            opt_dict = {
                "option_id": opt_id,
                "total_duration_min": total_min,
                "quiet_hour_min": quiet_min,
                "traffic_delay_min": delay_min,
                "transfers": transfers,
                "access_walk_min": access_min,
                "access_walk_m": access_m,
                "wait_time_min": bus_wait_min,
                "bus_ride_min": bus_ride_min,
                "transfer_walk_min": xfer_walk_min,
                "transfer_walk_m": xfer_walk_m,
                "egress_walk_min": egress_min,
                "egress_walk_m": egress_m,
                "legs_count": len(legs),
                "legs_summary": [
                    {
                        "mode": l["mode"],
                        "name": l.get("route_name") or "WALK",
                        "from": l.get("from") or l.get("from_stop"),
                        "to": l.get("to") or l.get("to_stop"),
                        "duration_min": (
                            l.get("duration_min")
                            if l.get("duration_min") is not None
                            else l.get("ride_min", 0.0)
                        ),
                        "wait_min": l.get("wait_min", 0.0)
                    } for l in legs
                ]
            }
            od_data["options"].append(opt_dict)
            
            print(f"Option {opt_id}: Total={total_min}m | AccessWalk={access_min}m ({access_m}m) | Wait={bus_wait_min}m | BusRide={bus_ride_min}m | XferWalk={xfer_walk_min}m | EgressWalk={egress_min}m ({egress_m}m) | Xfers={transfers}")
            for l in opt_dict["legs_summary"]:
                if l["mode"] == "BUS":
                    print(f"  -> BUS [{l['name']}]: {l['from']} -> {l['to']} (ride: {l['duration_min']}m, wait: {l['wait_min']}m)")
                else:
                    print(f"  -> WALK: {l['from']} -> {l['to']} ({l['duration_min']}m)")
        
        results.append(od_data)

    with open("scripts/od_passenger_audit_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\n[✓] Audit saved to scripts/od_passenger_audit_results.json")

if __name__ == '__main__':
    main()
