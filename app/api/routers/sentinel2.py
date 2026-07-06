from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from pathlib import Path
from time import sleep
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.session import SessionLocal
from app.models.satellite_image import ProcessingStatus, SatelliteImage
from app.api.routers import compat as compat_store
from app.services.sentinel2.history import (
    list_satellite_analysis_history,
    list_satellite_comparison_history,
    persist_satellite_analysis_record,
    persist_satellite_comparison_record,
    summarize_satellite_comparison_history,
)
from app.services.sentinel2.catalog import available_dates
from app.services.sentinel2.indices import INDEX_DEFINITIONS, normalize_index_key
from app.utils.storage_satellite import download_satellite_cache_png_bytes, safe_satellite_cache_key
from app.services.sentinel2.service import (
    DEFAULT_MAX_CLOUD,
    DB_CACHE_ENABLED,
    _json_safe,
    bbox_from_geometry,
    catalog_dates_for_geometry,
    generate_or_get_layer,
    get_cached_db_image,
    image_url_from_object_path,
    list_index_layers,
    local_png_path,
)

router = APIRouter(prefix="/satellite-free", tags=["Sentinel-2 Free Satellite"])
logger = logging.getLogger(__name__)


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


def _persist_generated_layer(
    *,
    parcel: dict[str, Any],
    current_user_id: str,
    index_key: str,
    result: dict[str, Any],
    requested_date: date | str | None = None,
    warnings: Optional[list[str]] = None,
    source: str = "sentinel2-map-layer",
) -> Optional[dict[str, Any]]:
    """Register one usable Sentinel-2 render in the shared persisted history.

    Image generation must not fail merely because history persistence is
    temporarily unavailable. The layer remains usable and the retry path can
    store it during the next request or background prefetch.
    """
    if not result.get("available"):
        return None
    try:
        return persist_satellite_analysis_record(
            user_id=current_user_id,
            parcel=parcel,
            index_key=index_key,
            result=result,
            requested_date=str(requested_date)[:10] if requested_date else None,
            warnings=warnings,
            source=source,
        )
    except Exception:
        logger.exception(
            "No se pudo persistir historial Sentinel-2",
            extra={"parcel_id": str(parcel.get("id") or ""), "index_key": index_key},
        )
        return None


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


@router.get("/history")
def history(
    parcel_id: Optional[str] = Query(None),
    index_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    data = list_satellite_comparison_history(
        user_id=_user_id(current_user),
        parcel_id=parcel_id,
        index_type=index_type,
        limit=limit,
    )
    return {"data": data, "count": len(data)}


@router.post("/history/comparisons")
def save_comparison(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    parcel_id = str(payload.get("parcel_id") or "").strip()
    if not parcel_id:
        raise HTTPException(status_code=422, detail="Selecciona un lote antes de guardar la comparación.")
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    left = payload.get("left")
    right = payload.get("right")
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise HTTPException(status_code=422, detail="La comparación debe incluir las dos imágenes revisadas.")
    record = persist_satellite_comparison_record(
        user_id=_user_id(current_user),
        parcel=parcel,
        left=left,
        right=right,
        max_cloud_coverage=payload.get("max_cloud_coverage"),
    )
    if not record:
        raise HTTPException(status_code=422, detail="Las dos imágenes deben estar disponibles antes de guardar la comparación.")
    return {"data": record}


@router.get("/history/summary")
def history_summary(
    parcel_id: Optional[str] = Query(None),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return {"data": summarize_satellite_comparison_history(user_id=_user_id(current_user), parcel_id=parcel_id)}


@router.get("/parcels/{parcel_id}/history")
def parcel_history(
    parcel_id: UUID,
    index_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _get_owned_parcel(db, parcel_id, current_user)
    data = list_satellite_comparison_history(
        user_id=_user_id(current_user),
        parcel_id=str(parcel_id),
        index_type=index_type,
        limit=limit,
    )
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
    for row in list_satellite_analysis_history(user_id=_user_id(current_user), parcel_id=str(parcel["id"]), limit=100):
        if not row.get("image_date"):
            continue
        db_dates.append({
            "date": str(row.get("image_date"))[:10],
            "cloudCoverage": row.get("cloud_coverage"),
            "cloud_coverage": row.get("cloud_coverage"),
            "isLoaded": True,
            "source": "history",
        })

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
        width=768,
        height=768,
    )
    if not result.get("available"):
        return {"data": {"status": "NO_DATA", "data": [], "reason": result.get("reason")}}
    _persist_generated_layer(
        parcel=parcel,
        current_user_id=_user_id(current_user),
        index_key=index_key,
        result=result,
        requested_date=target_date,
        source="sentinel2-statistics",
    )
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


@router.get("/parcels/{parcel_id}/layers/{layer_key}/series")
def statistics_series(
    parcel_id: UUID,
    layer_key: str,
    maxcc: float = Query(DEFAULT_MAX_CLOUD),
    limit: int = Query(6, ge=2, le=12),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return a real Sentinel-2 temporal series for charts.

    Each point is calculated from the selected lot geometry and a real catalog
    scene. The limit stays intentionally small because the first request may need
    to read remote Sentinel bands; subsequent requests use the generated cache.
    """
    parcel = _get_owned_parcel(db, parcel_id, current_user)
    index_key = normalize_index_key(layer_key)
    if index_key not in INDEX_DEFINITIONS:
        raise HTTPException(status_code=422, detail=f"Índice no soportado: {index_key}")

    try:
        date_items = catalog_dates_for_geometry(parcel["geometry"], max_cloud=maxcc)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo consultar catálogo Sentinel-2: {exc}") from exc

    points: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_dates: set[str] = set()

    for item in date_items:
        raw_date = str(item.get("date") or "")[:10]
        if not raw_date or raw_date in seen_dates:
            continue
        try:
            result = generate_or_get_layer(
                db,
                parcel_geometry=parcel["geometry"],
                user_id=_user_id(current_user),
                parcel_id=str(parcel["id"]),
                index_key=index_key,
                target_date=_parse_target_date(raw_date),
                max_cloud=maxcc,
                width=768,
                height=768,
            )
            if not result.get("available"):
                continue
            _persist_generated_layer(
                parcel=parcel,
                current_user_id=_user_id(current_user),
                index_key=index_key,
                result=result,
                requested_date=raw_date,
                source="sentinel2-series",
            )
            result_date = str(result.get("date") or raw_date)[:10]
            if result_date in seen_dates:
                continue
            seen_dates.add(result_date)
            points.append({
                "date": result_date,
                "cloud_coverage": result.get("cloud_coverage"),
                "statistics": result.get("statistics") or {},
                "source": result.get("source"),
                "coverage_percent": result.get("coverage_percent"),
                "unavailable_percent": result.get("unavailable_percent"),
                "is_partial": bool(result.get("is_partial", False)),
                "quality_placeholder_applied": bool(result.get("quality_placeholder_applied", False)),
                "quality_mask_applied": bool(result.get("quality_mask_applied", False)),
                "source_count": result.get("source_count", 1),
            })
            if len(points) >= limit:
                break
        except Exception as exc:
            warnings.append(f"{raw_date}: {exc}")

    points.sort(key=lambda point: str(point.get("date") or ""))
    return _json_safe({
        "data": {
            "status": "OK" if points else "NO_DATA",
            "layer": index_key,
            "points": points,
            "warnings": warnings[:4],
        }
    })


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
    revalidate_latest: bool = False,
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
            revalidate_latest=revalidate_latest,
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

    coverage_percent = result.get("coverage_percent")
    unavailable_percent = result.get("unavailable_percent")
    is_partial = bool(result.get("is_partial", False))
    quality_placeholder_applied = bool(result.get("quality_placeholder_applied", False))
    quality_mask_applied = bool(result.get("quality_mask_applied", False))
    warnings = list(result.get("warnings") or [])
    if is_partial:
        coverage_label = f"{float(coverage_percent or 0):.2f}%"
        unavailable_label = f"{float(unavailable_percent or 0):.2f}%"
        warnings.insert(
            0,
            f"Cobertura utilizable parcial: {coverage_label} del lote contiene píxeles Sentinel-2 válidos. "
            f"El {unavailable_label} restante se muestra en gris y no participa en las estadísticas.",
        )

    persisted = _persist_generated_layer(
        parcel=parcel,
        current_user_id=_user_id(current_user),
        index_key=index_key,
        result=result,
        requested_date=target_date,
        warnings=warnings,
    )

    overlay = {
        "id": f"sentinel2-{parcel.get('id')}-{index_key}-{result.get('date')}",
        "graniot_parcel_id": None,
        "image_url": result["image_url"],
        "bounds": result["bounds"],
        "date": result.get("date"),
        "layer": index_key,
        "source": result.get("source", "sentinel-2-l2a-mosaic"),
        "coverage_percent": coverage_percent,
        "unavailable_percent": unavailable_percent,
        "is_partial": is_partial,
        "quality_placeholder_applied": quality_placeholder_applied,
        "quality_mask_applied": quality_mask_applied,
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
            "warnings": warnings,
            "source_count": result.get("source_count", 1),
            "scene_ids": result.get("scene_ids") or [],
            "coverage_percent": coverage_percent,
            "unavailable_percent": unavailable_percent,
            "is_partial": is_partial,
            "quality_placeholder_applied": quality_placeholder_applied,
            "quality_mask_applied": quality_mask_applied,
            "processing_version": result.get("processing_version"),
            "history_id": (persisted or {}).get("id"),
            "analysis_key": (persisted or {}).get("analysis_key"),
        }
    })


@router.get("/parcels/{parcel_id}/ndvi/map-layer")
def map_layer(
    parcel_id: UUID,
    layer_key: Optional[str] = Query(None),
    wms_layer: Optional[str] = Query(None),
    date_param: Optional[str] = Query(None, alias="date"),
    width: int = Query(2048, ge=128, le=4096),
    height: int = Query(2048, ge=128, le=4096),
    maxcc: float = Query(DEFAULT_MAX_CLOUD),
    include_statistics: bool = Query(True),
    auto_sync: bool = Query(True),
    force_refresh: bool = Query(False),
    revalidate_latest: bool = Query(False),
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
        revalidate_latest=revalidate_latest,
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
        width=max(128, min(int(payload.get("width") or 2048), 4096)),
        height=max(128, min(int(payload.get("height") or 2048), 4096)),
        maxcc=float(payload.get("maxcc") or DEFAULT_MAX_CLOUD),
        include_statistics=bool(payload.get("include_statistics", True)),
        auto_sync=bool(payload.get("auto_sync", True)),
        force_refresh=bool(payload.get("force_refresh", False)),
        revalidate_latest=bool(payload.get("revalidate_latest", False)),
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
                    persisted = _persist_generated_layer(
                        parcel=parcel,
                        current_user_id=user_id,
                        index_key=layer_key,
                        result=result,
                        requested_date=target_date,
                        source="sentinel2-prefetch",
                    )
                    results.append({
                        "parcel_id": str(parcel["id"]),
                        "layer_key": layer_key,
                        "available": bool(result.get("available")),
                        "date": result.get("date"),
                        "history_id": (persisted or {}).get("id"),
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
    width = max(128, min(int(payload.get("width") or 2048), 4096))
    height = max(128, min(int(payload.get("height") or 2048), 4096))
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


def _satellite_png_headers(cache_key: str, origin: str | None = None) -> dict[str, str]:
    # These URLs are content-addressed: changing lot, geometry, layer, date, size
    # or render version produces a different hash. The browser can therefore keep
    # a generated PNG for a long time without hiding newly generated imagery.
    allowed_origin = origin if origin == "https://app.dataris.es" else "https://app.dataris.es"
    safe_key = safe_satellite_cache_key(cache_key)
    return {
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": f'"sentinel2-{safe_key}"',
        "Access-Control-Allow-Origin": allowed_origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Expose-Headers": "*",
        "Vary": "Origin",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "X-Content-Type-Options": "nosniff",
    }


def _cache_png_response(cache_key: str, request: Request, *, head_only: bool = False) -> Response:
    headers = _satellite_png_headers(cache_key, request.headers.get("origin"))
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    path = local_png_path(cache_key)
    if path.exists():
        if head_only:
            headers["Content-Length"] = str(path.stat().st_size)
            return Response(status_code=200, media_type="image/png", headers=headers)
        return FileResponse(path, media_type="image/png", headers=headers)

    try:
        content = download_satellite_cache_png_bytes(cache_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Imagen no encontrada en cache Sentinel-2/GCS") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo leer cache Sentinel-2/GCS: {exc}") from exc

    headers["Content-Length"] = str(len(content))
    if head_only:
        return Response(status_code=200, media_type="image/png", headers=headers)
    return Response(content=content, media_type="image/png", headers=headers)


@router.get("/cache/{cache_key}.png")
def cache_png(cache_key: str, request: Request) -> Response:
    return _cache_png_response(cache_key, request)


@router.head("/cache/{cache_key}.png")
def cache_png_head(cache_key: str, request: Request) -> Response:
    return _cache_png_response(cache_key, request, head_only=True)


# ---------------------------------------------------------------------------
# Commercial demo endpoint — fixed geometry, no auth, persistent GCS cache
# ---------------------------------------------------------------------------

# Salgado area, Veracruz, Mexico  (same bounds as /public/salgado-sentinel.png)
_DEMO_PARCEL_GEOMETRY: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [[
        [-96.248297, 18.575850],
        [-96.178140, 18.575850],
        [-96.178140, 18.617478],
        [-96.248297, 18.617478],
        [-96.248297, 18.575850],
    ]],
}
_DEMO_USER_ID = "commercial-demo"
_DEMO_PARCEL_ID = "commercial-demo-salgado"

# The commercial demo must always show a clean, impressive image — never "the most
# recent scene", which can be cloudy. Instead of a hardcoded date, scan the whole
# month asked for by product (June 2026) once via the cheap STAC catalog query
# (cloud-cover metadata only, no raster rendering), then actually render only the
# handful of lowest-cloud candidates and keep whichever produces the HIGHEST real
# pixel coverage over the demo parcel — cheap catalog cloud % is a scene-wide
# estimate and can disagree with the actual coverage over this specific small bbox.
_DEMO_COVERAGE_WINDOW_START = date(2026, 6, 1)
_DEMO_COVERAGE_WINDOW_END = date(2026, 6, 30)
_DEMO_CANDIDATE_LIMIT = 6
_DEMO_GOOD_ENOUGH_COVERAGE_PERCENT = 99.0


def _demo_best_candidate_dates() -> list[date]:
    """Cheapest-first shortlist of June-2026 dates for the demo parcel, ranked by
    catalog-reported cloud cover (no raster rendering — pure STAC metadata)."""
    bbox = bbox_from_geometry(_DEMO_PARCEL_GEOMETRY)
    candidates = available_dates(
        bbox=bbox,
        start_date=_DEMO_COVERAGE_WINDOW_START,
        end_date=_DEMO_COVERAGE_WINDOW_END,
        max_cloud=100.0,
        limit=140,
    )
    candidates.sort(key=lambda item: item.get("cloudCoverage") if item.get("cloudCoverage") is not None else 999.0)
    in_window = [
        date.fromisoformat(item["date"])
        for item in candidates
        if _DEMO_COVERAGE_WINDOW_START <= date.fromisoformat(item["date"]) <= _DEMO_COVERAGE_WINDOW_END
    ]
    return in_window[:_DEMO_CANDIDATE_LIMIT]


@router.get("/demo/map-layer")
def demo_map_layer(
    layer_key: str = Query("NDVI"),
    force_refresh: bool = Query(False),
    width: int = Query(2048, ge=128, le=4096),
    height: int = Query(2048, ge=128, le=4096),
) -> dict[str, Any]:
    """Real Sentinel-2 layer for the commercial demo, persistently cached in GCS.

    No authentication required. The geometry is hardcoded (Salgado area, Veracruz)
    so the backend can cache the raster permanently and serve it in milliseconds
    on every subsequent request. The same pipeline as production is used: STAC
    catalog → band download → index formula → geometry mask → PNG render.
    """
    index_key = normalize_index_key(layer_key)
    if index_key not in INDEX_DEFINITIONS:
        raise HTTPException(status_code=422, detail=f"Índice no soportado: {index_key}")

    db = SessionLocal()
    try:
        best_result: dict[str, Any] | None = None
        best_coverage = -1.0
        for candidate_date in _demo_best_candidate_dates():
            candidate_result = generate_or_get_layer(
                db,
                parcel_geometry=_DEMO_PARCEL_GEOMETRY,
                user_id=_DEMO_USER_ID,
                parcel_id=_DEMO_PARCEL_ID,
                index_key=index_key,
                target_date=candidate_date,
                max_cloud=100.0,
                width=width,
                height=height,
                force_refresh=force_refresh,
            )
            if not candidate_result.get("available"):
                continue

            coverage = candidate_result.get("coverage_percent") or 0.0
            if coverage > best_coverage:
                best_coverage = coverage
                best_result = candidate_result
            if best_coverage >= _DEMO_GOOD_ENOUGH_COVERAGE_PERCENT:
                break

        result = best_result or {"available": False, "reason": "Sin imagen satelital de junio 2026 disponible para el área demo"}
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo generar la capa demo Sentinel-2: {exc}",
        ) from exc
    finally:
        db.close()

    if not result.get("available"):
        return _json_safe({
            "data": {
                "available": False,
                "reason": result.get("reason") or "No hay imagen satelital para el área demo",
                "requires_sync": False,
                "date": None,
                "layer": {"key": index_key, "wms_layer": index_key, "source": "sentinel-2-l2a"},
                "overlays": [],
                "statistics": None,
                "warnings": [result.get("reason") or "Sin imagen disponible"],
                "source_count": 0,
            }
        })

    overlay = {
        "id": f"sentinel2-demo-{index_key}-{result.get('date')}",
        "graniot_parcel_id": None,
        "image_url": result["image_url"],
        "bounds": result["bounds"],
        "date": result.get("date"),
        "layer": index_key,
        "source": result.get("source", "sentinel-2-l2a-mosaic"),
        "coverage_percent": result.get("coverage_percent"),
        "unavailable_percent": result.get("unavailable_percent"),
        "is_partial": bool(result.get("is_partial", False)),
        "quality_placeholder_applied": bool(result.get("quality_placeholder_applied", False)),
        "quality_mask_applied": bool(result.get("quality_mask_applied", False)),
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
                "source": "sentinel-2-l2a",
                "family": "Sentinel-2",
                "auto_selected": True,
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
            "warnings": list(result.get("warnings") or []),
            "source_count": result.get("source_count", 1),
            "scene_ids": result.get("scene_ids") or [],
            "coverage_percent": result.get("coverage_percent"),
            "unavailable_percent": result.get("unavailable_percent"),
            "is_partial": bool(result.get("is_partial", False)),
            "processing_version": result.get("processing_version"),
        }
    })
