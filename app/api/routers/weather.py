import json
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException
import redis
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.task_weather import fetch_and_cache_parcel_weather
from app.models.parcel import Parcel
from app.services.weather.geocode import reverse_geocode
from app.services.weather.weather import get_weather
from app.utils.geo import get_centroid_from_geojson


REDIS_URL = os.getenv("REDIS_URL", "redis://default:dJhuLhWWu3qZeK2Ir6x9fGyhYD1dCdpn@redis-11931.c328.europe-west3-1.gce.cloud.redislabs.com:11931/0")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

router = APIRouter(prefix="/weather", tags=["Weather"])


@router.get("/parcel/{parcel_id}")
def get_weather_for_parcel(parcel_id: uuid.UUID, db: Session = Depends(get_db)):
    """
    Fetch weather and location info for a parcel directly.
    """
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if not parcel:
        raise HTTPException(status_code=404, detail="Parcel not found")

    try:
        lat, lon = get_centroid_from_geojson(parcel.geometry)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid parcel geometry: {e}")

    # Fetch weather and location info synchronously
    weather = get_weather(lat, lon)
    location_info = reverse_geocode(lat, lon)

    response = {
        "parcel_id": str(parcel_id),
        "location": {
            "latitude": lat,
            "longitude": lon,
            "city": location_info.get("city"),
            "country": location_info.get("country"),
            "town": location_info.get("town"),
            "village": location_info.get("village"),
            "municipality": location_info.get("municipality"),
            "county": location_info.get("county"),
            "state": location_info.get("state"),
            "postcode": location_info.get("postcode"),
        },
        "weather": weather
    }

    return response