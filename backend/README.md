# Backend Service: Delhi Transit Engine

FastAPI routing service and background GTFS-RT telemetry poller.

## Architecture
- **Framework**: FastAPI (Async Python)
- **Database**: PostgreSQL with PostGIS
- **Cache & Telemetry State**: Redis
- **Endpoints**:
  - `GET /journeys?from={lat,lon}&to={lat,lon}&depart={iso_timestamp}`
  - `GET /routes/{route_id}`
  - `GET /live/vehicles`
  - `GET /segments/{segment_id}/speed`
