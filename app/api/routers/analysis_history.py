from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user
from app.api.routers.compat import read_db, table
from app.api.routers.dashboard import (
    _aircraft_type,
    _as_datetime,
    _belongs_to_user,
    _bounds,
    _center,
    _geometry,
    _has_geometry,
    _iso,
    _role_for_user,
    _scoped_rows,
    _status,
)

router = APIRouter(prefix="/analysis-history", tags=["Analysis History"])


def _user_id_from_current(current_user: Any) -> str:
    if isinstance(current_user, dict):
        return str(current_user.get("id") or current_user.get("sub") or "")
    return str(current_user or "")


def _feature_collection(features: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    clean = [feature for feature in features if feature.get("geometry")]
    if not clean:
        return None
    return {"type": "FeatureCollection", "features": clean}


def _normalize_geojson(value: Any) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    if isinstance(value, str):
        try:
            import json

            value = json.loads(value)
        except Exception:
            return None
    if not isinstance(value, dict):
        return None
    geo_type = value.get("type")
    if geo_type == "FeatureCollection":
        return value
    if geo_type == "Feature":
        return {"type": "FeatureCollection", "features": [value]}
    if geo_type in {"Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point", "MultiPoint"}:
        return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": value}]}
    return None


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _date_value(row: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = row.get(key)
        if value:
            return _iso(value) or str(value)
    return None


def _sort_key(row: Dict[str, Any]) -> datetime:
    for key in ("date", "created_at", "updated_at", "image_date"):
        dt = _as_datetime(row.get(key))
        if dt:
            return dt
    return datetime.min.replace(tzinfo=timezone.utc)


def _parcel_lookup(parcels: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for parcel in parcels:
        parcel_id = str(parcel.get("id") or "")
        if parcel_id:
            lookup[parcel_id] = parcel
    return lookup


def _parcel_name(parcel: Optional[Dict[str, Any]]) -> Optional[str]:
    if not parcel:
        return None
    return _first(parcel.get("name"), parcel.get("lote"), parcel.get("codigo"), parcel.get("external_id"))


def _layer(
    *,
    id: str,
    source: str,
    source_label: str,
    title: str,
    subtitle: Optional[str],
    date: Optional[str],
    status: str,
    geometry: Optional[Dict[str, Any]],
    bounds: Optional[Dict[str, float]],
    center: Optional[Dict[str, float]],
    metrics: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": id,
        "source": source,
        "source_label": source_label,
        "title": title,
        "subtitle": subtitle,
        "date": date,
        "status": status,
        "geometry": geometry,
        "bounds": bounds,
        "center": center,
        "has_geometry": bool(geometry),
        "metrics": metrics or {},
        "metadata": metadata or {},
    }


def _aerial_layers(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    layers: List[Dict[str, Any]] = []
    for row in rows:
        raw_geom = row.get("parcel_geometries") or {}
        if isinstance(raw_geom, str):
            try:
                import json

                raw_geom = json.loads(raw_geom)
            except Exception:
                raw_geom = {}
        geometry = None
        if isinstance(raw_geom, dict):
            geometry = _normalize_geojson(raw_geom.get("parcelas") or raw_geom.get("sprOnTracks") or raw_geom.get("sprOn") or raw_geom.get("overlapGeom"))
        if geometry is None:
            results = row.get("parcel_results") if isinstance(row.get("parcel_results"), list) else []
            features: List[Dict[str, Any]] = []
            for result in results:
                if not isinstance(result, dict):
                    continue
                for key in ("coveredGeom", "uncoveredGeom", "overlapGeom", "geometry"):
                    fc = _normalize_geojson(result.get(key))
                    if fc:
                        for feature in fc.get("features", []):
                            feature = dict(feature)
                            feature["properties"] = {**(feature.get("properties") or {}), "layer": key, "parcel": result.get("name")}
                            features.append(feature)
            geometry = _feature_collection(features)

        layers.append(
            _layer(
                id=f"aerial:{row.get('id')}",
                source="aerial",
                source_label="Aplicaciones aéreas",
                title=f"{_aircraft_type(row).capitalize()} - {row.get('parcel_name') or 'Sin lote'}",
                subtitle=_first(row.get("producto"), row.get("piloto"), row.get("observaciones")),
                date=_date_value(row, "fecha_aplicacion", "created_at"),
                status=_status(row),
                geometry=geometry,
                bounds=_bounds(row),
                center=_center(row),
                metrics={
                    "Área total": row.get("total_ha"),
                    "Cobertura": row.get("covered_pct"),
                    "Sin cubrir": row.get("uncovered_ha"),
                    "Sobre-aplicado": row.get("overlap_ha"),
                },
                metadata={"aircraft_type": _aircraft_type(row), "raw_id": row.get("id")},
            )
        )
    return layers


def _mapeo_layers(sessions: List[Dict[str, Any]], points: List[Dict[str, Any]], parcels: Dict[str, Dict[str, Any]], max_points_per_layer: int) -> List[Dict[str, Any]]:
    points_by_session: Dict[str, List[Dict[str, Any]]] = {}
    for point in points:
        sid = str(point.get("session_id") or "")
        if sid:
            points_by_session.setdefault(sid, []).append(point)

    layers: List[Dict[str, Any]] = []
    for session in sessions:
        sid = str(session.get("id") or "")
        session_points = points_by_session.get(sid, [])
        stride = max(1, len(session_points) // max_points_per_layer) if max_points_per_layer > 0 else 1
        features = []
        for point in session_points[::stride][:max_points_per_layer]:
            lat = point.get("latitude") or point.get("lat")
            lng = point.get("longitude") or point.get("lng")
            try:
                lat_f = float(lat)
                lng_f = float(lng)
            except Exception:
                continue
            features.append(
                {
                    "type": "Feature",
                    "properties": point.get("attributes") or {},
                    "geometry": {"type": "Point", "coordinates": [lng_f, lat_f]},
                }
            )

        parcel = parcels.get(str(session.get("parcel_id") or ""))
        geometry = _feature_collection(features)
        if geometry is None:
            geometry = _normalize_geojson(_geometry(parcel or {}))

        layers.append(
            _layer(
                id=f"mapeo:{sid}",
                source="mapeo",
                source_label="Mapeo y maquinaria",
                title=session.get("name") or session.get("analysis_type") or "Análisis de mapeo",
                subtitle=_first(session.get("labor"), session.get("responsable"), session.get("maquinaria"), _parcel_name(parcel)),
                date=_date_value(session, "created_at", "updated_at"),
                status="completed",
                geometry=geometry,
                bounds=session.get("bounds") if isinstance(session.get("bounds"), dict) else _bounds(parcel or {}),
                center=_center(parcel or {}),
                metrics={"Puntos": len(session_points), "Variables": len(session.get("variables") or [])},
                metadata={"analysis_type": session.get("analysis_type"), "parcel_id": session.get("parcel_id"), "raw_id": sid},
            )
        )
    return layers


def _satellite_layers(rows: List[Dict[str, Any]], parcels: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    layers: List[Dict[str, Any]] = []
    for row in rows:
        parcel = parcels.get(str(row.get("parcel_id") or ""))
        image_url = _first(row.get("image_url"), row.get("url"), row.get("signed_url"), row.get("public_url"))
        geometry = _normalize_geojson(_geometry(row)) or _normalize_geojson(_geometry(parcel or {}))
        layers.append(
            _layer(
                id=f"satellite:{row.get('id')}",
                source="satellite",
                source_label="Satélite",
                title=f"{row.get('index_type') or row.get('index') or 'Índice'} - {_parcel_name(parcel) or 'Sin lote'}",
                subtitle=f"Nubosidad {row.get('cloud_coverage')}%" if row.get("cloud_coverage") is not None else _parcel_name(parcel),
                date=_date_value(row, "image_date", "created_at"),
                status=_status(row),
                geometry=geometry,
                bounds=_bounds(row) or _bounds(parcel or {}),
                center=_center(row) or _center(parcel or {}),
                metrics={"Nubosidad": row.get("cloud_coverage"), "Índice": row.get("index_type") or row.get("index")},
                metadata={"parcel_id": row.get("parcel_id"), "image_url": image_url, "raw_id": row.get("id")},
            )
        )
    return layers


@router.get("/layers")
def analysis_history_layers(
    sources: Optional[str] = Query(None, description="Comma-separated sources: aerial,mapeo,satellite"),
    limit: int = Query(80, ge=1, le=300),
    max_points_per_layer: int = Query(900, ge=50, le=5000),
    current_user: Any = Depends(get_current_user),
):
    user_id = _user_id_from_current(current_user)
    db = read_db()
    role = _role_for_user(db, user_id)
    admin_mode = role == "admin"
    wanted = {s.strip().lower() for s in sources.split(",")} if sources else {"aerial", "mapeo", "satellite"}

    parcels = _scoped_rows(db, "parcels", user_id, admin_mode)
    parcel_by_id = _parcel_lookup(parcels)

    layers: List[Dict[str, Any]] = []
    if "aerial" in wanted:
        layers.extend(_aerial_layers(_scoped_rows(db, "aerial_analyses", user_id, admin_mode)))
    if "mapeo" in wanted:
        sessions = _scoped_rows(db, "analysis_sessions", user_id, admin_mode)
        session_ids = {str(session.get("id")) for session in sessions}
        points = [
            dict(row)
            for row in table(db, "analysis_data_points")
            if str(row.get("session_id") or "") in session_ids
            and (admin_mode or any(_belongs_to_user(session, user_id, admin_mode) and str(session.get("id")) == str(row.get("session_id")) for session in sessions))
        ]
        layers.extend(_mapeo_layers(sessions, points, parcel_by_id, max_points_per_layer))
    if "satellite" in wanted:
        layers.extend(_satellite_layers(_scoped_rows(db, "satellite_images", user_id, admin_mode), parcel_by_id))

    layers = sorted(layers, key=_sort_key, reverse=True)[:limit]
    counts: Dict[str, int] = {}
    for layer in layers:
        counts[layer["source"]] = counts.get(layer["source"], 0) + 1

    return {
        "data": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "layers": layers,
            "counts": counts,
            "total": len(layers),
        },
        "error": None,
    }
