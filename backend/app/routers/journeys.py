from fastapi import APIRouter, HTTPException, Query, Request
from pyproj import Transformer
from shapely import wkb
from shapely.ops import substring

router = APIRouter(tags=["journeys"])
TO_WGS84 = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)


@router.get("/journeys")
def get_journeys(
    request: Request,
    origin: str = Query(..., alias="from", description="Origin stop name or 'lat,lon'"),
    destination: str = Query(..., alias="to", description="Destination stop name or 'lat,lon'"),
    depart: str | None = Query(None, description="ISO-8601 departure timestamp"),
    k: int = Query(3, ge=1, le=5),
):
    planner = getattr(request.app.state, "router", None)
    if planner is None:
        raise HTTPException(
            status_code=503,
            detail="Transit router is not yet initialized. Please verify speed_profile.parquet is built.",
        )
    try:
        return planner.plan(origin_q=origin, dest_q=destination, depart_iso=depart, k=k)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/journeys/shape")
def get_leg_shape(request: Request, segment_ids: str = Query(..., description="Comma-separated segment_ids")):
    """Slices the exact UTM route shape between first and last segment and reprojects to WGS84 GeoJSON."""
    seg_list = [s.strip() for s in segment_ids.split(",") if s.strip()]
    if not seg_list:
        return {"type": "LineString", "coordinates": []}

    seg_meta = getattr(request.app.state, "segment_meta", {})  # dict: segment_id -> (shape_id, start_m, end_m)
    shapes_utm = getattr(request.app.state, "shapes_utm", {})    # dict: shape_id -> shapely LineString

    first = seg_meta.get(seg_list[0])
    last = seg_meta.get(seg_list[-1])
    if not first or not last:
        return {"type": "LineString", "coordinates": []}

    coords = []
    # If all segments belong to the same shape_id, do a fast single slice:
    if first[0] == last[0] and all(seg_meta.get(s, (None,))[0] == first[0] for s in seg_list):
        line = shapes_utm.get(first[0])
        if line is not None:
            sliced = substring(line, max(0.0, first[1]), min(line.length, last[2]))
            for x, y in sliced.coords:
                lon, lat = TO_WGS84.transform(x, y)
                coords.append([round(lon, 6), round(lat, 6)])
    else:
        # Segments span multiple shape variants; slice each segment and concatenate points:
        for sid in seg_list:
            m = seg_meta.get(sid)
            if not m:
                continue
            shape_id, start_m, end_m = m
            line = shapes_utm.get(shape_id)
            if line is None:
                continue
            sliced = substring(line, max(0.0, start_m), min(line.length, end_m))
            for x, y in sliced.coords:
                lon, lat = TO_WGS84.transform(x, y)
                pt = [round(lon, 6), round(lat, 6)]
                if not coords or coords[-1] != pt:
                    coords.append(pt)

    return {"type": "LineString", "coordinates": coords}


@router.get("/vehicles/live")
def get_live_vehicles(request: Request, route_id: str | None = None, limit: int = 500):
    telemetry = getattr(request.app.state, "telemetry", None)
    if telemetry is None:
        return {
            "status": "uninitialized",
            "count": 0,
            "vehicles": [],
        }
    vehicles = list(telemetry.live_vehicles.values())
    if route_id:
        vehicles = [v for v in vehicles if v["route_id"] == route_id]
    return {
        "status": telemetry.poll_status,
        "count": len(vehicles),
        "vehicles": vehicles[:limit],
    }
