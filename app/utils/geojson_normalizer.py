from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pyproj import CRS, Transformer
try:
    from shapely.errors import GEOSException
except Exception:  # Compatibilidad con versiones antiguas de Shapely
    GEOSException = Exception
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

try:
    from shapely.validation import make_valid as shapely_make_valid
except Exception:  # pragma: no cover - fallback for older environments
    shapely_make_valid = None

MAX_MERCATOR_LATITUDE = 85.051129
MIN_RING_POINTS = 3



def _polygonal_parts(geom: BaseGeometry) -> List[BaseGeometry]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [part for part in geom.geoms if not part.is_empty]
    if geom.geom_type == "GeometryCollection":
        parts: List[BaseGeometry] = []
        for child in geom.geoms:
            parts.extend(_polygonal_parts(child))
        return parts
    return []


def _repair_single_polygonal(geom: BaseGeometry) -> Optional[BaseGeometry]:
    if geom is None or geom.is_empty:
        return None

    candidates: List[BaseGeometry] = [geom]
    if shapely_make_valid is not None:
        try:
            candidates.append(shapely_make_valid(geom))
        except Exception:
            pass
    try:
        candidates.append(geom.buffer(0))
    except Exception:
        pass

    for candidate in candidates:
        if candidate is None or candidate.is_empty:
            continue
        parts = _polygonal_parts(candidate)
        if not parts:
            continue
        clean_parts: List[BaseGeometry] = []
        for part in parts:
            cleaned = part
            if not cleaned.is_valid:
                if shapely_make_valid is not None:
                    try:
                        cleaned = shapely_make_valid(cleaned)
                    except Exception:
                        pass
                if not getattr(cleaned, "is_valid", False):
                    try:
                        cleaned = cleaned.buffer(0)
                    except Exception:
                        pass
            clean_parts.extend([p for p in _polygonal_parts(cleaned) if not p.is_empty])
        clean_parts = [p for p in clean_parts if p.is_valid and p.area > 0]
        if not clean_parts:
            continue
        if len(clean_parts) == 1:
            return clean_parts[0]
        try:
            merged = unary_union(clean_parts)
            if not merged.is_empty:
                if not merged.is_valid and shapely_make_valid is not None:
                    merged = shapely_make_valid(merged)
                if not merged.is_valid:
                    merged = merged.buffer(0)
                polygonal = _polygonal_parts(merged)
                if polygonal:
                    return unary_union(polygonal) if len(polygonal) > 1 else polygonal[0]
        except Exception:
            return MultiPolygon([p for p in clean_parts if p.geom_type == "Polygon"])
    return None


def _repair_polygonal_geometry(geom: BaseGeometry) -> Optional[BaseGeometry]:
    repaired = _repair_single_polygonal(geom)
    if repaired is None or repaired.is_empty:
        return None
    if repaired.geom_type not in {"Polygon", "MultiPolygon"}:
        parts = _polygonal_parts(repaired)
        if not parts:
            return None
        try:
            repaired = unary_union(parts) if len(parts) > 1 else parts[0]
        except Exception:
            repaired = MultiPolygon([p for p in parts if p.geom_type == "Polygon"])
    return repaired if not repaired.is_empty else None


def _normalize_polygonal_geojson(geom_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        geom_obj = shape(geom_dict)
        repaired = _repair_polygonal_geometry(geom_obj)
        if repaired is None:
            return None
        return mapping(repaired)
    except Exception:
        return geom_dict


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def parse_json_if_needed(value: Any) -> Any:
    current = value
    for _ in range(3):
        if not isinstance(current, str):
            return current
        text = current.strip()
        if not text:
            return None
        try:
            current = json.loads(text)
        except Exception:
            return current
    return current


def _is_num(value: Any) -> bool:
    try:
        return value is not None and float(value) == float(value)
    except Exception:
        return False


def _num(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        numeric = float(value)
        if numeric != numeric:
            return None
        return numeric
    except Exception:
        return None


def _norm_lng(value: float) -> float:
    return ((value + 180) % 360 + 360) % 360 - 180


def _valid_lat(value: float) -> bool:
    return -MAX_MERCATOR_LATITUDE <= value <= MAX_MERCATOR_LATITUDE


def _valid_lng(value: float) -> bool:
    return -180 <= value <= 180


def _coord_order(points: Iterable[List[Any]], fallback: str = "lnglat") -> str:
    lnglat = 0
    latlng = 0
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        a = _num(point[0])
        b = _num(point[1])
        if a is None or b is None:
            continue
        if _valid_lng(a) and _valid_lat(b):
            lnglat += 1
        if _valid_lat(a) and _valid_lng(b):
            latlng += 1
        if not _valid_lat(a) and _valid_lng(a) and _valid_lat(b):
            lnglat += 8
        if not _valid_lat(b) and _valid_lng(b) and _valid_lat(a):
            latlng += 8
    if lnglat > latlng:
        return "lnglat"
    if latlng > lnglat:
        return "latlng"
    return fallback


def _normalize_point(point: Any, order: str = "lnglat") -> Optional[List[float]]:
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    first = _num(point[0])
    second = _num(point[1])
    if first is None or second is None:
        return None
    lng = first if order == "lnglat" else second
    lat = second if order == "lnglat" else first
    if not _valid_lng(lng) or not _valid_lat(lat):
        return None
    return [_norm_lng(lng), lat]


def _normalize_ring(ring: Any, fallback_order: str = "lnglat") -> List[List[float]]:
    if not isinstance(ring, list):
        return []
    raw_points = [p for p in ring if isinstance(p, (list, tuple)) and len(p) >= 2 and _is_num(p[0]) and _is_num(p[1])]
    if len(raw_points) < MIN_RING_POINTS:
        return []
    order = _coord_order(raw_points, fallback_order)
    points = [p for p in (_normalize_point(point, order) for point in raw_points) if p]
    if len(points) < MIN_RING_POINTS:
        return []
    if points[0] != points[-1]:
        points.append(points[0])
    return points


def _normalize_geometry(geometry: Any) -> Optional[Dict[str, Any]]:
    geom = parse_json_if_needed(geometry)
    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    if gtype == "Feature":
        return _normalize_geometry(geom.get("geometry"))
    if gtype == "FeatureCollection":
        return None
    if gtype == "Polygon":
        rings = [_normalize_ring(ring, "lnglat") for ring in (geom.get("coordinates") or [])]
        rings = [ring for ring in rings if len(ring) >= MIN_RING_POINTS + 1]
        if not rings:
            return None
        return _normalize_polygonal_geojson({"type": "Polygon", "coordinates": rings})
    if gtype == "MultiPolygon":
        polygons = []
        for polygon in geom.get("coordinates") or []:
            rings = [_normalize_ring(ring, "lnglat") for ring in (polygon or [])]
            rings = [ring for ring in rings if len(ring) >= MIN_RING_POINTS + 1]
            if rings:
                polygons.append(rings)
        if not polygons:
            return None
        return _normalize_polygonal_geojson({"type": "MultiPolygon", "coordinates": polygons})
    if gtype in {"LineString", "MultiLineString", "Point", "MultiPoint"}:
        # For flight tracks/points: keep valid GeoJSON order and let the frontend render it.
        try:
            geom_obj = shape(geom)
            if geom_obj.is_empty:
                return None
            return mapping(geom_obj)
        except Exception:
            return geom
    if gtype == "GeometryCollection":
        geoms = [_normalize_geometry(child) for child in (geom.get("geometries") or [])]
        geoms = [child for child in geoms if child]
        return {"type": "GeometryCollection", "geometries": geoms} if geoms else None
    return None


def normalize_geojson(value: Any) -> Optional[Dict[str, Any]]:
    raw = parse_json_if_needed(value)
    if not raw:
        return None

    if isinstance(raw, dict) and raw.get("geometry_geojson"):
        return normalize_geojson(raw.get("geometry_geojson"))
    if isinstance(raw, dict) and raw.get("geojson"):
        return normalize_geojson(raw.get("geojson"))

    features: List[Dict[str, Any]] = []
    if isinstance(raw, dict) and raw.get("type") == "FeatureCollection":
        for feature in raw.get("features") or []:
            if not isinstance(feature, dict):
                continue
            geom = _normalize_geometry(feature.get("geometry"))
            if not geom:
                continue
            features.append({
                "type": "Feature",
                "properties": _jsonable(feature.get("properties") or {}),
                "geometry": geom,
            })
    elif isinstance(raw, dict) and raw.get("type") == "Feature":
        geom = _normalize_geometry(raw.get("geometry"))
        if geom:
            features.append({"type": "Feature", "properties": _jsonable(raw.get("properties") or {}), "geometry": geom})
    elif isinstance(raw, dict) and raw.get("type"):
        geom = _normalize_geometry(raw)
        if geom:
            features.append({"type": "Feature", "properties": {}, "geometry": geom})
    elif isinstance(raw, dict) and raw.get("geometry"):
        return normalize_geojson(raw.get("geometry"))

    if not features:
        return None
    return {"type": "FeatureCollection", "features": features}


def _iter_coords(value: Any):
    if isinstance(value, (list, tuple)) and len(value) >= 2 and _is_num(value[0]) and _is_num(value[1]):
        lng = _num(value[0])
        lat = _num(value[1])
        if lng is not None and lat is not None and _valid_lng(lng) and _valid_lat(lat):
            yield lng, lat
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_coords(item)


def geometry_bounds(feature_collection: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    if not feature_collection:
        return None
    coords: List[Tuple[float, float]] = []
    for feature in feature_collection.get("features") or []:
        coords.extend(list(_iter_coords((feature.get("geometry") or {}).get("coordinates"))))
    if not coords:
        return None
    lngs = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return {"south": min(lats), "north": max(lats), "west": min(lngs), "east": max(lngs)}


def geometry_center(bounds: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not bounds:
        return None
    return {
        "lat": (float(bounds["south"]) + float(bounds["north"])) / 2,
        "lng": (float(bounds["west"]) + float(bounds["east"])) / 2,
    }


def _metric_crs_for_geom(geom: BaseGeometry) -> CRS:
    c = geom.centroid
    zone = int((float(c.x) + 180) // 6) + 1
    epsg = 32600 + zone if float(c.y) >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def geometry_area_ha(feature_collection: Optional[Dict[str, Any]]) -> Optional[float]:
    if not feature_collection:
        return None
    geoms = []
    for feature in feature_collection.get("features") or []:
        try:
            geom = shape(feature.get("geometry"))
            if geom.is_empty:
                continue
            if geom.geom_type in {"Polygon", "MultiPolygon"}:
                geoms.append(geom)
        except Exception:
            continue
    if not geoms:
        return None
    repaired_geoms = []
    for geom in geoms:
        repaired = _repair_polygonal_geometry(geom)
        if repaired is not None and not repaired.is_empty:
            repaired_geoms.append(repaired)
    if not repaired_geoms:
        return None

    try:
        union = unary_union(repaired_geoms)
        repaired_union = _repair_polygonal_geometry(union)
        if repaired_union is not None and not repaired_union.is_empty:
            union = repaired_union
        metric = _metric_crs_for_geom(union)
        transformer = Transformer.from_crs(CRS.from_epsg(4326), metric, always_xy=True)
        metric_geom = transform(transformer.transform, union)
        return round(float(metric_geom.area / 10000.0), 4)
    except (GEOSException, ValueError, Exception):
        total = 0.0
        for geom in repaired_geoms:
            metric = _metric_crs_for_geom(geom)
            transformer = Transformer.from_crs(CRS.from_epsg(4326), metric, always_xy=True)
            metric_geom = transform(transformer.transform, geom)
            total += float(metric_geom.area / 10000.0)
        return round(total, 4)


def summarize_geojson(value: Any) -> Dict[str, Any]:
    fc = normalize_geojson(value)
    bounds = geometry_bounds(fc)
    center = geometry_center(bounds)
    area = geometry_area_ha(fc)
    first_type = None
    if fc and fc.get("features"):
        first_type = ((fc["features"][0].get("geometry") or {}).get("type"))
    return {
        "geometry_geojson": fc,
        "geometry_bounds": bounds,
        "geometry_center": center,
        "bbox": [bounds["west"], bounds["south"], bounds["east"], bounds["north"]] if bounds else None,
        "geometry_type": first_type,
        "geometry_feature_count": len(fc.get("features") or []) if fc else 0,
        "area": area,
    }


def normalize_record_geometries(table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row or {})

    if table_name == "parcels":
        source = row.get("geometry_geojson") or row.get("geometry") or row.get("geojson")
        summary = summarize_geojson(source)
        if summary.get("geometry_geojson"):
            row["geometry"] = summary["geometry_geojson"]
            row["geometry_geojson"] = summary["geometry_geojson"]
            row["geometry_bounds"] = summary["geometry_bounds"]
            row["bounds"] = summary["geometry_bounds"]
            row["geometry_center"] = summary["geometry_center"]
            row["center"] = summary["geometry_center"]
            row["bbox"] = summary["bbox"]
            row["geometry_type"] = summary["geometry_type"]
            row["geometry_feature_count"] = summary["geometry_feature_count"]
            if not row.get("area") and summary.get("area") is not None:
                row["area"] = summary["area"]
        return row

    def normalize_nested(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize_nested(item) for item in value]
        if isinstance(value, dict):
            result = {k: normalize_nested(v) for k, v in value.items()}
            for key in ("geometry", "geojson", "coveredGeom", "uncoveredGeom", "overlapGeom", "parcelas", "sprOn", "sprOff", "sprOnTracks", "sprOffTracks"):
                if key in result:
                    fc = normalize_geojson(result[key])
                    if fc:
                        result[key] = fc
            return result
        return value

    if table_name in {"aerial_analyses", "analysis_sessions", "analysis_data_points"}:
        for key in ("geometry", "geojson", "parcel_geometries", "parcel_results", "source_geojson", "result_geojson"):
            if key in row:
                row[key] = normalize_nested(row[key])
        source = row.get("geometry") or row.get("geojson") or row.get("source_geojson")
        summary = summarize_geojson(source)
        if summary.get("geometry_geojson"):
            row.setdefault("geometry_geojson", summary["geometry_geojson"])
            row.setdefault("geometry_bounds", summary["geometry_bounds"])
            row.setdefault("geometry_center", summary["geometry_center"])
            row.setdefault("bbox", summary["bbox"])
            row.setdefault("geometry_type", summary["geometry_type"])
            row.setdefault("geometry_feature_count", summary["geometry_feature_count"])
        return row

    return row
