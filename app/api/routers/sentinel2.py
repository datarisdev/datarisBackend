from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.parcel import Parcel
from app.models.satellite_image import ProcessingStatus, SatelliteImage
from app.services.sentinel2.indices import INDEX_DEFINITIONS, normalize_index_key
from app.services.sentinel2.service import (
    DEFAULT_MAX_CLOUD,
    catalog_dates_for_geometry,
    generate_or_get_layer,
    get_cached_db_image,
    image_url_from_object_path,
    list_index_layers,
    local_png_path,
)

router = APIRouter(prefix="/satellite-free", tags=["Sentinel-2 Free Satellite"])


def _user_id(current_user: dict[str, Any]) -> str:
    return str(current_user.get("id"))


def _get_owned_parcel(db: Session, parcel_id: UUID | str, current_user: dict[str, Any]) -> Parcel:
    parcel = db.query(Parcel).filter(
        Parcel.id == parcel_id,
        Parcel.user_id == _user_id(current_user),
    ).first()
    if not parcel:
        raise HTTPException(status_code=404, detail="Lote no encontrado o sin acceso")
    return parcel


def _parse_target_date(raw: str | None) -> date | None:
    if not raw or str(raw).strip().lower() in {"latest", "last", "none", "null", "undefined"}:
        return None
    try:
        return datetime.fromisoformat(str(raw)[:10]).date()
    except Exception:
        try:
            return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="Fecha inválida. Usa formato YYYY-MM-DD.") from exc


def _date_to_datetime_range(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


@router.get("/status")
def status() -> dict[str, Any]:
    return {
        "data": {
            "configured": True,
            "provider": "Sentinel-2 L2A STAC",
            "base_url": "STAC configurable por SENTINEL_STAC_URL",
            "auth_header": "none",
        }
    }


@router.get("/layers")
def layers(
    platform: bool = Query(True),
    refresh: bool = Query(False),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    # current_user is intentionally required so the catalog is only exposed to
    # authenticated users, matching the old Graniot UI flow.
    _ = platform, refresh, current_user
    data = list_index_layers()
    return {"data": data, "count": len(data)}


@router.get("/parcels/{parcel_id}/resolutions/{resolution_key}/dates")
def dates(
    parcel_id: UUID,
    resolution_key: str,
    maxcc: float = Query(DEFAULT_MAX_CLOUD),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    parcel = _get_owned_parcel(db, parcel_id, current_user)

    db_dates: list[dict[str, Any]] = []
    rows = db.query(SatelliteImage).filter(
        SatelliteImage.user_id == _user_id(current_user),
        SatelliteImage.parcel_id == parcel.id,
        SatelliteImage.processing_status == ProcessingStatus.completed,
    ).order_by(SatelliteImage.image_date.desc()).limit(60).all()
    for row in rows:
        db_dates.append({
            "date": row.image_date.date().isoformat(),
            "cloudCoverage": row.cloud_coverage,
            "cloud_coverage": row.cloud_coverage,
            "isLoaded": True,
            "source": "cache",
        })

    try:
        catalog_dates = catalog_dates_for_geometry(parcel.geometry, max_cloud=maxcc)
    except Exception as exc:
        if db_dates:
            return {"data": db_dates, "warning": f"Catálogo Sentinel-2 no disponible: {exc}"}
        raise HTTPException(status_code=502, detail=f"No se pudo consultar catálogo Sentinel-2: {exc}") from exc

    by_date: dict[str, dict[str, Any]] = {item["date"]: item for item in catalog_dates}
    for item in db_dates:
        existing = by_date.get(item["date"])
        by_date[item["date"]] = {**(existing or {}), **item, "isLoaded": True}

    data = sorted(by_date.values(), key=lambda item: item["date"], reverse=True)
    return {"data": data, "count": len(data), "resolution": resolution_key}


@router.get("/parcels/{parcel_id}/layers/{layer_key}/statistics")
def statistics(
    parcel_id: UUID,
    layer_key: str,
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    maxcc: float = Query(DEFAULT_MAX_CLOUD),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _ = from_date, maxcc
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    index_key = normalize_index_key(layer_key)
    target_date = _parse_target_date(to_date)

    cached = get_cached_db_image(
        db,
        user_id=_user_id(current_user),
        parcel_id=str(parcel.id),
        index_key=index_key,
        target_date=target_date,
    )
    if cached and cached.statistics:
        return {
            "data": {
                "status": "OK",
                "data": [
                    {
                        "date": cached.image_date.date().isoformat(),
                        "cloud_coverage": cached.cloud_coverage,
                        "basicStats": [cached.statistics],
                    }
                ],
                **cached.statistics,
            }
        }

    result = generate_or_get_layer(
        db,
        parcel_geometry=parcel.geometry,
        user_id=_user_id(current_user),
        parcel_id=str(parcel.id),
        index_key=index_key,
        target_date=target_date,
        max_cloud=maxcc,
        width=1024,
        height=1024,
    )
    if not result.get("available"):
        return {"data": {"status": "NO_DATA", "data": [], "reason": result.get("reason")}}
    stats = result.get("statistics") or {}
    return {
        "data": {
            "status": "OK",
            "data": [
                {
                    "date": result.get("date"),
                    "cloud_coverage": result.get("cloud_coverage"),
                    "basicStats": [stats],
                }
            ],
            **stats,
        }
    }


@router.get("/parcels/{parcel_id}/ndvi/map-layer")
def map_layer(
    parcel_id: UUID,
    layer_key: Optional[str] = Query(None),
    wms_layer: Optional[str] = Query(None),
    date_param: Optional[str] = Query(None, alias="date"),
    width: int = Query(1024, ge=128, le=2048),
    height: int = Query(1024, ge=128, le=2048),
    maxcc: float = Query(DEFAULT_MAX_CLOUD),
    include_statistics: bool = Query(True),
    auto_sync: bool = Query(True),
    force_refresh: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _ = include_statistics, auto_sync
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    index_key = normalize_index_key(layer_key or wms_layer or "NDVI")
    if index_key not in INDEX_DEFINITIONS:
        raise HTTPException(status_code=422, detail=f"Índice no soportado: {index_key}")
    target_date = _parse_target_date(date_param)

    try:
        result = generate_or_get_layer(
            db,
            parcel_geometry=parcel.geometry,
            user_id=_user_id(current_user),
            parcel_id=str(parcel.id),
            index_key=index_key,
            target_date=target_date,
            max_cloud=maxcc,
            width=width,
            height=height,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo generar la capa Sentinel-2: {exc}") from exc

    if not result.get("available"):
        return {
            "data": {
                "available": False,
                "reason": result.get("reason") or "No hay imagen disponible",
                "requires_sync": False,
                "date": None,
                "layer": {"key": index_key, "wms_layer": index_key, "source": "sentinel-2-l2a"},
                "overlays": [],
                "statistics": None,
                "warnings": [result.get("reason") or "No hay imagen disponible"],
                "source_count": 0,
            }
        }

    overlay = {
        "id": f"sentinel2-{parcel.id}-{index_key}-{result.get('date')}",
        "graniot_parcel_id": None,
        "image_url": result["image_url"],
        "bounds": result["bounds"],
        "date": result.get("date"),
        "layer": index_key,
        "source": result.get("source", "sentinel-2-l2a"),
    }
    return {
        "data": {
            "available": True,
            "reason": None,
            "requires_sync": False,
            "date": result.get("date"),
            "layer": {
                "key": index_key,
                "wms_layer": index_key,
                "resolution_key": INDEX_DEFINITIONS[index_key].resolution,
                "resolution_label": INDEX_DEFINITIONS[index_key].resolution,
                "source": "sentinel-2-l2a",
                "family": "Sentinel-2",
                "auto_selected": target_date is None,
            },
            "overlays": [overlay],
            "statistics": {
                "status": "OK",
                "data": [
                    {
                        "date": result.get("date"),
                        "cloud_coverage": result.get("cloud_coverage"),
                        "basicStats": [result.get("statistics") or {}],
                    }
                ],
                **(result.get("statistics") or {}),
            },
            "warnings": [],
            "source_count": 1,
        }
    }


@router.post("/satellite/prefetch")
def prefetch(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    parcel_ids = payload.get("parcel_ids") or []
    max_parcels = int(payload.get("max_parcels") or 8)
    layer_key = normalize_index_key(payload.get("layer_key") or payload.get("wms_layer") or "NDVI")
    target_date = _parse_target_date(payload.get("date"))
    width = int(payload.get("width") or 1024)
    height = int(payload.get("height") or 1024)

    query = db.query(Parcel).filter(Parcel.user_id == _user_id(current_user))
    if parcel_ids:
        query = query.filter(Parcel.id.in_(parcel_ids))
    parcels = query.limit(max_parcels).all()

    results: list[dict[str, Any]] = []
    for parcel in parcels:
        try:
            result = generate_or_get_layer(
                db,
                parcel_geometry=parcel.geometry,
                user_id=_user_id(current_user),
                parcel_id=str(parcel.id),
                index_key=layer_key,
                target_date=target_date,
                width=width,
                height=height,
            )
            results.append({"parcel_id": str(parcel.id), "available": bool(result.get("available")), "date": result.get("date")})
        except Exception as exc:
            results.append({"parcel_id": str(parcel.id), "available": False, "error": str(exc)})

    return {
        "data": {
            "queued": False,
            "parcel_count": len(parcels),
            "image_count": sum(1 for item in results if item.get("available")),
            "results": results,
        }
    }


@router.get("/cache/{cache_key}.png")
def cache_png(cache_key: str) -> Response:
    path = local_png_path(cache_key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Imagen no encontrada en cache local")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=604800, immutable"})
