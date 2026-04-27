import os
from celery import shared_task
from app.celery_app import celery_app
from app.api.deps import get_db
from app.db.session import SessionLocal
from app.models.parcel import Parcel
from app.services.weather.geocode import reverse_geocode
from app.services.weather.weather import get_weather
from app.utils.geo import get_centroid_from_geojson
import redis
import json
from datetime import timedelta
from sqlalchemy.orm import Session

# Reuse existing Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://default:dJhuLhWWu3qZeK2Ir6x9fGyhYD1dCdpn@redis-11931.c328.europe-west3-1.gce.cloud.redislabs.com:11931/0")
r = redis.Redis.from_url(REDIS_URL, decode_responses=False)# store raw bytes
CACHE_TTL = int(timedelta(hours=24).total_seconds())  # 86400

@celery_app.task(name="app.api.task_weather.fetch_and_cache_parcel_weather")
def fetch_and_cache_parcel_weather(parcel_id: str, user_id: str):
    db = SessionLocal()
    try:
        parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
        if not parcel:
            return None

        lat, lon = get_centroid_from_geojson(parcel.geometry)
        weather = get_weather(lat, lon)
        location_info = reverse_geocode(lat, lon)

        response = {
            "parcel_id": parcel_id,
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

        cache_key = f"user:{user_id}:parcel:{parcel_id}"
        r.setex(cache_key, CACHE_TTL, json.dumps(response).encode("utf-8"))
        return response
    finally:
        db.close()