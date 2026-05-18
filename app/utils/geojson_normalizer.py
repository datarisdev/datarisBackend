from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pyproj import CRS, Transformer
from shapely.geometry import GeometryCollection, MultiLineString, MultiPoint, MultiPolygon, mapping, shape as shapely_shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

try:  # Shapely 2.x
    from shapely.validation import make_valid as shapely_make_valid
except Exception:  # pragma: no cover
    shapely_make_valid = None

WGS84 = CRS.from_epsg(4326)
MAX_MERCATOR_LATITUDE = 85.051129
MIN_BOUNDS_SPAN = 1e-8

# CRSs commonly seen in Central America / agricultural exports. The list is only
# used when a SHP/GeoJSON arrives without .prj metadata and coordinates are not
# already valid longitude/latitude values.
FALLBACK_SOURCE_CRS = [
    "EPSG:4326",
    "EPSG:3857",
    "EPSG:32614", "EPSG:32615", "EPSG:32616", "EPSG:32617",
    "EPSG:32714", "EPSG:32715", "EPSG:32716", "EPSG:32717",
    "EPSG:5367",  # Guatemala Transverse Mercator, when pyproj knows it.
]

GeometryMetrics = Dict[str, Any]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _parse_json_if_needed(value: Any) -> Any:
    current = value
    for _ in range(3):
        if not isinstance(current, str):
            return current
        trimmed = current.strip()
        if not trimmed:
            return None
        if trimmed[0] not in "[{":
            return current
        try:
            current = json.loads(trimmed)
        except Exception:
            return current
    return current


def _iter_feature_geometries(value: Any) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    parsed = _parse_json_if_needed(value)
    if not parsed:
        return

    if isinstance(parsed, dict):
        geom_type = parsed.get("type")
        if geom_type == "FeatureCollection":
            for feature in parsed.get("features") or []:
                if isinstance(feature, dict) and feature.get("geometry"):
                    yield feature.get("geometry"), _json_safe(feature.get("properties") or {})
            return
        if geom_type == "Feature":
            if parsed.get("geometry"):
                yield parsed.get("geometry"), _json_safe(parsed.get("properties") or {})
            return
        if geom_type in {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}:
            yield parsed, {}
            return

        # Common wrappers found in compatibility APIs and cached rows.
        for key in ("geometry", "geojson", "geometry_geojson", "data", "parcel"):
            child = parsed.get(key)
            if child:
                yield from _iter_feature_geometries(child)
                return

    if isinstance(parsed, list):
        # Raw ring / list of rings fallback.
        if parsed and isinstance(parsed[0], (list, tuple)):
            yield {"type": "Polygon", "coordinates": [parsed]}, {}


def _repair_geometry(geom: BaseGeometry) -> BaseGeometry:
    if geom.is_empty:
        return geom
    if geom.is_valid:
        return geom
    try:
        if shapely_make_valid:
            fixed = shapely_make_valid(geom)
            if fixed and not fixed.is_empty:
                return fixed
    except Exception:
        pass
    try:
        fixed = geom.buffer(0)
        if fixed and not fixed.is_empty:
            return fixed
    except Exception:
        pass
    return geom


def _transformer(src: CRS, dst: CRS = WGS84) -> Optional[Transformer]:
    try:
        if src == dst:
            return None
        return Transformer.from_crs(src, dst, always_xy=True)
    except Exception:
        return None


def _to_wgs84(geom: BaseGeometry, src: CRS) -> BaseGeometry:
    tr = _transformer(src, WGS84)
    if tr is None:
        return geom
    return transform(tr.transform, geom)


def _valid_wgs_bounds(geom: BaseGeometry) -> bool:
    if not geom or geom.is_empty:
        return False
    try:
        west, south, east, north = geom.bounds
    except Exception:
        return False
    values = [west, south, east, north]
    if not all(math.isfinite(float(v)) for v in values):
        return False
    if west < -180 or east > 180:
        return False
    if south < -MAX_MERCATOR_LATITUDE or north > MAX_MERCATOR_LATITUDE:
        return False
    if abs(east - west) < MIN_BOUNDS_SPAN or abs(north - south) < MIN_BOUNDS_SPAN:
        return False
    return True


def _score_wgs_geom(geom: BaseGeometry, crs_name: str) -> float:
    if not _valid_wgs_bounds(geom):
        return -1e9
    west, south, east, north = geom.bounds
    centroid = geom.centroid
    lng = float(centroid.x)
    lat = float(centroid.y)
    score = 20.0

    # Strong preference for Guatemala / Central America data because the product
    # currently works there, but still accepts valid data elsewhere.
    if -93.5 <= lng <= -88.0 and 13.0 <= lat <= 18.5:
        score += 100.0
    elif -105.0 <= lng <= -75.0 and 3.0 <= lat <= 25.0:
        score += 70.0
    elif -180 <= lng <= 180 and -60 <= lat <= 75:
        score += 30.0

    # Prefer explicit lon/lat when coordinates already look sane.
    if crs_name.upper().endswith("4326"):
        score += 15.0

    # Penalize enormous extents for a parcel/flight geometry.
    span = max(abs(east - west), abs(north - south))
    if span > 15:
        score -= 40.0
    elif span < 2:
        score += 10.0
    return score


def _coerce_crs(value: Any) -> Optional[CRS]:
    if not value:
        return None
    try:
        return CRS.from_user_input(value)
    except Exception:
        return None


def _infer_source_crs(raw_geoms: List[BaseGeometry], source_crs: Any = None) -> CRS:
    explicit = _coerce_crs(source_crs)
    if explicit:
        return explicit

    union = unary_union(raw_geoms) if len(raw_geoms) > 1 else raw_geoms[0]
    best: Tuple[float, CRS] | None = None
    for candidate in FALLBACK_SOURCE_CRS:
        crs = _coerce_crs(candidate)
        if not crs:
            continue
        try:
            wgs = _repair_geometry(_to_wgs84(union, crs))
            score = _score_wgs_geom(wgs, candidate)
            if best is None or score > best[0]:
                best = (score, crs)
        except Exception:
            continue

    if best and best[0] > -1e8:
        return best[1]
    return WGS84


def _extract_polygonal(geom: BaseGeometry) -> List[BaseGeometry]:
    if geom.is_empty:
        return []
    if geom.geom_type in {"Polygon", "MultiPolygon"}:
        return [geom]
    if geom.geom_type == "GeometryCollection":
        result: List[BaseGeometry] = []
        for child in getattr(geom, "geoms", []):
            result.extend(_extract_polygonal(child))
        return result
    return []


def _area_hectares(geoms_wgs: List[BaseGeometry]) -> float:
    polygons: List[BaseGeometry] = []
    for geom in geoms_wgs:
        polygons.extend(_extract_polygonal(geom))
    if not polygons:
        return 0.0
    union = unary_union(polygons) if len(polygons) > 1 else polygons[0]
    if union.is_empty:
        return 0.0
    c = union.centroid
    zone = int((float(c.x) + 180) // 6) + 1
    epsg = 32600 + zone if float(c.y) >= 0 else 32700 + zone
    try:
        metric = CRS.from_epsg(epsg)
        tr = Transformer.from_crs(WGS84, metric, always_xy=True)
        projected = transform(tr.transform, union)
        return round(float(projected.area) / 10000.0, 4)
    except Exception:
        return 0.0


def _bounds_and_center(geoms: List[BaseGeometry]) -> Tuple[Optional[Dict[str, float]], Optional[Dict[str, float]], Optional[List[float]]]:
    valid = [g for g in geoms if g and not g.is_empty]
    if not valid:
        return None, None, None
    union = unary_union(valid) if len(valid) > 1 else valid[0]
    if union.is_empty:
        return None, None, None
    west, south, east, north = [float(v) for v in union.bounds]
    if not all(math.isfinite(v) for v in [west, south, east, north]):
        return None, None, None
    centroid = union.centroid
    center = {"lat": float(centroid.y), "lng": float(centroid.x)}
    bounds = {"south": south, "north": north, "west": west, "east": east}
    bbox = [west, south, east, north]
    return bounds, center, bbox


def normalize_geojson(value: Any, source_crs: Any = None, keep_all_geometry_types: bool = True) -> GeometryMetrics:
    """
    Normalizes SHP/KML/GeoJSON-like geometry to a safe WGS84 GeoJSON FeatureCollection.

    Output geometry always uses GeoJSON order [lng, lat]. The frontend should only
    render this normalized geometry; area, bounds and center are calculated here.
    """
    raw_features = list(_iter_feature_geometries(value))
    raw_geoms: List[BaseGeometry] = []
    props_by_index: List[Dict[str, Any]] = []

    for geom_json, props in raw_features:
        try:
            geom = _repair_geometry(shapely_shape(geom_json))
        except Exception:
            continue
        if geom.is_empty:
            continue
        raw_geoms.append(geom)
        props_by_index.append(_json_safe(props or {}))

    if not raw_geoms:
        return {
            "geometry": {"type": "FeatureCollection", "features": []},
            "area": 0.0,
            "bounds": None,
            "center": None,
            "bbox": None,
            "feature_count": 0,
            "source_crs": None,
            "geometry_type": None,
        }

    src = _infer_source_crs(raw_geoms, source_crs)
    normalized_features: List[Dict[str, Any]] = []
    normalized_geoms: List[BaseGeometry] = []

    for geom, props in zip(raw_geoms, props_by_index):
        try:
            geom_wgs = _repair_geometry(_to_wgs84(geom, src))
        except Exception:
            continue
        if geom_wgs.is_empty or not _valid_wgs_bounds(geom_wgs):
            continue
        if not keep_all_geometry_types and geom_wgs.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        normalized_geoms.append(geom_wgs)
        normalized_features.append({
            "type": "Feature",
            "properties": props,
            "geometry": mapping(geom_wgs),
        })

    bounds, center, bbox = _bounds_and_center(normalized_geoms)
    geometry_type = None
    if normalized_geoms:
        types = sorted({g.geom_type for g in normalized_geoms})
        geometry_type = types[0] if len(types) == 1 else "Mixed"

    return {
        "geometry": {"type": "FeatureCollection", "features": normalized_features},
        "area": _area_hectares(normalized_geoms),
        "bounds": bounds,
        "center": center,
        "bbox": bbox,
        "feature_count": len(normalized_features),
        "source_crs": src.to_string() if src else None,
        "geometry_type": geometry_type,
    }


def enrich_geometry_row(row: Dict[str, Any], geometry_key: str = "geometry") -> Dict[str, Any]:
    result = dict(row or {})
    geometry = result.get("geometry_geojson") or result.get(geometry_key)
    if not geometry:
        return result
    try:
        normalized = normalize_geojson(geometry)
    except Exception:
        return result
    if normalized.get("feature_count", 0) <= 0:
        return result

    result[geometry_key] = normalized["geometry"]
    result["geometry_geojson"] = normalized["geometry"]
    result["geometry_bounds"] = normalized.get("bounds")
    result["bounds"] = normalized.get("bounds") or result.get("bounds")
    result["geometry_center"] = normalized.get("center")
    result["center"] = normalized.get("center") or result.get("center")
    result["bbox"] = normalized.get("bbox")
    result["geometry_type"] = normalized.get("geometry_type")
    result["geometry_feature_count"] = normalized.get("feature_count")
    if not result.get("area") and normalized.get("area"):
        result["area"] = normalized["area"]
    return result
