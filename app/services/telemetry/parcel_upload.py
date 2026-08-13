from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import shapefile  # pyshp
from pyproj import CRS, Transformer
try:
    from shapely.errors import GEOSException
except Exception:  # Compatibilidad con versiones antiguas de Shapely
    GEOSException = Exception
from shapely.geometry import MultiPolygon, Polygon, mapping, shape as shapely_shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

try:
    from shapely.validation import make_valid as shapely_make_valid
except Exception:  # pragma: no cover
    shapely_make_valid = None

from app.utils.geojson_normalizer import summarize_geojson



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


def _repair_polygonal_geometry(geom: BaseGeometry) -> Optional[BaseGeometry]:
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
            clean_parts.extend([p for p in _polygonal_parts(cleaned) if p.is_valid and p.area > 0])

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
            polygons = [p for p in clean_parts if p.geom_type == "Polygon"]
            return MultiPolygon(polygons) if polygons else None
    return None


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
    repaired_geoms = []
    for geom in geoms:
        repaired = _repair_polygonal_geometry(geom)
        if repaired is not None and not repaired.is_empty:
            repaired_geoms.append(repaired)
    if not repaired_geoms:
        return 0.0

    try:
        union = unary_union(repaired_geoms)
        repaired_union = _repair_polygonal_geometry(union)
        if repaired_union is not None and not repaired_union.is_empty:
            union = repaired_union
        c = union.centroid
        metric = _utm_crs_from_lonlat(c.x, c.y)
        metric_geom = _to_crs(union, CRS.from_epsg(4326), metric)
        return float(metric_geom.area / 10000.0)
    except (GEOSException, ValueError, Exception):
        total = 0.0
        for geom in repaired_geoms:
            c = geom.centroid
            metric = _utm_crs_from_lonlat(c.x, c.y)
            metric_geom = _to_crs(geom, CRS.from_epsg(4326), metric)
            total += float(metric_geom.area / 10000.0)
        return total


def _record_props(reader: shapefile.Reader, record: Any) -> Dict[str, Any]:
    fields = [f[0] for f in reader.fields[1:]]
    values = list(record)
    props: Dict[str, Any] = {}
    for key, value in zip(fields, values):
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        props[str(key)] = value
    return props


# Nombres de columna de atributo (case-insensitive) que un shapefile de lote
# suele usar para identificar cada parcela individual. Se prueban en orden de
# prioridad; la primera columna con un valor no vacío gana.
_PARCEL_LABEL_PROPERTY_KEYS = [
    "parcela", "nombre_parcela", "nom_parcela", "parcel_name", "parcelname",
    "nombre", "name", "label", "etiqueta",
    "lote", "nom_lote", "lote_nom",
    "codigo", "cod_parcela", "cod_lote", "code",
    "id_parcela", "parcel_id", "plot", "plot_id", "clave", "id",
]


def _feature_label(properties: Optional[Dict[str, Any]], index: int, fallback_base: str) -> str:
    lower_map = {str(key).strip().lower(): value for key, value in (properties or {}).items()}
    for key in _PARCEL_LABEL_PROPERTY_KEYS:
        value = lower_map.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"none", "null", "nan"}:
            return text
    return f"{fallback_base} {index + 1}"


def _feature_result(feature: Dict[str, Any], label: str, source_crs_label: str) -> Dict[str, Any]:
    fc = _feature_collection([feature])
    summary = summarize_geojson(fc)
    area = summary.get("area") or round(_area_ha_wgs([feature]), 4)
    return {
        "name": label,
        "geometry": summary.get("geometry_geojson") or fc,
        "geometry_geojson": summary.get("geometry_geojson") or fc,
        "geometry_bounds": summary.get("geometry_bounds"),
        "geometry_center": summary.get("geometry_center"),
        "bbox": summary.get("bbox"),
        "geometry_type": summary.get("geometry_type"),
        "geometry_feature_count": summary.get("geometry_feature_count"),
        "geometry_source_crs": source_crs_label,
        "area": area,
        "properties": feature.get("properties") or {},
    }


def _drop_finca_contours(features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Descarta el contorno de la finca cuando viene como un polígono más.

    Muchos KML/SHP traen, además de los lotes, un polígono que ENVUELVE a casi
    todos (el contorno de la finca). Si se carga como un lote, se dibuja encima
    de todos los demás y, peor aún, duplica la superficie facturable. Se detecta
    de forma conservadora: un polígono es contorno solo si CONTIENE (>=90% de su
    área) al menos el 70% de los OTROS polígonos del archivo. Así se descarta el
    envolvente de la finca pero NO un lote que apenas contenga a unos pocos (por
    ejemplo, un lote con zonas de despoblación dentro, que sí debe conservarse).
    """
    n = len(features)
    if n < 4:
        return features

    geoms: List[Any] = []
    for feature in features:
        try:
            geoms.append(shapely_shape(feature["geometry"]).buffer(0))
        except Exception:
            geoms.append(None)

    dropped: set = set()
    for i in range(n):
        gi = geoms[i]
        if gi is None or gi.is_empty or gi.area <= 0:
            continue
        contains = 0
        for j in range(n):
            if i == j:
                continue
            gj = geoms[j]
            if gj is None or gj.is_empty or gj.area <= 0:
                continue
            # Prefiltro por bbox: si no se solapan, gi no contiene a gj.
            if gi.bounds[2] < gj.bounds[0] or gj.bounds[2] < gi.bounds[0]:
                continue
            if gi.bounds[3] < gj.bounds[1] or gj.bounds[3] < gi.bounds[1]:
                continue
            if gi.intersection(gj).area / gj.area >= 0.9:
                contains += 1
        if contains >= 0.7 * (n - 1):
            dropped.add(i)

    if not dropped:
        return features
    kept = [f for k, f in enumerate(features) if k not in dropped]
    # Nunca vaciar el archivo por completo: si todo quedó marcado (caso raro de
    # polígonos idénticos), se conserva la lista original.
    return kept or features


def _read_shapefile(shp_path: Path, base_name: str) -> Dict[str, Any]:
    src_crs = _read_prj(shp_path) or CRS.from_epsg(4326)
    reader = shapefile.Reader(str(shp_path))
    features: List[Dict[str, Any]] = []
    for sr in reader.iterShapeRecords():
        try:
            geom = shapely_shape(sr.shape.__geo_interface__)
        except Exception:
            continue
        if geom.is_empty:
            continue
        geom_wgs = _to_crs(geom, src_crs, CRS.from_epsg(4326))
        # Parcelas/lotes deben ser polígonos. Se reparan geometrías SHP
        # comunes con auto-intersecciones, anillos abiertos o multipartes
        # problemáticas para evitar errores TopologyException al guardar.
        geom_wgs = _repair_polygonal_geometry(geom_wgs)
        if geom_wgs is None or geom_wgs.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        features.append({
            "type": "Feature",
            "properties": _record_props(reader, sr.record),
            "geometry": mapping(geom_wgs),
        })
    if not features:
        raise ValueError("El shapefile no contiene polígonos válidos")
    features = _drop_finca_contours(features)
    source_crs_label = (
        str(src_crs.to_authority()[0] + ":" + src_crs.to_authority()[1])
        if src_crs and src_crs.to_authority()
        else str(src_crs or "EPSG:4326")
    )
    # Cada polígono del shapefile es su propia parcela: se conserva por
    # separado (con su propia área y atributos) en vez de fusionar todo el
    # lote en una sola geometría, para que el clic en el mapa seleccione
    # parcela por parcela.
    parcels = [
        _feature_result(feature, _feature_label(feature.get("properties"), index, base_name), source_crs_label)
        for index, feature in enumerate(features)
    ]
    return {"parcels": parcels}


def _parse_kml_text(text: str, base_name: str) -> Dict[str, Any]:
    root = ET.fromstring(text)
    features: List[Dict[str, Any]] = []
    for placemark in root.findall(".//{*}Placemark"):
        name_el = placemark.find("{*}name")
        name = name_el.text.strip() if name_el is not None and name_el.text else None
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
            geom = _repair_polygonal_geometry(Polygon(coords))
            if geom is not None and geom.is_valid and not geom.is_empty:
                features.append({
                    "type": "Feature",
                    "properties": {"name": name or "Parcela"},
                    "geometry": mapping(geom),
                    "_name": name,
                })
    if not features:
        raise ValueError("El KML/KMZ no contiene polígonos válidos")
    features = _drop_finca_contours(features)
    # Cada Placemark/Polygon del KML es su propia parcela, igual que con shapefiles.
    parcels = []
    for index, feature in enumerate(features):
        label = feature.pop("_name", None) or f"{base_name} {index + 1}"
        parcels.append(_feature_result(feature, label, "EPSG:4326"))
    return {"parcels": parcels}


def parse_parcel_file(path: Path, original_name: str, base_name: Optional[str] = None) -> Dict[str, Any]:
    suffix = original_name.lower().split(".")[-1]
    fallback_base = base_name or original_name.rsplit(".", 1)[0] or "Parcela"

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
            return _read_shapefile(shp_files[0], fallback_base)

    if suffix == "shp":
        return _read_shapefile(path, fallback_base)

    if suffix == "kml":
        return _parse_kml_text(path.read_text(encoding="utf-8", errors="ignore"), fallback_base)

    if suffix == "kmz":
        with zipfile.ZipFile(path) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                raise ValueError("El KMZ no contiene KML")
            with zf.open(kml_names[0]) as fh:
                return _parse_kml_text(fh.read().decode("utf-8", errors="ignore"), fallback_base)

    raise ValueError("Formato no soportado. Usa .zip, .shp, .kml o .kmz")
