from fastapi import APIRouter, Query, HTTPException
from typing import Optional, List
import pandas as pd
import numpy as np
from pyproj import Transformer
from backend.app.config import settings

router = APIRouter()

_stops_df: Optional[pd.DataFrame] = None
to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)

def get_stops_df() -> pd.DataFrame:
    global _stops_df
    if _stops_df is None:
        parquet_path = settings.PROCESSED_DIR / "stops_utm.parquet"
        if parquet_path.exists():
            _stops_df = pd.read_parquet(parquet_path)
        else:
            txt_path = settings.GTFS_DIR / "stops.txt"
            if txt_path.exists():
                _stops_df = pd.read_csv(txt_path, dtype={"stop_id": str})
                sx, sy = to_utm.transform(_stops_df["stop_lon"].values, _stops_df["stop_lat"].values)
                _stops_df["x"] = sx
                _stops_df["y"] = sy
            else:
                raise HTTPException(status_code=500, detail="Stops dataset not found")
    return _stops_df

@router.get("/stops")
def search_stops(
    q: Optional[str] = Query(None, description="Search term for stop name or code"),
    lat: Optional[float] = Query(None, description="Origin latitude for proximity search"),
    lon: Optional[float] = Query(None, description="Origin longitude for proximity search"),
    limit: int = Query(10, ge=1, le=100, description="Max results to return"),
):
    df = get_stops_df()

    if lat is not None and lon is not None:
        # Spatial nearest stops search via metric UTM coordinates
        x, y = to_utm.transform(lon, lat)
        dists = np.sqrt((df["x"] - x) ** 2 + (df["y"] - y) ** 2)
        top_idx = dists.nsmallest(limit).index
        result = df.loc[top_idx].copy()
        result["distance_m"] = dists.loc[top_idx].round(1)
        return result[["stop_id", "stop_name", "stop_lat", "stop_lon", "distance_m"]].to_dict(orient="records")

    if q:
        mask = df["stop_name"].str.contains(q, case=False, na=False) | df["stop_code"].astype(str).str.contains(q, case=False, na=False)
        matched = df[mask].head(limit)
        return matched[["stop_id", "stop_name", "stop_lat", "stop_lon"]].to_dict(orient="records")

    return df.head(limit)[["stop_id", "stop_name", "stop_lat", "stop_lon"]].to_dict(orient="records")
