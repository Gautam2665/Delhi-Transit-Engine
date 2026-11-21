from pathlib import Path
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_NAME: str = "Delhi Transit Engine"
    API_V1_STR: str = "/api/v1"
    
    # Paths relative to project root
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    PROCESSED_DIR: Path = DATA_DIR / "processed"
    GTFS_DIR: Path = DATA_DIR / "gtfs_static"
    
    # Transport Stack Realtime Feed
    GTFS_RT_URL: str = "https://delhi.transportstack.in/api/dataset/otd/get-file?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb"
    GTFS_RT_API_KEY: str = ""

    class Config:
        env_file = ".env"

settings = Settings()
