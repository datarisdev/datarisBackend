from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from shapely.geometry import GeometryCollection, shape
from shapely.ops import unary_union
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.api.routers import compat as compat_store
from app.services.weather.geocode import reverse_geocode
from app.services.weather.weather import get_weather

router = APIRouter(prefix="/weather", tags=["Weather"])


def _user_id(current_user: dict[str, Any]) -> str:
    return str(current_user.get("id"))


def _parcel_geometry(parcel: dict[str, Any]) -> Any:
    for key in ("geometry", "geometry_geojson", "geojson", "feature_collection", "featureCollection"):
        value = parcel.get(key)
        if value:
            return value
    return None


def _owned_parcel(parcel_id: uuid.UUID | str, current_user: dict[str, Any]) -> dict[str, Any]:
    wanted_id = str(parcel_id)
    user_id = _user_id(current_user)
    try:
        rows = compat_store.table(compat_store.read_db(), "parcels")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudieron leer los lotes guardados: {exc}") from exc

    parcel = next(
        (
            row
            for row in rows
            if str(row.get("id")) == wanted_id
            and (not row.get("user_id") or str(row.get("user_id")) == user_id)
        ),
        None,
    )
    if not parcel:
        raise HTTPException(status_code=404, detail="Lote no encontrado o sin acceso")
    return parcel


def _centroid_from_geometry(value: Any) -> tuple[float, float]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("La geometría no tiene formato GeoJSON")

    geometry_type = value.get("type")
    if geometry_type == "FeatureCollection":
        geometries = [
            shape(feature.get("geometry"))
            for feature in value.get("features", [])
            if isinstance(feature, dict) and feature.get("geometry")
        ]
        geometry = unary_union(geometries) if geometries else GeometryCollection()
    elif geometry_type == "Feature":
        geometry = shape(value.get("geometry") or {})
    else:
        geometry = shape(value)

    if geometry.is_empty:
        raise ValueError("La geometría del lote está vacía")
    centroid = geometry.centroid
    return float(centroid.y), float(centroid.x)


@router.get("/parcel/{parcel_id}")
def get_weather_for_parcel(
    parcel_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return real weather for the centroid of a parcel owned by the user."""
    _ = db
    parcel = _owned_parcel(parcel_id, current_user)
    geometry = _parcel_geometry(parcel)
    if not geometry:
        raise HTTPException(status_code=422, detail="El lote no tiene geometría GeoJSON válida")

    try:
        lat, lon = _centroid_from_geometry(geometry)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Geometría inválida: {exc}") from exc

    try:
        weather = get_weather(lat, lon)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo consultar el pronóstico meteorológico: {exc}") from exc

    try:
        location_info = reverse_geocode(lat, lon)
    except Exception:
        # Weather is still useful if the optional reverse-geocoding service is
        # unavailable or rate-limited.
        location_info = {}

    return {
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
        "weather": weather,
    }
