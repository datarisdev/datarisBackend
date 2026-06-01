from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.routers.compat import read_db, table

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

MONITORING_DAYS = 30


def _user_id_from_current(current_user: Any) -> str:
    if isinstance(current_user, dict):
        return str(current_user.get("id") or current_user.get("sub") or "")
    return str(current_user or "")


def _as_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _iso(value: Any) -> Optional[str]:
    dt = _as_datetime(value)
    return dt.isoformat() if dt else None


def _date_label(value: Any) -> str:
    dt = _as_datetime(value)
    if not dt:
        return "Sin fecha"
    return dt.strftime("%d/%m/%Y")


def _float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except Exception:
        return None
    return None


def _first_number(*values: Any) -> Optional[float]:
    for value in values:
        number = _float(value)
        if number is not None:
            return number
    return None


def _nested_get(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _statistics(row: Dict[str, Any]) -> Dict[str, Any]:
    stats = row.get("statistics") or row.get("stats") or row.get("result_statistics") or {}
    return stats if isinstance(stats, dict) else {}


def _ndvi_mean(row: Dict[str, Any]) -> Optional[float]:
    stats = _statistics(row)
    candidates = [
        row.get("ndvi_mean"),
        row.get("mean_ndvi"),
        row.get("average_ndvi"),
        row.get("avg_ndvi"),
        stats.get("ndvi_mean"),
        stats.get("mean_ndvi"),
        stats.get("average_ndvi"),
        stats.get("avg_ndvi"),
        stats.get("mean"),
        _nested_get(stats, "NDVI", "mean"),
        _nested_get(stats, "ndvi", "mean"),
    ]
    return _first_number(*candidates)


def _area_from_row(row: Dict[str, Any]) -> float:
    return _first_number(
        row.get("area"),
        row.get("area_ha"),
        row.get("total_area"),
        row.get("covered_area"),
        row.get("covered_area_ha"),
        row.get("hectares"),
        row.get("hectareas"),
        _nested_get(row, "metrics", "area"),
        _nested_get(row, "metrics", "area_ha"),
        _nested_get(row, "summary", "area"),
        _nested_get(row, "summary", "area_ha"),
    ) or 0.0


def _has_geometry(row: Dict[str, Any]) -> bool:
    geometry = row.get("geometry_geojson") or row.get("geojson") or row.get("feature_collection") or row.get("geometry")
    bbox = row.get("bbox") or row.get("bounds") or row.get("geometry_bounds")
    if isinstance(geometry, dict):
        if geometry.get("type") == "FeatureCollection":
            return bool(geometry.get("features"))
        if geometry.get("type") in {"Feature", "Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point"}:
            return True
    if isinstance(geometry, str) and geometry.strip():
        return True
    if isinstance(bbox, list) and len(bbox) >= 4:
        return all(_float(v) is not None for v in bbox[:4])
    if isinstance(bbox, dict):
        return all(_float(bbox.get(k)) is not None for k in ("south", "north", "west", "east"))
    return False


def _bounds(row: Dict[str, Any]) -> Optional[Dict[str, float]]:
    raw = row.get("geometry_bounds") or row.get("bounds") or row.get("bbox")
    if isinstance(raw, dict):
        south = _float(raw.get("south"))
        north = _float(raw.get("north"))
        west = _float(raw.get("west"))
        east = _float(raw.get("east"))
    elif isinstance(raw, list) and len(raw) >= 4:
        west, south, east, north = [_float(v) for v in raw[:4]]
    else:
        return None
    if None in {south, north, west, east}:
        return None
    if south is not None and north is not None and south > north:
        south, north = north, south
    if west is not None and east is not None and west > east:
        west, east = east, west
    return {"south": south, "north": north, "west": west, "east": east}  # type: ignore[dict-item]


def _center(row: Dict[str, Any]) -> Optional[Dict[str, float]]:
    raw = row.get("geometry_center") or row.get("center")
    if isinstance(raw, dict):
        lat = _float(raw.get("lat"))
        lng = _float(raw.get("lng"))
        if lat is not None and lng is not None:
            return {"lat": lat, "lng": lng}
    bounds = _bounds(row)
    if bounds:
        return {
            "lat": (bounds["south"] + bounds["north"]) / 2,
            "lng": (bounds["west"] + bounds["east"]) / 2,
        }
    return None


def _geometry(row: Dict[str, Any]) -> Any:
    return row.get("geometry_geojson") or row.get("geojson") or row.get("feature_collection") or row.get("geometry")


def _aircraft_type(row: Dict[str, Any]) -> str:
    raw = str(
        row.get("aircraft_type")
        or row.get("aircraftType")
        or row.get("vehicle_type")
        or row.get("tipo")
        or row.get("type")
        or _nested_get(row, "metadata", "aircraftType")
        or "otro"
    ).strip().lower()
    if raw in {"drone", "dron", "drones"}:
        return "drone"
    if raw in {"helicopter", "helicoptero", "helicóptero", "heli"}:
        return "helicoptero"
    if raw in {"plane", "avioneta", "avion", "avión", "airplane"}:
        return "avioneta"
    if "drone" in raw or "dron" in raw:
        return "drone"
    if "heli" in raw:
        return "helicoptero"
    if "avion" in raw or "plane" in raw:
        return "avioneta"
    return raw or "otro"


def _status(row: Dict[str, Any]) -> str:
    raw = str(row.get("status") or row.get("processing_status") or row.get("state") or row.get("estado") or "sin_estado").strip().lower()
    if raw in {"done", "completed", "complete", "success", "procesado", "procesada", "finalizado"}:
        return "completed"
    if raw in {"failed", "error", "fallido", "fallida"}:
        return "failed"
    if raw in {"running", "processing", "procesando", "in_progress"}:
        return "processing"
    if raw in {"pending", "pendiente", "queued"}:
        return "pending"
    return raw or "sin_estado"


def _belongs_to_user(row: Dict[str, Any], user_id: str, admin_mode: bool = False) -> bool:
    if admin_mode:
        return True
    candidate_keys = ("user_id", "requested_by_user_id", "created_by", "owner_id")
    for key in candidate_keys:
        if row.get(key) and str(row.get(key)) == user_id:
            return True
    # Registros sin usuario explícito se omiten para evitar mezclar datos de otros usuarios.
    return False


def _role_for_user(db: Dict[str, Any], user_id: str) -> str:
    roles = [str(r.get("role") or "").lower() for r in table(db, "user_roles") if str(r.get("user_id")) == user_id]
    if any(role in {"superadmin", "platform_admin", "admin"} for role in roles):
        return "admin"
    return roles[0] if roles else "user"


def _scoped_rows(db: Dict[str, Any], table_name: str, user_id: str, admin_mode: bool = False) -> List[Dict[str, Any]]:
    return [dict(row) for row in table(db, table_name) if _belongs_to_user(dict(row), user_id, admin_mode)]


def _parcel_unique_key(row: Dict[str, Any]) -> str:
    for key in ("lote", "codigo", "name", "id"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return " ".join(value.split())
    return str(id(row))


def _dedupe_parcels(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = _parcel_unique_key(row)
        current = by_key.get(key)
        if current is None:
            by_key[key] = row
            continue
        current_dt = _as_datetime(current.get("updated_at") or current.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
        row_dt = _as_datetime(row.get("updated_at") or row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
        if row_dt >= current_dt:
            by_key[key] = row
    return list(by_key.values())


def _latest(rows: Iterable[Dict[str, Any]], *date_keys: str) -> Optional[Dict[str, Any]]:
    def key(row: Dict[str, Any]) -> datetime:
        for date_key in date_keys:
            dt = _as_datetime(row.get(date_key))
            if dt:
                return dt
        return datetime.min.replace(tzinfo=timezone.utc)

    items = list(rows)
    if not items:
        return None
    return max(items, key=key)


def _sort_recent(rows: Iterable[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    def key(row: Dict[str, Any]) -> datetime:
        for date_key in ("created_at", "updated_at", "image_date", "day", "date"):
            dt = _as_datetime(row.get(date_key))
            if dt:
                return dt
        return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(list(rows), key=key, reverse=True)[:limit]


def _activity(kind: str, title: str, when: Any, description: Optional[str] = None, module: Optional[str] = None) -> Dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "description": description,
        "module": module,
        "created_at": _iso(when),
        "date_label": _date_label(when),
    }


def _extension_summary(extension_requests: List[Dict[str, Any]], platform_modules: List[Dict[str, Any]]) -> Dict[str, Any]:
    extension_ids = {"digiforms", "graniot"}
    extensions = [m for m in platform_modules if str(m.get("id") or "").lower() in extension_ids or str(m.get("category") or "").lower() == "extension"]
    counts = Counter(str(r.get("status") or "pending").lower() for r in extension_requests)
    return {
        "available": len([e for e in extensions if e.get("is_active", True)]),
        "active": len([e for e in extensions if e.get("is_active", True)]),
        "pending_requests": counts.get("pending", 0) + counts.get("requested", 0),
        "approved_requests": counts.get("approved", 0) + counts.get("aprobada", 0),
        "rejected_requests": counts.get("rejected", 0) + counts.get("rechazada", 0),
        "items": [
            {
                "id": e.get("id"),
                "name": e.get("name") or e.get("id"),
                "is_active": bool(e.get("is_active", True)),
                "description": e.get("description"),
            }
            for e in extensions
        ],
    }


@router.get("/summary")
def dashboard_summary(current_user: Any = Depends(get_current_user)):
    user_id = _user_id_from_current(current_user)
    db = read_db()
    role = _role_for_user(db, user_id)
    # El dashboard operativo siempre representa el espacio de trabajo del usuario
    # autenticado. Los administradores disponen de sus vistas globales en /admin;
    # mezclar aquí registros de otros usuarios provoca conteos, mapas y alertas
    # inconsistentes con Configuración y con los demás módulos de la plataforma.
    admin_mode = False

    parcels = _dedupe_parcels(_scoped_rows(db, "parcels", user_id, admin_mode))
    parcel_ids = {str(row.get("id")) for row in parcels if row.get("id")}
    satellite_images = _scoped_rows(db, "satellite_images", user_id, admin_mode)
    satellite_jobs = _scoped_rows(db, "satellite_jobs", user_id, admin_mode)
    aerial_analyses = _scoped_rows(db, "aerial_analyses", user_id, admin_mode)
    analysis_sessions = _scoped_rows(db, "analysis_sessions", user_id, admin_mode)
    analysis_points = _scoped_rows(db, "analysis_data_points", user_id, admin_mode)
    field_notes = _scoped_rows(db, "field_notes", user_id, admin_mode)
    satellite_images = [row for row in satellite_images if not row.get("parcel_id") or str(row.get("parcel_id")) in parcel_ids]
    satellite_jobs = [row for row in satellite_jobs if not row.get("parcel_id") or str(row.get("parcel_id")) in parcel_ids]
    aerial_analyses = [row for row in aerial_analyses if not row.get("parcel_id") or str(row.get("parcel_id")) in parcel_ids]
    analysis_sessions = [row for row in analysis_sessions if not row.get("parcel_id") or str(row.get("parcel_id")) in parcel_ids]
    analysis_points = [row for row in analysis_points if not row.get("parcel_id") or str(row.get("parcel_id")) in parcel_ids]
    field_notes = [row for row in field_notes if not row.get("parcel_id") or str(row.get("parcel_id")) in parcel_ids]
    extension_requests = _scoped_rows(db, "extension_requests", user_id, admin_mode)
    platform_modules = [dict(row) for row in table(db, "platform_modules")]

    total_area = round(sum(_area_from_row(row) for row in parcels), 4)
    with_geometry = [row for row in parcels if _has_geometry(row)]
    without_geometry = [row for row in parcels if not _has_geometry(row)]
    completed_satellite = [row for row in satellite_images if _status(row) == "completed"]
    satellite_with_ndvi = [row for row in satellite_images if _ndvi_mean(row) is not None]
    average_ndvi = None
    if satellite_with_ndvi:
        average_ndvi = round(sum(_ndvi_mean(row) or 0 for row in satellite_with_ndvi) / len(satellite_with_ndvi), 4)

    latest_sat_by_parcel: Dict[str, Dict[str, Any]] = {}
    for row in satellite_images:
        parcel_id = str(row.get("parcel_id") or "")
        if not parcel_id:
            continue
        current = latest_sat_by_parcel.get(parcel_id)
        if current is None:
            latest_sat_by_parcel[parcel_id] = row
        else:
            current_dt = _as_datetime(current.get("image_date") or current.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
            row_dt = _as_datetime(row.get("image_date") or row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
            if row_dt > current_dt:
                latest_sat_by_parcel[parcel_id] = row

    now_dt = datetime.now(timezone.utc)
    stale_cutoff = now_dt - timedelta(days=MONITORING_DAYS)
    unmonitored_parcel_ids = []
    for parcel in parcels:
        pid = str(parcel.get("id") or "")
        latest_row = latest_sat_by_parcel.get(pid)
        latest_dt = _as_datetime((latest_row or {}).get("image_date") or (latest_row or {}).get("created_at"))
        if not latest_dt or latest_dt < stale_cutoff:
            unmonitored_parcel_ids.append(pid)

    stressed_parcel_ids = set()
    for parcel_id, row in latest_sat_by_parcel.items():
        ndvi = _ndvi_mean(row)
        if ndvi is not None and ndvi < 0.35:
            stressed_parcel_ids.add(parcel_id)

    by_aircraft = Counter(_aircraft_type(row) for row in aerial_analyses)
    by_aerial_status = Counter(_status(row) for row in aerial_analyses)
    aerial_area = round(sum(_area_from_row(row) for row in aerial_analyses), 4)

    satellite_status = Counter(_status(row) for row in satellite_images)
    satellite_job_status = Counter(_status(row) for row in satellite_jobs)

    alerts: List[Dict[str, Any]] = []
    if without_geometry:
        alerts.append({
            "severity": "warning",
            "title": "Lotes sin geometría válida",
            "message": f"{len(without_geometry)} lote(s) requieren revisar su geometría para poder visualizarse y analizarse correctamente.",
            "count": len(without_geometry),
            "module": "lotes",
        })
    if unmonitored_parcel_ids:
        alerts.append({
            "severity": "info",
            "title": "Lotes sin monitoreo reciente",
            "message": f"{len(unmonitored_parcel_ids)} lote(s) no tienen análisis satelital en los últimos {MONITORING_DAYS} días.",
            "count": len(unmonitored_parcel_ids),
            "module": "satelite",
        })
    if stressed_parcel_ids:
        alerts.append({
            "severity": "warning",
            "title": "Posible estrés vegetal",
            "message": f"{len(stressed_parcel_ids)} lote(s) tienen NDVI promedio bajo en su último análisis.",
            "count": len(stressed_parcel_ids),
            "module": "satelite",
        })
    failed_aerial = by_aerial_status.get("failed", 0)
    if failed_aerial:
        alerts.append({
            "severity": "error",
            "title": "Análisis aéreos con error",
            "message": f"{failed_aerial} análisis aéreo(s) requieren revisión.",
            "count": failed_aerial,
            "module": "aplicaciones-aereas",
        })
    pending_extensions = Counter(str(r.get("status") or "pending").lower() for r in extension_requests).get("pending", 0)
    if pending_extensions:
        alerts.append({
            "severity": "info",
            "title": "Solicitudes de extensiones pendientes",
            "message": f"{pending_extensions} solicitud(es) de extensión pendientes de aprobación.",
            "count": pending_extensions,
            "module": "extensiones",
        })

    parcel_cards = []
    for parcel in parcels:
        pid = str(parcel.get("id") or "")
        latest_sat = latest_sat_by_parcel.get(pid)
        ndvi = _ndvi_mean(latest_sat or {}) if latest_sat else None
        parcel_cards.append({
            "id": pid,
            "name": parcel.get("name") or parcel.get("lote") or parcel.get("finca") or "Lote sin nombre",
            "finca": parcel.get("finca"),
            "lote": parcel.get("lote"),
            "codigo": parcel.get("codigo") or parcel.get("external_id"),
            "area": _area_from_row(parcel),
            "has_geometry": _has_geometry(parcel),
            "bounds": _bounds(parcel),
            "center": _center(parcel),
            "geometry": _geometry(parcel),
            "latest_ndvi": ndvi,
            "latest_satellite_at": _iso((latest_sat or {}).get("image_date") or (latest_sat or {}).get("created_at")) if latest_sat else None,
            "satellite_count": len([row for row in satellite_images if str(row.get("parcel_id")) == pid]),
            "aerial_count": len([row for row in aerial_analyses if str(row.get("parcel_id")) == pid]),
        })

    activities: List[Dict[str, Any]] = []
    for row in _sort_recent(parcels, 5):
        activities.append(_activity("parcel", f"Lote cargado: {row.get('name') or row.get('lote') or 'Sin nombre'}", row.get("created_at"), module="lotes"))
    for row in _sort_recent(satellite_images, 5):
        title = f"Análisis satelital {row.get('index_type') or row.get('index') or ''}".strip()
        activities.append(_activity("satellite", title, row.get("image_date") or row.get("created_at"), row.get("processing_status"), "satelite"))
    for row in _sort_recent(aerial_analyses, 5):
        activities.append(_activity("aerial", f"Análisis aéreo: {_aircraft_type(row)}", row.get("created_at") or row.get("date"), _status(row), "aplicaciones-aereas"))
    for row in _sort_recent(field_notes, 5):
        activities.append(_activity("note", row.get("title") or "Nota de campo registrada", row.get("created_at"), row.get("description"), "campo"))
    activities = sorted(activities, key=lambda x: _as_datetime(x.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:12]

    satellite_last = _latest(satellite_images, "image_date", "created_at")
    aerial_last = _latest(aerial_analyses, "created_at", "date", "updated_at")

    payload = {
        "generated_at": now_dt.isoformat(),
        "scope": {"user_id": user_id, "role": role, "admin_mode": admin_mode, "monitoring_days": MONITORING_DAYS},
        "parcels": {
            "total": len(parcels),
            "total_area": total_area,
            "with_geometry": len(with_geometry),
            "without_geometry": len(without_geometry),
            "geometry_quality_percent": round((len(with_geometry) / len(parcels) * 100), 2) if parcels else None,
            "items": parcel_cards,
        },
        "satellite": {
            "total_analyses": len(satellite_images),
            "completed_analyses": len(completed_satellite),
            "status_counts": dict(satellite_status),
            "job_status_counts": dict(satellite_job_status),
            "average_ndvi": average_ndvi,
            "analyses_with_ndvi": len(satellite_with_ndvi),
            "stressed_parcels": len(stressed_parcel_ids),
            "unmonitored_parcels": len(unmonitored_parcel_ids),
            "last_analysis_at": _iso((satellite_last or {}).get("image_date") or (satellite_last or {}).get("created_at")) if satellite_last else None,
            "last_index_type": (satellite_last or {}).get("index_type") or (satellite_last or {}).get("index") if satellite_last else None,
        },
        "aerial": {
            "total_analyses": len(aerial_analyses),
            "by_aircraft": dict(by_aircraft),
            "status_counts": dict(by_aerial_status),
            "covered_area": aerial_area,
            "last_analysis_at": _iso((aerial_last or {}).get("created_at") or (aerial_last or {}).get("date")) if aerial_last else None,
            "last_aircraft_type": _aircraft_type(aerial_last or {}) if aerial_last else None,
            "recent": [
                {
                    "id": row.get("id"),
                    "aircraft_type": _aircraft_type(row),
                    "status": _status(row),
                    "area": _area_from_row(row),
                    "created_at": _iso(row.get("created_at") or row.get("date")),
                    "parcel_id": row.get("parcel_id"),
                }
                for row in _sort_recent(aerial_analyses, 8)
            ],
        },
        "operations": {
            "field_notes": len(field_notes),
            "analysis_sessions": len(analysis_sessions),
            "analysis_data_points": len(analysis_points),
            "monitoring_coverage_percent": round(((len(parcel_ids) - len(unmonitored_parcel_ids)) / len(parcel_ids) * 100), 2) if parcel_ids else None,
            "aerial_coverage_percent": round((len({str(row.get('parcel_id')) for row in aerial_analyses if row.get('parcel_id')}) / len(parcel_ids) * 100), 2) if parcel_ids else None,
        },
        "extensions": _extension_summary(extension_requests, platform_modules),
        "alerts": alerts,
        "recent_activity": activities,
        "data_health": {
            "has_parcels": bool(parcels),
            "has_satellite": bool(satellite_images),
            "has_aerial": bool(aerial_analyses),
            "has_ndvi_metrics": bool(satellite_with_ndvi),
            "notes": [
                "Los indicadores usan únicamente tablas reales de compatibilidad/backend.",
                "Si una métrica no existe, el frontend debe mostrar estado vacío y no inventar valores.",
            ],
        },
    }

    return {"data": payload, "error": None}
