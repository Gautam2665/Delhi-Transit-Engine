from fastapi import APIRouter
from backend.app.config import settings

router = APIRouter()

@router.get("/health")
def health_check():
    processed_files = {
        "segments": (settings.PROCESSED_DIR / "segments.parquet").exists(),
        "shapes": (settings.PROCESSED_DIR / "shapes_utm.parquet").exists(),
        "stops": (settings.PROCESSED_DIR / "stops_utm.parquet").exists(),
        "speed_profile": (settings.PROCESSED_DIR / "speed_profile.parquet").exists(),
    }
    
    daily_obs_count = len(list((settings.PROCESSED_DIR / "daily_segment_obs").glob("*.parquet"))) if (settings.PROCESSED_DIR / "daily_segment_obs").exists() else 0

    return {
        "status": "healthy",
        "service": settings.APP_NAME,
        "processed_artifacts": processed_files,
        "daily_observations_available": daily_obs_count,
    }
