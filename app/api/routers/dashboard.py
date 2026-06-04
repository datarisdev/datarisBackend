from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.routers.compat import read_db, table
from app.services.sentinel2.history import representative_satellite_comparison_side, satellite_comparison_sides

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
    explicit_ndvi = _first_number(
        row.get("ndvi_mean"),
        row.get("mean_ndvi"),
        row.get("average_ndvi"),
        row.get("avg_ndvi"),
        stats.get("ndvi_mean"),
        stats.get("mean_ndvi"),
        stats.get("average_ndvi"),
        stats.get("avg_ndvi"),
        _nested_get(stats, "NDVI", "mean"),
        _nested_get(stats, "ndvi", "mean"),
    )
    if explicit_ndvi is not None:
        return explicit_ndvi

    # A generic `statistics.mean` only represents NDVI when the saved analysis
    # itself is NDVI. Treating NDWI/GNDVI means as NDVI distorts vegetation KPIs.
    index_type = str(row.get("index_type") or row.get("index") or "").upper().strip()
    return _first_number(stats.get("mean"), stats.get("average")) if index_type == "NDVI" else None


def _satellite_coverage(row: Dict[str, Any]) -> Optional[float]:
    stats = _statistics(row)
    return _first_number(
        row.get("coverage_percent"),
        stats.get("coverage_percent"),
        _nested_get(row, "analysis_payload", "coverage_percent"),
    )


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
        south = _first_number(raw.get("south"), raw.get("minLat"), raw.get("min_lat"))
        north = _first_number(raw.get("north"), raw.get("maxLat"), raw.get("max_lat"))
        west = _first_number(raw.get("west"), raw.get("minLng"), raw.get("min_lng"), raw.get("minLon"), raw.get("min_lon"))
        east = _first_number(raw.get("east"), raw.get("maxLng"), raw.get("max_lng"), raw.get("maxLon"), raw.get("max_lon"))
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


def _parcel_ref(row: Dict[str, Any]) -> str:
    return str(row.get("parcel_id") or row.get("lote_id") or row.get("lot_id") or "")


def _mapping_image_url(row: Dict[str, Any]) -> Optional[str]:
    for value in (
        row.get("image_url"),
        row.get("preview_url"),
        row.get("public_url"),
        row.get("result_image_url"),
        row.get("orthomosaic_url"),
        _nested_get(row, "metadata", "image_url"),
        _nested_get(row, "analysis_payload", "image_url"),
    ):
        if value:
            return str(value)
    return None


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _aerial_geometry(row: Dict[str, Any]) -> Any:
    raw = _as_dict(row.get("parcel_geometries"))
    for key in ("sprOnTracks", "sprOn", "parcelas", "overlapGeom", "sprOffTracks", "sprOff"):
        candidate = raw.get(key)
        if candidate:
            return candidate
    results = row.get("parcel_results") if isinstance(row.get("parcel_results"), list) else []
    for result in results:
        if not isinstance(result, dict):
            continue
        for key in ("coveredGeom", "geometry", "overlapGeom", "uncoveredGeom"):
            candidate = result.get(key)
            if candidate:
                return candidate
    return _geometry(row)


def _mapeo_geometry(points: Iterable[Dict[str, Any]], limit: int = 600) -> Optional[Dict[str, Any]]:
    features: List[Dict[str, Any]] = []
    rows = list(points)
    stride = max(1, math.ceil(len(rows) / max(1, limit)))
    for point in rows[::stride][:limit]:
        lat = _first_number(point.get("latitude"), point.get("lat"))
        lng = _first_number(point.get("longitude"), point.get("lng"), point.get("lon"))
        if lat is None or lng is None:
            continue
        features.append({
            "type": "Feature",
            "properties": point.get("attributes") if isinstance(point.get("attributes"), dict) else {},
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
        })
    return {"type": "FeatureCollection", "features": features} if features else None


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


def _row_timestamp(row: Dict[str, Any], *date_keys: str) -> datetime:
    keys = date_keys or ("compared_at", "created_at", "updated_at", "image_date", "date")
    for key in keys:
        dt = _as_datetime(row.get(key))
        if dt:
            return dt
    return datetime.min.replace(tzinfo=timezone.utc)


def _comparison_ndvi_values(row: Dict[str, Any]) -> List[float]:
    values: List[float] = []
    for side in satellite_comparison_sides(row):
        if str(side.get("index_type") or side.get("index") or "").upper().strip() != "NDVI":
            continue
        value = _ndvi_mean(side)
        if value is not None:
            values.append(value)
    return values


def _comparison_ndvi(row: Dict[str, Any]) -> Optional[float]:
    values = _comparison_ndvi_values(row)
    return round(sum(values) / len(values), 4) if values else None


def _comparison_indices(row: Dict[str, Any]) -> List[str]:
    values = [str(side.get("index_type") or side.get("index") or "").upper().strip() for side in satellite_comparison_sides(row)]
    return [value for value in dict.fromkeys(values) if value]


def _comparison_coverage_values(row: Dict[str, Any]) -> List[float]:
    values: List[float] = []
    for side in satellite_comparison_sides(row):
        coverage = _satellite_coverage(side)
        if coverage is not None:
            values.append(coverage)
    return values


def _comparison_is_partial(row: Dict[str, Any]) -> bool:
    return bool(row.get("is_partial")) or any(bool(side.get("is_partial")) for side in satellite_comparison_sides(row))


def _comparison_title(row: Dict[str, Any]) -> str:
    sides = satellite_comparison_sides(row)
    if len(sides) >= 2:
        left, right = sides[0], sides[1]
        return (
            f"Comparación satelital {left.get('index_type') or 'Índice'} {left.get('image_date') or ''} "
            f"vs {right.get('index_type') or 'Índice'} {right.get('image_date') or ''}"
        ).strip()
    return str(row.get("title") or "Comparación satelital")


def _satellite_mapping(row: Dict[str, Any]) -> Dict[str, Any]:
    side = representative_satellite_comparison_side(row)
    sides = satellite_comparison_sides(row)
    left = sides[0] if sides else {}
    right = sides[1] if len(sides) > 1 else {}
    analysis_at = row.get("compared_at") or row.get("updated_at") or row.get("created_at")
    return {
        "source": "satellite",
        "source_label": "Comparación satelital",
        "title": _comparison_title(row),
        "analysis_at": _iso(analysis_at),
        "image_url": side.get("image_url"),
        "bounds": side.get("bounds"),
        "index_type": side.get("index_type"),
        "ndvi": _comparison_ndvi(row),
        "comparison": {
            "left_date": left.get("image_date"),
            "right_date": right.get("image_date"),
            "left_index": left.get("index_type"),
            "right_index": right.get("index_type"),
        },
    }


def _mapeo_mapping(row: Dict[str, Any], points: Iterable[Dict[str, Any]] = ()) -> Dict[str, Any]:
    analysis_at = row.get("created_at") or row.get("updated_at") or row.get("date")
    return {
        "source": "mapeo",
        "source_label": "Mapeo",
        "title": row.get("name") or row.get("analysis_type") or "Análisis de mapeo",
        "analysis_at": _iso(analysis_at),
        "image_url": _mapping_image_url(row),
        "bounds": _bounds(row),
        "geometry": _mapeo_geometry(points) or _geometry(row),
        "comparison": None,
    }


def _aerial_mapping(row: Dict[str, Any]) -> Dict[str, Any]:
    analysis_at = row.get("created_at") or row.get("fecha_aplicacion") or row.get("date") or row.get("updated_at")
    return {
        "source": "aerial",
        "source_label": "Aplicación aérea",
        "title": f"Análisis aéreo: {_aircraft_type(row)}",
        "analysis_at": _iso(analysis_at),
        "image_url": _mapping_image_url(row),
        "bounds": _bounds(row),
        "geometry": _aerial_geometry(row),
        "comparison": None,
    }


def _mapping_sort_key(mapping: Dict[str, Any]) -> datetime:
    return _as_datetime(mapping.get("analysis_at")) or datetime.min.replace(tzinfo=timezone.utc)


@router.get("/summary")
def dashboard_summary(current_user: Any = Depends(get_current_user)):
    user_id = _user_id_from_current(current_user)
    db = read_db()
    role = _role_for_user(db, user_id)
    # El Centro de Control siempre resume el espacio de trabajo del usuario
    # autenticado. Las vistas administrativas globales se mantienen separadas.
    admin_mode = False

    parcels = _dedupe_parcels(_scoped_rows(db, "parcels", user_id, admin_mode))
    parcel_ids = {str(row.get("id")) for row in parcels if row.get("id")}
    satellite_comparisons = _scoped_rows(db, "satellite_comparisons", user_id, admin_mode)
    satellite_jobs = _scoped_rows(db, "satellite_jobs", user_id, admin_mode)
    aerial_analyses = _scoped_rows(db, "aerial_analyses", user_id, admin_mode)
    analysis_sessions = _scoped_rows(db, "analysis_sessions", user_id, admin_mode)
    session_ids = {str(row.get("id")) for row in analysis_sessions if row.get("id")}
    analysis_points = [dict(row) for row in table(db, "analysis_data_points") if str(row.get("session_id") or "") in session_ids]
    field_notes = _scoped_rows(db, "field_notes", user_id, admin_mode)

    satellite_comparisons = [row for row in satellite_comparisons if not _parcel_ref(row) or _parcel_ref(row) in parcel_ids]
    satellite_jobs = [row for row in satellite_jobs if not _parcel_ref(row) or _parcel_ref(row) in parcel_ids]
    aerial_analyses = [row for row in aerial_analyses if not _parcel_ref(row) or _parcel_ref(row) in parcel_ids]
    analysis_sessions = [row for row in analysis_sessions if not _parcel_ref(row) or _parcel_ref(row) in parcel_ids]
    field_notes = [row for row in field_notes if not _parcel_ref(row) or _parcel_ref(row) in parcel_ids]
    extension_requests = _scoped_rows(db, "extension_requests", user_id, admin_mode)
    platform_modules = [dict(row) for row in table(db, "platform_modules")]

    total_area = round(sum(_area_from_row(row) for row in parcels), 4)
    with_geometry = [row for row in parcels if _has_geometry(row)]
    without_geometry = [row for row in parcels if not _has_geometry(row)]
    completed_satellite = [row for row in satellite_comparisons if _status(row) == "completed"]
    satellite_ndvi_values = [value for row in completed_satellite for value in _comparison_ndvi_values(row)]
    average_ndvi = round(sum(satellite_ndvi_values) / len(satellite_ndvi_values), 4) if satellite_ndvi_values else None

    latest_sat_by_parcel: Dict[str, Dict[str, Any]] = {}
    latest_ndvi_by_parcel: Dict[str, Dict[str, Any]] = {}
    for row in completed_satellite:
        parcel_id = str(row.get("parcel_id") or "")
        if not parcel_id:
            continue
        if parcel_id not in latest_sat_by_parcel or _row_timestamp(row) > _row_timestamp(latest_sat_by_parcel[parcel_id]):
            latest_sat_by_parcel[parcel_id] = row
        if _comparison_ndvi(row) is not None and (parcel_id not in latest_ndvi_by_parcel or _row_timestamp(row) > _row_timestamp(latest_ndvi_by_parcel[parcel_id])):
            latest_ndvi_by_parcel[parcel_id] = row

    now_dt = datetime.now(timezone.utc)
    stale_cutoff = now_dt - timedelta(days=MONITORING_DAYS)
    unmonitored_parcel_ids = []
    for parcel in parcels:
        pid = str(parcel.get("id") or "")
        latest_row = latest_sat_by_parcel.get(pid)
        latest_dt = _row_timestamp(latest_row) if latest_row else None
        if not latest_dt or latest_dt < stale_cutoff:
            unmonitored_parcel_ids.append(pid)

    stressed_parcel_ids = {parcel_id for parcel_id, row in latest_ndvi_by_parcel.items() if (_comparison_ndvi(row) is not None and (_comparison_ndvi(row) or 0) < 0.35)}

    by_aircraft = Counter(_aircraft_type(row) for row in aerial_analyses)
    by_aerial_status = Counter(_status(row) for row in aerial_analyses)
    aerial_area = round(sum(_area_from_row(row) for row in aerial_analyses), 4)
    satellite_status = Counter(_status(row) for row in satellite_comparisons)
    satellite_job_status = Counter(_status(row) for row in satellite_jobs)
    satellite_index_counts: Counter[str] = Counter()
    for row in satellite_comparisons:
        for index in _comparison_indices(row):
            satellite_index_counts[index] += 1
    satellite_coverage_values = [value for row in completed_satellite for value in _comparison_coverage_values(row)]
    average_valid_coverage_percent = round(sum(satellite_coverage_values) / len(satellite_coverage_values), 2) if satellite_coverage_values else None
    partial_satellite_analyses = len([row for row in completed_satellite if _comparison_is_partial(row)])
    covered_satellite_parcels = {str(row.get("parcel_id")) for row in satellite_comparisons if row.get("parcel_id")}

    alerts: List[Dict[str, Any]] = []
    if without_geometry:
        alerts.append({"severity": "warning", "title": "Lotes sin geometría válida", "message": f"{len(without_geometry)} lote(s) requieren revisar su geometría para poder visualizarse y analizarse correctamente.", "count": len(without_geometry), "module": "lotes"})
    if unmonitored_parcel_ids:
        alerts.append({"severity": "info", "title": "Lotes sin monitoreo reciente", "message": f"{len(unmonitored_parcel_ids)} lote(s) no tienen comparaciones satelitales guardadas en los últimos {MONITORING_DAYS} días.", "count": len(unmonitored_parcel_ids), "module": "satelite"})
    if stressed_parcel_ids:
        alerts.append({"severity": "warning", "title": "Posible estrés vegetal", "message": f"{len(stressed_parcel_ids)} lote(s) tienen NDVI promedio bajo en su comparación más reciente.", "count": len(stressed_parcel_ids), "module": "satelite"})
    failed_aerial = by_aerial_status.get("failed", 0)
    if failed_aerial:
        alerts.append({"severity": "error", "title": "Análisis aéreos con error", "message": f"{failed_aerial} análisis aéreo(s) requieren revisión.", "count": failed_aerial, "module": "aplicaciones-aereas"})
    pending_extensions = Counter(str(r.get("status") or "pending").lower() for r in extension_requests).get("pending", 0)
    if pending_extensions:
        alerts.append({"severity": "info", "title": "Solicitudes de extensiones pendientes", "message": f"{pending_extensions} solicitud(es) de extensión están pendientes de revisión.", "count": pending_extensions, "module": "extensiones"})

    points_by_session: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for point in analysis_points:
        points_by_session[str(point.get("session_id") or "")].append(point)

    parcel_cards = []
    for parcel in parcels:
        pid = str(parcel.get("id") or "")
        latest_sat = latest_sat_by_parcel.get(pid)
        latest_ndvi_row = latest_ndvi_by_parcel.get(pid)
        candidates: List[Dict[str, Any]] = []
        if latest_sat:
            candidates.append(_satellite_mapping(latest_sat))
        candidates.extend(_mapeo_mapping(row, points_by_session.get(str(row.get("id") or ""), [])) for row in analysis_sessions if _parcel_ref(row) == pid)
        candidates.extend(_aerial_mapping(row) for row in aerial_analyses if _parcel_ref(row) == pid)
        latest_mapping = max(candidates, key=_mapping_sort_key) if candidates else None
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
            "latest_ndvi": _comparison_ndvi(latest_ndvi_row) if latest_ndvi_row else None,
            "latest_satellite_at": _iso((latest_sat or {}).get("compared_at") or (latest_sat or {}).get("updated_at") or (latest_sat or {}).get("created_at")) if latest_sat else None,
            "latest_analysis_at": latest_mapping.get("analysis_at") if latest_mapping else None,
            "latest_mapping": latest_mapping,
            "satellite_count": len([row for row in satellite_comparisons if str(row.get("parcel_id")) == pid]),
            "aerial_count": len([row for row in aerial_analyses if _parcel_ref(row) == pid]),
            "mapeo_count": len([row for row in analysis_sessions if _parcel_ref(row) == pid]),
        })

    activities: List[Dict[str, Any]] = []
    for row in _sort_recent(parcels, 5):
        activities.append(_activity("parcel", f"Lote cargado: {row.get('name') or row.get('lote') or 'Sin nombre'}", row.get("created_at"), module="lotes"))
    for row in sorted(satellite_comparisons, key=_row_timestamp, reverse=True)[:5]:
        activities.append(_activity("satellite", _comparison_title(row), row.get("compared_at") or row.get("created_at"), "Comparación guardada", "satelite"))
    for row in _sort_recent(aerial_analyses, 5):
        activities.append(_activity("aerial", f"Análisis aéreo: {_aircraft_type(row)}", row.get("created_at") or row.get("date"), _status(row), "aplicaciones-aereas"))
    for row in _sort_recent(analysis_sessions, 5):
        activities.append(_activity("mapping", row.get("name") or row.get("analysis_type") or "Análisis de mapeo", row.get("created_at") or row.get("updated_at"), "Mapeo guardado", "mapeo"))
    for row in _sort_recent(field_notes, 5):
        activities.append(_activity("note", row.get("title") or "Nota de campo registrada", row.get("created_at"), row.get("description"), "campo"))
    activities = sorted(activities, key=lambda item: _as_datetime(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:12]

    satellite_last = max(satellite_comparisons, key=_row_timestamp) if satellite_comparisons else None
    satellite_last_side = representative_satellite_comparison_side(satellite_last or {})
    aerial_last = _latest(aerial_analyses, "created_at", "date", "updated_at")
    payload = {
        "generated_at": now_dt.isoformat(),
        "scope": {"user_id": user_id, "role": role, "admin_mode": admin_mode, "monitoring_days": MONITORING_DAYS},
        "parcels": {"total": len(parcels), "total_area": total_area, "with_geometry": len(with_geometry), "without_geometry": len(without_geometry), "geometry_quality_percent": round((len(with_geometry) / len(parcels) * 100), 2) if parcels else None, "items": parcel_cards},
        "satellite": {
            "total_analyses": len(satellite_comparisons),
            "completed_analyses": len(completed_satellite),
            "status_counts": dict(satellite_status),
            "job_status_counts": dict(satellite_job_status),
            "average_ndvi": average_ndvi,
            "analyses_with_ndvi": len([row for row in satellite_comparisons if _comparison_ndvi_values(row)]),
            "stressed_parcels": len(stressed_parcel_ids),
            "unmonitored_parcels": len(unmonitored_parcel_ids),
            "covered_parcels": len(covered_satellite_parcels),
            "index_counts": dict(satellite_index_counts),
            "average_valid_coverage_percent": average_valid_coverage_percent,
            "partial_analyses": partial_satellite_analyses,
            "last_analysis_at": _iso((satellite_last or {}).get("compared_at") or (satellite_last or {}).get("updated_at") or (satellite_last or {}).get("created_at")) if satellite_last else None,
            "last_index_type": satellite_last_side.get("index_type") if satellite_last_side else None,
        },
        "aerial": {
            "total_analyses": len(aerial_analyses),
            "by_aircraft": dict(by_aircraft),
            "status_counts": dict(by_aerial_status),
            "covered_area": aerial_area,
            "last_analysis_at": _iso((aerial_last or {}).get("created_at") or (aerial_last or {}).get("date")) if aerial_last else None,
            "last_aircraft_type": _aircraft_type(aerial_last or {}) if aerial_last else None,
            "recent": [{"id": row.get("id"), "aircraft_type": _aircraft_type(row), "status": _status(row), "area": _area_from_row(row), "created_at": _iso(row.get("created_at") or row.get("fecha_aplicacion") or row.get("date")), "parcel_id": _parcel_ref(row) or None} for row in _sort_recent(aerial_analyses, 8)],
        },
        "operations": {
            "field_notes": len(field_notes),
            "analysis_sessions": len(analysis_sessions),
            "analysis_data_points": len(analysis_points),
            "monitoring_coverage_percent": round(((len(parcel_ids) - len(unmonitored_parcel_ids)) / len(parcel_ids) * 100), 2) if parcel_ids else None,
            "aerial_coverage_percent": round((len({_parcel_ref(row) for row in aerial_analyses if _parcel_ref(row)}) / len(parcel_ids) * 100), 2) if parcel_ids else None,
        },
        "extensions": _extension_summary(extension_requests, platform_modules),
        "alerts": alerts,
        "recent_activity": activities,
        "data_health": {
            "has_parcels": bool(parcels),
            "has_satellite": bool(satellite_comparisons),
            "has_aerial": bool(aerial_analyses),
            "has_ndvi_metrics": bool(satellite_ndvi_values),
            "notes": [
                "Los indicadores se actualizan con las comparaciones guardadas y los análisis disponibles para cada lote.",
                "Cuando todavía no existe información suficiente, se muestra un estado pendiente de análisis.",
            ],
        },
    }
    return {"data": payload, "error": None}

