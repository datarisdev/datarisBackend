from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import shapefile  # pyshp
from pyproj import CRS, Transformer
from shapely.geometry import Polygon, mapping, shape as shapely_shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from app.utils.geojson_normalizer import normalize_geojson


def _safe_extract(zip_file: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in zip_file.infolist():
        name = member.filename.replace("\\", "/").lstrip("/")
        if not name or name.startswith("../") or "/../" in name:
            continue
        target = (destination / name).resolve()
        if not str(target).startswith(str(root)):
            continue
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zip_file.open(member) as src, target.open("wb") as dst:
            dst.write(src.read())


def _read_prj(shp_path: Path) -> Optional[CRS]:
    prj_path = shp_path.with_suffix(".prj")
    if not prj_path.exists():
        return None
    try:
        return CRS.from_wkt(prj_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _transformer(src: Optional[CRS], dst: CRS) -> Optional[Transformer]:
    if src is None:
        src = CRS.from_epsg(4326)
    try:
        if src == dst:
            return None
        return Transformer.from_crs(src, dst, always_xy=True)
    except Exception:
        return None


def _to_crs(geom: BaseGeometry, src: Optional[CRS], dst: CRS) -> BaseGeometry:
    tr = _transformer(src, dst)
    if tr is None:
        return geom
    return transform(tr.transform, geom)


def _utm_crs_from_lonlat(lon: float, lat: float) -> CRS:
    zone = int((lon + 180) // 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def _feature_collection(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def _area_ha_wgs(features: List[Dict[str, Any]]) -> float:
    geoms = []
    for feat in features:
        try:
            geom = shapely_shape(feat.get("geometry"))
            if not geom.is_empty:
                geoms.append(geom)
        except Exception:
            continue
    if not geoms:
        return 0.0
    union = unary_union(geoms)
    c = union.centroid
    metric = _utm_crs_from_lonlat(c.x, c.y)
    metric_geom = _to_crs(union, CRS.from_epsg(4326), metric)
    return float(metric_geom.area / 10000.0)


def _record_props(reader: shapefile.Reader, record: Any) -> Dict[str, Any]:
    fields = [f[0] for f in reader.fields[1:]]
    values = list(record)
    props: Dict[str, Any] = {}
    for key, value in zip(fields, values):
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        props[str(key)] = value
    return props


def _read_shapefile(shp_path: Path) -> Dict[str, Any]:
    """Read SHP and return backend-normalized WGS84 GeoJSON.

    If the ZIP/SHP does not include .prj, normalize_geojson tries common CRS
    candidates and picks the one that produces valid WGS84 bounds. This avoids
    saving projected UTM/GTM coordinates that Mapbox cannot draw.
    """
    src_crs = _read_prj(shp_path)
    reader = shapefile.Reader(str(shp_path))
    raw_features: List[Dict[str, Any]] = []

    for sr in reader.iterShapeRecords():
        try:
            geom = shapely_shape(sr.shape.__geo_interface__)
        except Exception:
            continue
        if geom.is_empty:
            continue
        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        raw_features.append({
            "type": "Feature",
            "properties": _record_props(reader, sr.record),
            "geometry": mapping(geom),
        })

    if not raw_features:
        raise ValueError("El shapefile no contiene polígonos válidos")

    normalized = normalize_geojson(_feature_collection(raw_features), source_crs=src_crs, keep_all_geometry_types=False)
    if normalized.get("feature_count", 0) <= 0:
        raise ValueError("No se pudieron normalizar las geometrías del shapefile a coordenadas WGS84 válidas")

    return {
        "geometry": normalized["geometry"],
        "area": round(float(normalized.get("area") or 0), 4),
        "bounds": normalized.get("bounds"),
        "center": normalized.get("center"),
        "bbox": normalized.get("bbox"),
        "source_crs": normalized.get("source_crs"),
        "geometry_type": normalized.get("geometry_type"),
        "feature_count": normalized.get("feature_count"),
    }


def _parse_kml_text(text: str) -> Dict[str, Any]:
    root = ET.fromstring(text)
    features: List[Dict[str, Any]] = []
    for placemark in root.findall(".//{*}Placemark"):
        name_el = placemark.find("{*}name")
        name = name_el.text.strip() if name_el is not None and name_el.text else "Parcela"
        for polygon in placemark.findall(".//{*}Polygon"):
            coords_el = polygon.find(".//{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
            if coords_el is None:
                coords_el = polygon.find(".//{*}coordinates")
            if coords_el is None or not coords_el.text:
                continue
            coords: List[Tuple[float, float]] = []
            for chunk in coords_el.text.replace("\n", " ").split():
                parts = chunk.split(",")
                if len(parts) < 2:
                    continue
                try:
                    coords.append((float(parts[0]), float(parts[1])))
                except Exception:
                    continue
            if len(coords) < 3:
                continue
            if coords[0] != coords[-1]:
                coords.append(coords[0])
            geom = Polygon(coords)
            if geom.is_valid and not geom.is_empty:
                features.append({
                    "type": "Feature",
                    "properties": {"name": name, "Name": name},
                    "geometry": mapping(geom),
                })
    if not features:
        raise ValueError("El KML/KMZ no contiene polígonos válidos")
    normalized = normalize_geojson(_feature_collection(features), source_crs="EPSG:4326", keep_all_geometry_types=False)
    if normalized.get("feature_count", 0) <= 0:
        raise ValueError("No se pudieron normalizar las geometrías del KML/KMZ")
    return {
        "geometry": normalized["geometry"],
        "area": round(float(normalized.get("area") or 0), 4),
        "bounds": normalized.get("bounds"),
        "center": normalized.get("center"),
        "bbox": normalized.get("bbox"),
        "source_crs": normalized.get("source_crs"),
        "geometry_type": normalized.get("geometry_type"),
        "feature_count": normalized.get("feature_count"),
    }


def parse_parcel_file(path: Path, original_name: str) -> Dict[str, Any]:
    suffix = original_name.lower().split(".")[-1]

    if suffix == "zip":
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with zipfile.ZipFile(path) as zf:
                _safe_extract(zf, tmp)
            shp_files = [p for p in tmp.rglob("*.shp") if not p.name.startswith(".")]
            if not shp_files:
                raise ValueError("El ZIP no contiene archivos .shp")
            # Prioriza shapefiles que parezcan polígonos/lotes.
            shp_files.sort(key=lambda p: (0 if any(k in p.stem.lower() for k in ["polygon", "poligono", "parcel", "parcela", "lote", "area"]) else 1, str(p)))
            return _read_shapefile(shp_files[0])

    if suffix == "shp":
        return _read_shapefile(path)

    if suffix == "kml":
        return _parse_kml_text(path.read_text(encoding="utf-8", errors="ignore"))

    if suffix == "kmz":
        with zipfile.ZipFile(path) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("El KMZ no contiene KML")
            with zf.open(kml_names[0]) as fh:
                return _parse_kml_text(fh.read().decode("utf-8", errors="ignore"))

    raise ValueError("Formato no soportado. Usa .zip, .shp, .kml o .kmz")
