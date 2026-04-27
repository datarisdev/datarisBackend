from __future__ import annotations

import json
import math
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import shapefile  # pyshp
from pyproj import CRS, Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape as shapely_shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

MAX_POINT_GAP_METERS = 120.0
DEFAULT_SWATH_WIDTH_METERS = 16.0

FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "parcel_name": (
        "__dataris_parcel_name", "AREA_NAME", "AREA", "NAME", "NOMBRE", "NOMBRE_LOTE",
        "LOTE", "LOT", "LOT_NAME", "PARCELA", "PARCEL", "CAMPO", "BLOCK", "BLOQUE",
        "SECTOR", "FINCA", "CODIGO", "CODIGO_LOTE", "ID",
    ),
    "line": ("LIN", "LINE", "LINEA", "LÍNEA", "LINE_ID", "ID_LINEA", "IDLINEA", "TRACK", "TRACK_ID", "PASADA", "PASS", "TRAMO", "RUTA", "NUMERO", "NUMBER", "N", "ID"),
    "time": ("TIMEGPS", "GPS_TIME", "TIME", "HORA", "TIMESTAMP", "DATE_TIME", "DATETIME", "FECHA_HORA", "FECHAHORA"),
    "altitude": ("ALTm", "ALT", "ALT_M", "ALTITUDE", "ALTITUD", "ALTURA", "HEIGHT", "HEIGHT_M", "ELEV", "ELEVATION"),
    "speed": ("SPkph", "SPEED", "SPEED_KPH", "VEL", "VELOCIDAD", "VEL_KMH", "KMH", "KPH"),
    "swath_width": ("SW_WIDTHm", "SWATH", "WIDTH", "ANCHO", "ANCHO_M", "FAJA", "FAJA_M", "BOOM_WIDTH"),
    "volume": ("SPR_VOL", "VOLUME", "VOL", "VOLUMEN", "LITROS", "L_HA", "LTS", "DOSIS"),
}


@dataclass
class LayerGroup:
    id: str
    polygon_shp: Path
    spron_shp: Path
    sproff_shp: Optional[Path]


def _norm_key(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"[^a-zA-Z0-9]", "", raw).lower()


def _column_map(props: Dict[str, Any]) -> Dict[str, str]:
    return {_norm_key(k): k for k in props.keys()}


def _get_any(props: Dict[str, Any], aliases: Iterable[str], default: Any = None) -> Any:
    cmap = _column_map(props)
    for alias in aliases:
        key = cmap.get(_norm_key(alias))
        if key is not None:
            val = props.get(key)
            if val is not None and val != "":
                return val
    return default


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace(",", ".")
        n = float(value)
        if math.isnan(n) or math.isinf(n):
            return default
        return n
    except Exception:
        return default


def _jsonable(v: Any) -> Any:
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


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
    prj = shp_path.with_suffix(".prj")
    if not prj.exists():
        return None
    try:
        return CRS.from_wkt(prj.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _transformer(src: Optional[CRS], dst: CRS) -> Optional[Transformer]:
    src = src or CRS.from_epsg(4326)
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


def _utm_crs(lon: float, lat: float) -> CRS:
    zone = int((lon + 180) // 6) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def _reader_props(reader: shapefile.Reader, record: Any) -> Dict[str, Any]:
    fields = [f[0] for f in reader.fields[1:]]
    out: Dict[str, Any] = {}
    for k, v in zip(fields, list(record)):
        out[str(k)] = _jsonable(v)
    return out


def _read_features(shp_path: Path, group_id: str, layer_type: str) -> List[Dict[str, Any]]:
    src_crs = _read_prj(shp_path) or CRS.from_epsg(4326)
    reader = shapefile.Reader(str(shp_path))
    features: List[Dict[str, Any]] = []
    for idx, sr in enumerate(reader.iterShapeRecords()):
        try:
            geom_src = shapely_shape(sr.shape.__geo_interface__)
        except Exception:
            continue
        if geom_src.is_empty:
            continue
        geom_wgs = _to_crs(geom_src, src_crs, CRS.from_epsg(4326))
        props = _reader_props(reader, sr.record)
        props["__group"] = group_id
        props["__layer_type"] = layer_type
        props["__source_file"] = str(shp_path.name)
        props["__row_index"] = idx
        features.append({"geometry": geom_wgs, "properties": props})
    return features


def _find_groups(tmp: Path) -> List[LayerGroup]:
    shp_files = [p for p in tmp.rglob("*.shp") if not p.name.startswith(".")]
    if not shp_files:
        raise ValueError("El ZIP no contiene shapefiles .shp")

    by_dir: Dict[Path, Dict[str, Path]] = {}
    for p in shp_files:
        key = p.parent
        name = _norm_key(p.stem)
        item = by_dir.setdefault(key, {})
        if "spron" in name or ("spr" in name and "on" in name):
            item["spron"] = p
        elif "sproff" in name or ("spr" in name and "off" in name):
            item["sproff"] = p
        elif "polygon" in name or "poligono" in name or "parcel" in name or "parcela" in name or "lote" in name:
            item["polygon"] = p

    groups: List[LayerGroup] = []
    for d, item in by_dir.items():
        if item.get("polygon") and item.get("spron"):
            gid = d.name if d != tmp else "default"
            groups.append(LayerGroup(gid, item["polygon"], item["spron"], item.get("sproff")))

    if not groups:
        # Fallback global: toma el primer polygon y el primer SprOn encontrados.
        poly = next((p for p in shp_files if any(k in _norm_key(p.stem) for k in ["polygon", "poligono", "parcela", "parcel", "lote"])), None)
        spron = next((p for p in shp_files if "spron" in _norm_key(p.stem) or ("spr" in _norm_key(p.stem) and "on" in _norm_key(p.stem))), None)
        sproff = next((p for p in shp_files if "sproff" in _norm_key(p.stem) or ("spr" in _norm_key(p.stem) and "off" in _norm_key(p.stem))), None)
        if poly and spron:
            groups.append(LayerGroup("default", poly, spron, sproff))

    if not groups:
        raise ValueError("No se encontraron capas Polygon y SprOn en el ZIP")
    return groups


def _polygonal(geom: Optional[BaseGeometry]) -> Optional[BaseGeometry]:
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in {"Polygon", "MultiPolygon"}:
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type in {"Polygon", "MultiPolygon"}]
    if polys:
        return unary_union(polys)
    return None


def _avg(values: List[float]) -> float:
    values = [v for v in values if v]
    return sum(values) / len(values) if values else 0.0


def _line_key(props: Dict[str, Any]) -> str:
    val = _get_any(props, FIELD_ALIASES["line"], "default")
    return f"{props.get('__group', 'default')}:{val}"


def _time_key(props: Dict[str, Any]) -> str:
    return str(_get_any(props, FIELD_ALIASES["time"], props.get("__row_index", "")))


def _feature(geom: Optional[BaseGeometry], props: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if geom is None or geom.is_empty:
        return None
    return {"type": "Feature", "properties": props or {}, "geometry": mapping(geom)}


def _feature_collection_from_geoms(items: Iterable[Tuple[Optional[BaseGeometry], Dict[str, Any]]], limit: Optional[int] = None) -> Dict[str, Any]:
    features = []
    for geom, props in items:
        feat = _feature(geom, props)
        if feat:
            features.append(feat)
            if limit and len(features) >= limit:
                break
    return {"type": "FeatureCollection", "features": features}


def _build_tracks(points_metric: List[Dict[str, Any]], swath_width: float, parcel_union: Optional[BaseGeometry]) -> Tuple[List[Dict[str, Any]], List[Tuple[LineString, Dict[str, Any]]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for feat in points_metric:
        if not isinstance(feat["geometry"], Point):
            continue
        grouped.setdefault(_line_key(feat["properties"]), []).append(feat)

    line_infos: List[Dict[str, Any]] = []
    tracks: List[Tuple[LineString, Dict[str, Any]]] = []

    for key, rows in grouped.items():
        rows.sort(key=lambda r: (_time_key(r["properties"]), r["properties"].get("__row_index", 0)))
        segment: List[Dict[str, Any]] = []
        seg_index = 0

        def flush() -> None:
            nonlocal seg_index, segment
            if len(segment) < 2:
                segment = []
                return
            coords = [(p["geometry"].x, p["geometry"].y) for p in segment]
            line = LineString(coords)
            props = dict(segment[0]["properties"])
            props.update({
                "__dataris_track_key": key,
                "__dataris_segment": seg_index,
                "__dataris_point_count": len(segment),
                "tipo_capa": "Trayectoria SprOn",
            })
            sw = _num(_get_any(props, FIELD_ALIASES["swath_width"]), swath_width) or swath_width
            poly = line.buffer(sw / 2.0, cap_style=2, join_style=2)
            clipped = _polygonal(poly.intersection(parcel_union)) if parcel_union else _polygonal(poly)
            line_infos.append({
                "line": line,
                "polygon": clipped,
                "props": props,
                "altitude": _num(_get_any(props, FIELD_ALIASES["altitude"])),
                "speed": _num(_get_any(props, FIELD_ALIASES["speed"])),
            })
            tracks.append((line, props))
            seg_index += 1
            segment = []

        prev = None
        for row in rows:
            pt = row["geometry"]
            if prev is not None and pt.distance(prev) > MAX_POINT_GAP_METERS:
                flush()
            segment.append(row)
            prev = pt
        flush()

    return line_infos, tracks


def _overlap_union(polys: List[BaseGeometry]) -> Optional[BaseGeometry]:
    overlaps: List[BaseGeometry] = []
    for i, a in enumerate(polys):
        if a is None or a.is_empty:
            continue
        for b in polys[i + 1:]:
            if b is None or b.is_empty:
                continue
            if not a.bounds or not b.bounds:
                continue
            inter = a.intersection(b)
            if not inter.is_empty:
                p = _polygonal(inter)
                if p:
                    overlaps.append(p)
    return _polygonal(unary_union(overlaps)) if overlaps else None


def process_helicopter_zip(zip_path: Path | str, swath_width: float = DEFAULT_SWATH_WIDTH_METERS) -> Dict[str, Any]:
    zip_path = Path(zip_path)
    swath_width = float(swath_width or DEFAULT_SWATH_WIDTH_METERS)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp)

        groups = _find_groups(tmp)
        parcels_wgs: List[Dict[str, Any]] = []
        spron_wgs: List[Dict[str, Any]] = []
        sproff_wgs: List[Dict[str, Any]] = []

        for group in groups:
            parcels_wgs.extend(_read_features(group.polygon_shp, group.id, "Polygon"))
            spron_wgs.extend(_read_features(group.spron_shp, group.id, "SprOn"))
            if group.sproff_shp:
                sproff_wgs.extend(_read_features(group.sproff_shp, group.id, "SprOff"))

        parcels_wgs = [f for f in parcels_wgs if f["geometry"].geom_type in {"Polygon", "MultiPolygon"}]
        if not parcels_wgs:
            raise ValueError("No se encontraron polígonos de parcela válidos en la capa Polygon")
        if not spron_wgs:
            raise ValueError("No se encontraron puntos SprOn válidos")

        parcel_union_wgs = unary_union([f["geometry"] for f in parcels_wgs])
        c = parcel_union_wgs.centroid
        metric_crs = _utm_crs(c.x, c.y)

        def to_metric(feats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out = []
            for f in feats:
                out.append({"geometry": _to_crs(f["geometry"], CRS.from_epsg(4326), metric_crs), "properties": dict(f["properties"])})
            return out

        def to_wgs_geom(geom: Optional[BaseGeometry]) -> Optional[BaseGeometry]:
            if geom is None:
                return None
            return _to_crs(geom, metric_crs, CRS.from_epsg(4326))

        parcels_metric = to_metric(parcels_wgs)
        spron_metric = to_metric(spron_wgs)
        sproff_metric = to_metric(sproff_wgs)
        parcel_union = _polygonal(unary_union([f["geometry"] for f in parcels_metric]))

        line_infos, spron_tracks = _build_tracks(spron_metric, swath_width, parcel_union)
        _, sproff_tracks = _build_tracks(sproff_metric, swath_width, parcel_union) if sproff_metric else ([], [])

        line_polys = [li["polygon"] for li in line_infos if li.get("polygon") is not None and not li["polygon"].is_empty]
        coverage = _polygonal(unary_union(line_polys)) if line_polys else None
        overlap = _overlap_union(line_polys)

        parcel_results: List[Dict[str, Any]] = []
        for idx, row in enumerate(parcels_metric):
            geom = _polygonal(row["geometry"])
            if geom is None:
                continue
            area_ha = geom.area / 10000.0
            covered_geom = _polygonal(geom.intersection(coverage)) if coverage else None
            covered_ha = covered_geom.area / 10000.0 if covered_geom else 0.0
            uncovered_geom = _polygonal(geom.difference(coverage)) if coverage else geom
            overlap_geom = _polygonal(geom.intersection(overlap)) if overlap else None
            overlap_ha = overlap_geom.area / 10000.0 if overlap_geom else 0.0

            inside = [p for p in spron_metric if isinstance(p["geometry"], Point) and p["geometry"].within(geom)]
            altitudes = [_num(_get_any(p["properties"], FIELD_ALIASES["altitude"])) for p in inside]
            speeds = [_num(_get_any(p["properties"], FIELD_ALIASES["speed"])) for p in inside]
            line_keys = {_line_key(p["properties"]) for p in inside}

            source_props = dict(parcels_wgs[idx]["properties"]) if idx < len(parcels_wgs) else dict(row["properties"])
            name = _get_any(source_props, FIELD_ALIASES["parcel_name"]) or source_props.get("__group") or f"Área {idx + 1}"
            source_props["__dataris_parcel_name"] = str(name)
            vol = _num(_get_any(source_props, FIELD_ALIASES["volume"]), 0.0)

            parcel_results.append({
                "name": str(name),
                "totalHa": area_ha,
                "coveredHa": covered_ha,
                "coveredPct": (covered_ha / area_ha) * 100 if area_ha else 0.0,
                "uncoveredHa": max(area_ha - covered_ha, 0.0),
                "overlapHa": overlap_ha,
                "avgAltitude": _avg(altitudes),
                "avgSpeed": _avg(speeds),
                "uniqueLines": len(line_keys),
                "volume": vol,
                "sourceProperties": source_props,
                "coveredGeom": _feature(to_wgs_geom(covered_geom), {**source_props, "tipo_capa": "Zona aplicada", "hectareas_aplicadas": round(covered_ha, 4), "cobertura_pct": round((covered_ha / area_ha) * 100 if area_ha else 0.0, 2)}),
                "uncoveredGeom": _feature(to_wgs_geom(uncovered_geom), {**source_props, "tipo_capa": "Sin cubrir", "hectareas_sin_cubrir": round(max(area_ha - covered_ha, 0.0), 4)}),
                "overlapGeom": _feature(to_wgs_geom(overlap_geom), {**source_props, "tipo_capa": "Sobre-aplicado", "hectareas_sobre_aplicadas": round(overlap_ha, 4)}),
                "geometry": _feature(to_wgs_geom(geom), source_props),
            })

        total_ha = sum(p["totalHa"] for p in parcel_results)
        total_covered_ha = sum(p["coveredHa"] for p in parcel_results)
        total_uncovered_ha = max(total_ha - total_covered_ha, 0.0)
        overlap_ha = overlap.area / 10000.0 if overlap else 0.0
        speeds_all = [_num(_get_any(f["properties"], FIELD_ALIASES["speed"])) for f in spron_metric]
        alts_all = [_num(_get_any(f["properties"], FIELD_ALIASES["altitude"])) for f in spron_metric]
        line_keys_global = {_line_key(f["properties"]) for f in spron_metric}
        total_volume = sum(_num(_get_any(f["properties"], FIELD_ALIASES["volume"]), 0.0) for f in parcels_wgs)

        return {
            "parcelas": _feature_collection_from_geoms([(f["geometry"], f["properties"]) for f in parcels_wgs]),
            "sprOn": _feature_collection_from_geoms([(f["geometry"], f["properties"]) for f in spron_wgs], limit=4000),
            "sprOff": _feature_collection_from_geoms([(f["geometry"], f["properties"]) for f in sproff_wgs], limit=2000),
            "sprOnTracks": _feature_collection_from_geoms([(to_wgs_geom(g), {**p, "tipo_capa": "Trayectoria SprOn"}) for g, p in spron_tracks]),
            "sprOffTracks": _feature_collection_from_geoms([(to_wgs_geom(g), {**p, "tipo_capa": "Trayectoria SprOff"}) for g, p in sproff_tracks]),
            "sprOnUnion": _feature(to_wgs_geom(coverage), {"tipo_capa": "Zona aplicada"}),
            "overlapGeom": _feature(to_wgs_geom(overlap), {"tipo_capa": "Sobre-aplicado"}),
            "overlapHa": overlap_ha,
            "parcelResults": parcel_results,
            "totalHa": total_ha,
            "totalCoveredHa": total_covered_ha,
            "totalCoveredPct": (total_covered_ha / total_ha) * 100 if total_ha else 0.0,
            "totalUncoveredHa": total_uncovered_ha,
            "avgAltitude": _avg(alts_all),
            "avgSpeed": _avg(speeds_all),
            "totalLines": len(line_keys_global),
            "totalVolume": total_volume,
            "efficiency": (total_covered_ha / total_ha) * 100 if total_ha else 0.0,
            "speedRange": [min([v for v in speeds_all if v] or [0]), max([v for v in speeds_all if v] or [0])],
            "altitudeRange": [min([v for v in alts_all if v] or [0]), max([v for v in alts_all if v] or [0])],
        }
