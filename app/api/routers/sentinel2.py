from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.session import SessionLocal
from app.models.satellite_image import ProcessingStatus, SatelliteImage
from app.api.routers import compat as compat_store
from app.services.sentinel2.indices import INDEX_DEFINITIONS, normalize_index_key
from app.services.sentinel2.service import (
    DEFAULT_MAX_CLOUD,
    DB_CACHE_ENABLED,
    _json_safe,
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


def _get_owned_parcel(db: Session, parcel_id: UUID | str, current_user: dict[str, Any]) -> dict[str, Any]:
    """Return a parcel from the real Dataris storage used by this project.

    The production app stores frontend-compatible data in the compat JSON state
    table (dataris_compat_state), not in a normalized SQL table named
    `parcels`. The previous Sentinel-2 router queried the ORM Parcel model and
    failed in Cloud Run with: relation "parcels" does not exist.

    Reading from compat storage keeps this endpoint aligned with the parcels the
    user sees in the current frontend. The SQLAlchemy session is intentionally
    unused here; it is kept only so existing dependency injection and future DB
    cache support remain compatible.
    """
    _ = db
    wanted_id = str(parcel_id)
    user_id = _user_id(current_user)
    try:
        compat_db = compat_store.read_db()
        parcels = compat_store.table(compat_db, "parcels")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudieron leer los lotes guardados: {exc}") from exc

    parcel = next(
        (
            row
            for row in parcels
            if str(row.get("id")) == wanted_id
            and (not row.get("user_id") or str(row.get("user_id")) == user_id)
        ),
        None,
    )
    if not parcel:
        raise HTTPException(status_code=404, detail="Lote no encontrado o sin acceso")

    geometry = _parcel_geometry(parcel)
    if not geometry:
        raise HTTPException(status_code=422, detail="El lote no tiene geometría GeoJSON válida para Sentinel-2")

    normalized = dict(parcel)
    normalized["geometry"] = geometry
    return normalized


def _parcel_geometry(parcel: dict[str, Any]) -> Any:
    """Resolve all geometry keys produced by upload/import flows."""
    for key in ("geometry", "geometry_geojson", "geojson", "feature_collection", "featureCollection"):
        value = parcel.get(key)
        if value:
            return value
    return None


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
    if DB_CACHE_ENABLED:
        try:
            rows = db.query(SatelliteImage).filter(
                SatelliteImage.user_id == _user_id(current_user),
                SatelliteImage.parcel_id == parcel["id"],
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
        except Exception:
            db.rollback()
            db_dates = []

    try:
        catalog_dates = catalog_dates_for_geometry(parcel["geometry"], max_cloud=maxcc)
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
        parcel_id=str(parcel["id"]),
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
        parcel_geometry=parcel["geometry"],
        user_id=_user_id(current_user),
        parcel_id=str(parcel["id"]),
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


def _resolve_parcel_geometry_override(payload: dict[str, Any] | None) -> Any:
    if not payload:
        return None
    for key in ("geometry", "parcel_geometry", "geometry_geojson", "geojson", "feature_collection", "featureCollection"):
        value = payload.get(key)
        if value:
            return value
    parcel = payload.get("parcel")
    if isinstance(parcel, dict):
        return _parcel_geometry(parcel)
    return None


def _build_map_layer_response(
    *,
    db: Session,
    parcel: dict[str, Any],
    current_user: dict[str, Any],
    layer_key: Optional[str],
    wms_layer: Optional[str],
    date_param: Optional[str],
    width: int,
    height: int,
    maxcc: float,
    include_statistics: bool,
    auto_sync: bool,
    force_refresh: bool,
) -> dict[str, Any]:
    _ = include_statistics, auto_sync
    index_key = normalize_index_key(layer_key or wms_layer or "NDVI")
    if index_key not in INDEX_DEFINITIONS:
        raise HTTPException(status_code=422, detail=f"Índice no soportado: {index_key}")
    target_date = _parse_target_date(date_param)

    try:
        result = generate_or_get_layer(
            db,
            parcel_geometry=parcel["geometry"],
            user_id=_user_id(current_user),
            parcel_id=str(parcel["id"]),
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
        "id": f"sentinel2-{parcel.get('id')}-{index_key}-{result.get('date')}",
        "graniot_parcel_id": None,
        "image_url": result["image_url"],
        "bounds": result["bounds"],
        "date": result.get("date"),
        "layer": index_key,
        "source": result.get("source", "sentinel-2-l2a"),
    }
    return _json_safe({
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
    })


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
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    return _build_map_layer_response(
        db=db,
        parcel=parcel,
        current_user=current_user,
        layer_key=layer_key,
        wms_layer=wms_layer,
        date_param=date_param,
        width=width,
        height=height,
        maxcc=maxcc,
        include_statistics=include_statistics,
        auto_sync=auto_sync,
        force_refresh=force_refresh,
    )


@router.post("/parcels/{parcel_id}/ndvi/map-layer")
def map_layer_from_geometry(
    parcel_id: UUID,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    geometry_override = _resolve_parcel_geometry_override(payload)
    if geometry_override:
        # The frontend sends the exact currently selected geometry. This prevents
        # stale compat-storage geometries from generating a larger raster that
        # covers other lots.
        parcel["geometry"] = geometry_override

    return _build_map_layer_response(
        db=db,
        parcel=parcel,
        current_user=current_user,
        layer_key=payload.get("layer_key"),
        wms_layer=payload.get("wms_layer"),
        date_param=payload.get("date"),
        width=max(128, min(int(payload.get("width") or 1024), 2048)),
        height=max(128, min(int(payload.get("height") or 1024), 2048)),
        maxcc=float(payload.get("maxcc") or DEFAULT_MAX_CLOUD),
        include_statistics=bool(payload.get("include_statistics", True)),
        auto_sync=bool(payload.get("auto_sync", True)),
        force_refresh=bool(payload.get("force_refresh", False)),
    )


def _normalize_prefetch_layers(payload: dict[str, Any]) -> list[str]:
    raw_layers = payload.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        raw_layers = [payload.get("layer_key") or payload.get("wms_layer") or "NDVI"]
    normalized: list[str] = []
    for value in ["NDVI", *raw_layers]:
        key = normalize_index_key(value)
        if key in INDEX_DEFINITIONS and key not in normalized:
            normalized.append(key)
    return normalized[: int(payload.get("max_layers") or 4)]


def _collect_prefetch_parcels(payload: dict[str, Any], current_user: dict[str, Any], max_parcels: int) -> list[dict[str, Any]]:
    user_id = _user_id(current_user)
    parcel_payloads = payload.get("parcels") or payload.get("parcel_geometries") or []
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()

    if isinstance(parcel_payloads, list):
        for item in parcel_payloads:
            if not isinstance(item, dict):
                continue
            parcel_id = str(item.get("id") or item.get("parcel_id") or "").strip()
            geometry = _resolve_parcel_geometry_override(item)
            if not parcel_id or not geometry or parcel_id in seen:
                continue
            collected.append({"id": parcel_id, "geometry": geometry})
            seen.add(parcel_id)
            if len(collected) >= max_parcels:
                return collected

    requested_ids = {str(item) for item in (payload.get("parcel_ids") or []) if item}
    try:
        compat_db = compat_store.read_db()
        all_parcels = compat_store.table(compat_db, "parcels")
        for row in all_parcels:
            parcel_id = str(row.get("id") or "")
            if not parcel_id or parcel_id in seen:
                continue
            if row.get("user_id") and str(row.get("user_id")) != user_id:
                continue
            if requested_ids and parcel_id not in requested_ids:
                continue
            geometry = _parcel_geometry(row)
            if not geometry:
                continue
            collected.append(dict(row, geometry=geometry))
            seen.add(parcel_id)
            if len(collected) >= max_parcels:
                break
    except Exception as exc:
        if not collected:
            raise HTTPException(status_code=502, detail=f"No se pudieron consultar lotes para precache Sentinel-2: {exc}") from exc

    return collected


def _run_prefetch_job(
    *,
    parcels: list[dict[str, Any]],
    layers: list[str],
    user_id: str,
    target_date: date | None,
    width: int,
    height: int,
    max_cloud: float,
    delay_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    db = SessionLocal()
    try:
        for parcel in parcels:
            for layer_key in layers:
                try:
                    result = generate_or_get_layer(
                        db,
                        parcel_geometry=parcel["geometry"],
                        user_id=user_id,
                        parcel_id=str(parcel["id"]),
                        index_key=layer_key,
                        target_date=target_date,
                        max_cloud=max_cloud,
                        width=width,
                        height=height,
                    )
                    results.append({
                        "parcel_id": str(parcel["id"]),
                        "layer_key": layer_key,
                        "available": bool(result.get("available")),
                        "date": result.get("date"),
                    })
                except Exception as exc:
                    results.append({"parcel_id": str(parcel.get("id")), "layer_key": layer_key, "available": False, "error": str(exc)})
                if delay_seconds > 0:
                    sleep(delay_seconds)
    finally:
        db.close()
    return results


@router.post("/satellite/prefetch")
def prefetch(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    max_parcels = max(1, min(int(payload.get("max_parcels") or 8), 40))
    width = max(128, min(int(payload.get("width") or 1024), 2048))
    height = max(128, min(int(payload.get("height") or 1024), 2048))
    target_date = _parse_target_date(payload.get("date"))
    max_cloud = float(payload.get("maxcc") or DEFAULT_MAX_CLOUD)
    delay_seconds = max(0.0, min(float(payload.get("delay_seconds") or 0.35), 5.0))
    layers = _normalize_prefetch_layers(payload)
    parcels = _collect_prefetch_parcels(payload, current_user, max_parcels)
    background = bool(payload.get("background", True))

    if not parcels:
        return {"data": {"queued": False, "parcel_count": 0, "image_count": 0, "results": []}}

    if background:
        background_tasks.add_task(
            _run_prefetch_job,
            parcels=parcels,
            layers=layers,
            user_id=_user_id(current_user),
            target_date=target_date,
            width=width,
            height=height,
            max_cloud=max_cloud,
            delay_seconds=delay_seconds,
        )
        return _json_safe({
            "data": {
                "queued": True,
                "parcel_count": len(parcels),
                "layer_count": len(layers),
                "image_count": len(parcels) * len(layers),
                "layers": layers,
                "results": [],
            }
        })

    results = _run_prefetch_job(
        parcels=parcels,
        layers=layers,
        user_id=_user_id(current_user),
        target_date=target_date,
        width=width,
        height=height,
        max_cloud=max_cloud,
        delay_seconds=delay_seconds,
    )
    return _json_safe({
        "data": {
            "queued": False,
            "parcel_count": len(parcels),
            "layer_count": len(layers),
            "image_count": sum(1 for item in results if item.get("available")),
            "layers": layers,
            "results": results,
        }
    })


@router.get("/cache/{cache_key}.png")
def cache_png(cache_key: str) -> Response:
    path = local_png_path(cache_key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Imagen no encontrada en cache local")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=604800, immutable"})
