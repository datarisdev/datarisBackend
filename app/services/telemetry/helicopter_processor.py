from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from numbers import Integral
from typing import Any, Dict, Iterable, List, Optional, Tuple

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree


MAX_POINT_GAP_METERS = 120.0
DEFAULT_SWATH_WIDTH_METERS = 16.0

# Solo afecta la geometría que se envía al navegador/BD, no los cálculos.
# Reduce muchísimo el tamaño del GeoJSON y mantiene las métricas exactas.
OUTPUT_SIMPLIFY_TOLERANCE_METERS = float(os.getenv("HELICOPTER_OUTPUT_SIMPLIFY_TOLERANCE_METERS", "0.25"))
MAX_SPRON_POINT_FEATURES = int(os.getenv("HELICOPTER_MAX_SPRON_POINTS", "1500"))
MAX_SPROFF_POINT_FEATURES = int(os.getenv("HELICOPTER_MAX_SPROFF_POINTS", "750"))


@dataclass
class HelicopterLayerGroup:
    id: str
    base_path: str
    polygon_shp: str
    spron_shp: str
    sproff_shp: str


@dataclass
class LinePolyInfo:
    poly: BaseGeometry
    track: BaseGeometry
    group: str
    lin: float
    line_key: str


def _normalise_zip_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def _shp_groups(names: Iterable[str]) -> List[HelicopterLayerGroup]:
    buckets: Dict[str, Dict[str, str]] = {}
    ids: Dict[str, str] = {}
    for raw in names:
        name = _normalise_zip_name(raw)
        lower = name.lower()
        if not lower.endswith(".shp"):
            continue
        layer = None
        if lower.endswith("polygon.shp"):
            layer = "polygon_shp"
            suffix = "polygon.shp"
        elif lower.endswith("spron.shp"):
            layer = "spron_shp"
            suffix = "spron.shp"
        elif lower.endswith("sproff.shp"):
            layer = "sproff_shp"
            suffix = "sproff.shp"
        if not layer:
            continue
        base = name[: -len(suffix)]
        group_id = Path(base.rstrip("/")).name or base.rstrip("/") or "default"
        buckets.setdefault(base, {})[layer] = name
        ids[base] = group_id

    groups: List[HelicopterLayerGroup] = []
    for base, item in buckets.items():
        if {"polygon_shp", "spron_shp", "sproff_shp"}.issubset(item):
            groups.append(
                HelicopterLayerGroup(
                    id=ids.get(base) or "default",
                    base_path=base,
                    polygon_shp=item["polygon_shp"],
                    spron_shp=item["spron_shp"],
                    sproff_shp=item["sproff_shp"],
                )
            )
    return sorted(groups, key=lambda g: g.id)


def _safe_extract(zip_file: zipfile.ZipFile, destination: Path) -> None:
    for member in zip_file.infolist():
        name = _normalise_zip_name(member.filename)
        if not name or name.startswith("../") or "/../" in name:
            continue
        target = (destination / name).resolve()
        if not str(target).startswith(str(destination.resolve())):
            continue
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zip_file.open(member) as src, target.open("wb") as dst:
            shutil_copy_buffer = 1024 * 1024
            while True:
                chunk = src.read(shutil_copy_buffer)
                if not chunk:
                    break
                dst.write(chunk)


def _read_layer(root: Path, relative_path: str, group_id: str, source_name: str) -> gpd.GeoDataFrame:
    path = root / _normalise_zip_name(relative_path)
    if not path.exists():
        raise ValueError(f"No se encontró {relative_path}")
    gdf = gpd.read_file(path)
    if gdf.empty:
        return gdf
    if gdf.crs is None:
        # Los logs de helicóptero suelen venir en lon/lat. Si no hay .prj, asumimos WGS84.
        gdf = gdf.set_crs("EPSG:4326", allow_override=True)
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf["__group"] = group_id
    gdf["__source"] = source_name
    return gdf


def _metric_crs(gdf: gpd.GeoDataFrame) -> Any:
    try:
        estimated = gdf.estimate_utm_crs()
        if estimated:
            return estimated
    except Exception:
        pass
    return "EPSG:3857"


def _to_wgs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf
    if gdf.crs is None:
        return gdf.set_crs("EPSG:4326", allow_override=True)
    return gdf.to_crs("EPSG:4326")


def _jsonable_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def _feature_collection(
    gdf: gpd.GeoDataFrame,
    max_point_features: Optional[int] = None,
    simplify_tolerance_m: float = 0.0,
) -> Dict[str, Any]:
    """Convierte a GeoJSON con muestreo de puntos y simplificación visual opcional.

    La simplificación se aplica únicamente a geometrías de salida para mapa/historial.
    Los cálculos de hectáreas, cobertura y traslape se hacen antes con geometría completa.
    """
    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}

    gdf_src = gdf.copy()

    # Evita mandar decenas de miles de puntos al frontend. Las trayectorias reales van en
    # sprOnTracks/sprOffTracks, que son mucho más livianas para renderizar.
    if max_point_features and len(gdf_src) > max_point_features:
        stride = max(1, math.ceil(len(gdf_src) / max_point_features))
        gdf_src = gdf_src.iloc[::stride].copy()

    if simplify_tolerance_m and simplify_tolerance_m > 0:
        try:
            if gdf_src.crs is not None and str(gdf_src.crs).upper() != "EPSG:4326":
                non_points = ~gdf_src.geometry.geom_type.isin(["Point", "MultiPoint"])
                if non_points.any():
                    gdf_src.loc[non_points, "geometry"] = gdf_src.loc[non_points, "geometry"].simplify(
                        simplify_tolerance_m,
                        preserve_topology=True,
                    )
        except Exception:
            pass

    gdf_wgs = _to_wgs(gdf_src)
    features: List[Dict[str, Any]] = []
    for feature in gdf_wgs.iterfeatures(na="null", drop_id=True):
        geom = feature.get("geometry")
        if not geom:
            continue
        props = {k: _jsonable_value(v) for k, v in (feature.get("properties") or {}).items()}
        features.append({"type": "Feature", "properties": props, "geometry": geom})
    return {"type": "FeatureCollection", "features": features}


def _geom_to_wgs(geom: Optional[BaseGeometry], from_crs: Any) -> Optional[Dict[str, Any]]:
    if geom is None or geom.is_empty:
        return None
    geom = _polygonal(geom)
    if geom is None or geom.is_empty:
        return None

    # Optimización de salida: conserva cálculos exactos y solo simplifica el dibujo.
    if OUTPUT_SIMPLIFY_TOLERANCE_METERS > 0:
        try:
            geom = geom.simplify(OUTPUT_SIMPLIFY_TOLERANCE_METERS, preserve_topology=True)
        except Exception:
            pass

    transformer = Transformer.from_crs(from_crs, "EPSG:4326", always_xy=True)
    converted = transform(transformer.transform, geom)
    return {"type": "Feature", "properties": {}, "geometry": mapping(converted)}


def _geometry_to_feature(geom: Optional[BaseGeometry], props: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if geom is None or geom.is_empty:
        return None
    return {"type": "Feature", "properties": props or {}, "geometry": mapping(geom)}


def _polygonal(geom: Optional[BaseGeometry]) -> Optional[BaseGeometry]:
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys: List[Polygon] = []
        for item in geom.geoms:
            p = _polygonal(item)
            if isinstance(p, Polygon):
                polys.append(p)
            elif isinstance(p, MultiPolygon):
                polys.extend(list(p.geoms))
        if not polys:
            return None
        return unary_union(polys)
    return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _line_key(row: pd.Series) -> Optional[str]:
    lin = row.get("LIN")
    if lin is None or pd.isna(lin):
        return None
    return f"{row.get('__group', 'default')}:{lin}"


def _split_coords(coords: List[Tuple[float, float]], max_gap: float = MAX_POINT_GAP_METERS) -> List[List[Tuple[float, float]]]:
    if len(coords) < 2:
        return []
    segments: List[List[Tuple[float, float]]] = []
    current = [coords[0]]
    for prev, nxt in zip(coords[:-1], coords[1:]):
        gap = math.hypot(nxt[0] - prev[0], nxt[1] - prev[1])
        if gap > max_gap:
            if len(current) >= 2:
                segments.append(current)
            current = [nxt]
        else:
            current.append(nxt)
    if len(current) >= 2:
        segments.append(current)
    return segments


def _build_line_polygons(spr_metric: gpd.GeoDataFrame, swath_width: float) -> Tuple[List[LinePolyInfo], gpd.GeoDataFrame]:
    if spr_metric.empty:
        return [], gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=spr_metric.crs)

    sort_cols = [c for c in ["__group", "LIN", "TIMEGPS"] if c in spr_metric.columns]
    sorted_gdf = spr_metric.sort_values(sort_cols).copy() if sort_cols else spr_metric.copy()

    buckets: Dict[str, Dict[str, Any]] = {}
    for _, row in sorted_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty or geom.geom_type != "Point":
            continue
        key = _line_key(row) or f"{row.get('__group', 'default')}:0"
        bucket = buckets.setdefault(
            key,
            {"group": row.get("__group", "default"), "lin": _num(row.get("LIN"), 0.0), "coords": [], "widths": []},
        )
        bucket["coords"].append((geom.x, geom.y))
        w = _num(row.get("SW_WIDTHm"), 0.0)
        if w > 0:
            bucket["widths"].append(w)

    line_infos: List[LinePolyInfo] = []
    track_records: List[Dict[str, Any]] = []
    for key, bucket in buckets.items():
        coords = bucket["coords"]
        if len(coords) < 2:
            continue
        segments = _split_coords(coords)
        width = sum(bucket["widths"]) / len(bucket["widths"]) if bucket["widths"] else (swath_width or DEFAULT_SWATH_WIDTH_METERS)
        for idx, seg in enumerate(segments):
            if len(seg) < 2:
                continue
            line = LineString(seg)
            if line.length <= 0:
                continue
            poly = line.buffer(width / 2.0, cap_style=2, join_style=2)
            info_key = f"{key}:{idx}"
            line_infos.append(
                LinePolyInfo(poly=poly, track=line, group=str(bucket["group"]), lin=float(bucket["lin"]), line_key=info_key)
            )
            track_records.append({"LIN": bucket["lin"], "__group": bucket["group"], "lineKey": info_key, "geometry": line})

    tracks = gpd.GeoDataFrame(track_records, geometry="geometry", crs=spr_metric.crs) if track_records else gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=spr_metric.crs)
    return line_infos, tracks


def _line_infos_from_polygons(polys_metric: gpd.GeoDataFrame) -> List[LinePolyInfo]:
    """Como _build_line_polygons, pero para fuentes que ya traen el polígono de
    cobertura por pasada (p.ej. el KML "Spray Coverage" de Satloc/AgGPS) en vez
    de puntos crudos que haya que bufferear."""
    infos: List[LinePolyInfo] = []
    for idx, row in polys_metric.iterrows():
        geom = _polygonal(row.geometry)
        if geom is None or geom.is_empty:
            continue
        group = str(row.get("__group", "default"))
        lin = _num(row.get("LIN"), 0.0)
        infos.append(LinePolyInfo(poly=geom, track=geom, group=group, lin=lin, line_key=f"{group}:{lin}:{idx}"))
    return infos


def _clip_line_polys(line_infos: List[LinePolyInfo], parcel_union: Optional[BaseGeometry]) -> List[LinePolyInfo]:
    if not parcel_union or parcel_union.is_empty:
        return line_infos
    clipped: List[LinePolyInfo] = []
    for info in line_infos:
        if info.poly.is_empty or not info.poly.bounds:
            continue
        try:
            inter = info.poly.intersection(parcel_union)
        except Exception:
            inter = info.poly
        inter = _polygonal(inter)
        if inter is not None and not inter.is_empty:
            clipped.append(LinePolyInfo(poly=inter, track=info.track, group=info.group, lin=info.lin, line_key=info.line_key))
    return clipped


def _coverage_union(line_infos: List[LinePolyInfo]) -> Optional[BaseGeometry]:
    polys = [_polygonal(info.poly) for info in line_infos if info.poly is not None and not info.poly.is_empty]
    polys = [p for p in polys if p is not None and not p.is_empty]
    if not polys:
        return None
    return _polygonal(unary_union(polys))


def _query_tree_indices(
    tree: STRtree,
    geom_id_to_idx: Dict[int, int],
    geom: BaseGeometry,
) -> Iterable[int]:
    """Compatibilidad Shapely 1.x/2.x: query devuelve geometrías o índices."""
    for candidate in tree.query(geom):
        if isinstance(candidate, Integral):
            yield int(candidate)
        else:
            idx = geom_id_to_idx.get(id(candidate))
            if idx is not None:
                yield idx


def _point_stats_by_parcel(spron_metric: gpd.GeoDataFrame, parcelas_metric: gpd.GeoDataFrame) -> Dict[Any, Dict[str, Any]]:
    """Calcula altitud, velocidad y líneas por parcela con spatial join una sola vez.

    Antes se hacía un within() contra todos los puntos por cada parcela. En archivos
    grandes eso escala muy mal. El spatial join usa índice espacial y reduce mucho
    el tiempo sin cambiar el resultado.
    """
    if spron_metric.empty or parcelas_metric.empty:
        return {}

    cols = ["geometry"]
    for c in ["ALTm", "SPkph", "LIN", "__group", "__line_key"]:
        if c in spron_metric.columns:
            cols.append(c)

    points = spron_metric[cols].copy()
    parcels = parcelas_metric[["geometry"]].copy()

    try:
        joined = gpd.sjoin(points, parcels, how="inner", predicate="within")
    except Exception:
        # Fallback seguro si no hay índice espacial disponible.
        stats: Dict[Any, Dict[str, Any]] = {}
        prepared = [(idx, prep(_polygonal(row.geometry) or row.geometry)) for idx, row in parcelas_metric.iterrows()]
        for point_idx, row in points.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            for parcel_idx, prepared_geom in prepared:
                try:
                    if prepared_geom.contains(geom):
                        bucket = stats.setdefault(parcel_idx, {"altitudes": [], "speeds": [], "lineKeys": set()})
                        alt = _num(row.get("ALTm"), 0.0)
                        spd = _num(row.get("SPkph"), 0.0)
                        if alt != 0:
                            bucket["altitudes"].append(alt)
                        if spd != 0:
                            bucket["speeds"].append(spd)
                        key = row.get("__line_key") or _line_key(row)
                        if key:
                            bucket["lineKeys"].add(key)
                        break
                except Exception:
                    continue
        return stats

    stats: Dict[Any, Dict[str, Any]] = {}
    if joined.empty:
        return stats

    for parcel_idx, group in joined.groupby("index_right", sort=False):
        altitudes = [_num(v) for v in group.get("ALTm", pd.Series(dtype=float)).tolist() if _num(v) != 0]
        speeds = [_num(v) for v in group.get("SPkph", pd.Series(dtype=float)).tolist() if _num(v) != 0]
        if "__line_key" in group.columns:
            line_keys = {v for v in group["__line_key"].dropna().tolist() if v}
        else:
            line_keys = {_line_key(r) for _, r in group.iterrows()}
            line_keys.discard(None)
        stats[parcel_idx] = {"altitudes": altitudes, "speeds": speeds, "lineKeys": line_keys}
    return stats


def _overlap_union(line_infos: List[LinePolyInfo]) -> Optional[BaseGeometry]:
    grouped: Dict[str, List[LinePolyInfo]] = {}
    for info in line_infos:
        grouped.setdefault(info.group, []).append(info)

    overlaps: List[BaseGeometry] = []
    for lines in grouped.values():
        lines.sort(key=lambda x: (x.lin, x.line_key))
        for i, a in enumerate(lines):
            for b in lines[i + 1 :]:
                diff = b.lin - a.lin
                if diff > 1:
                    break
                if diff < 1:
                    continue
                if not a.poly.bounds or not b.poly.bounds:
                    continue
                ax1, ay1, ax2, ay2 = a.poly.bounds
                bx1, by1, bx2, by2 = b.poly.bounds
                if ax1 > bx2 or ax2 < bx1 or ay1 > by2 or ay2 < by1:
                    continue
                try:
                    inter = _polygonal(a.poly.intersection(b.poly))
                    if inter is not None and not inter.is_empty:
                        overlaps.append(inter)
                except Exception:
                    continue
    if not overlaps:
        return None
    return _polygonal(unary_union(overlaps))


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _assemble_result(
    parcelas: gpd.GeoDataFrame,
    spron: gpd.GeoDataFrame,
    sproff: gpd.GeoDataFrame,
    swath_width: float,
    coverage_polys: Optional[gpd.GeoDataFrame] = None,
) -> Dict[str, Any]:
    """Cálculo de cobertura/traslape/KPIs compartido por el flujo shapefile
    (Polygon/SprOn/SprOff) y el flujo KML (Spray Areas/Coverage/Points).

    `spron` siempre son los puntos de telemetría (altitud, velocidad, línea) usados
    para las estadísticas por parcela. Si `coverage_polys` viene dado, los polígonos
    de pasada se toman de ahí (ya vienen calculados, p.ej. desde un KML) en vez de
    bufferear los puntos de `spron`.
    """
    metric_crs = _metric_crs(parcelas)
    parcelas_metric = parcelas.to_crs(metric_crs)
    spron_metric = spron.to_crs(metric_crs) if not spron.empty else spron
    sproff_metric = sproff.to_crs(metric_crs) if not sproff.empty else sproff

    if not spron_metric.empty:
        spron_metric["__line_key"] = spron_metric.apply(_line_key, axis=1)
    if not sproff_metric.empty:
        sproff_metric["__line_key"] = sproff_metric.apply(_line_key, axis=1)

    parcel_union = _polygonal(unary_union(list(parcelas_metric.geometry)))
    if coverage_polys is not None and not coverage_polys.empty:
        line_infos_raw = _line_infos_from_polygons(coverage_polys.to_crs(metric_crs))
        _, spron_tracks_metric = _build_line_polygons(spron_metric, swath_width)
    else:
        line_infos_raw, spron_tracks_metric = _build_line_polygons(spron_metric, swath_width)
    line_infos = _clip_line_polys(line_infos_raw, parcel_union)
    coverage = _coverage_union(line_infos)
    overlap = _overlap_union(line_infos)

    _, sproff_tracks_metric = _build_line_polygons(sproff_metric, swath_width) if not sproff_metric.empty else ([], gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=metric_crs))

    point_stats = _point_stats_by_parcel(spron_metric, parcelas_metric)

    parcel_results: List[Dict[str, Any]] = []
    for idx, row in parcelas_metric.iterrows():
        geom = _polygonal(row.geometry)
        if geom is None or geom.is_empty:
            continue
        area_ha = geom.area / 10000.0
        covered_geom = _polygonal(geom.intersection(coverage)) if coverage else None
        covered_ha = covered_geom.area / 10000.0 if covered_geom else 0.0
        uncovered_geom = _polygonal(geom.difference(coverage)) if coverage else geom
        overlap_geom = _polygonal(geom.intersection(overlap)) if overlap else None
        overlap_ha = overlap_geom.area / 10000.0 if overlap_geom else 0.0

        stats = point_stats.get(idx, {})
        altitudes = stats.get("altitudes", [])
        speeds = stats.get("speeds", [])
        line_keys = stats.get("lineKeys", set())

        source_row = parcelas.loc[idx] if idx in parcelas.index else row
        name = (
            source_row.get("AREA_NAME")
            or source_row.get("Name")
            or source_row.get("NOMBRE")
            or source_row.get("LOTE")
            or source_row.get("__group")
            or f"Área {len(parcel_results) + 1}"
        )
        vol = _num(source_row.get("SPR_VOL"), 0.0)

        parcel_results.append(
            {
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
                "coveredGeom": _geom_to_wgs(covered_geom, metric_crs),
                "uncoveredGeom": _geom_to_wgs(uncovered_geom, metric_crs),
                "overlapGeom": _geom_to_wgs(overlap_geom, metric_crs),
                "geometry": _geom_to_wgs(geom, metric_crs),
            }
        )

    total_ha = sum(p["totalHa"] for p in parcel_results)
    total_covered_ha = sum(p["coveredHa"] for p in parcel_results)
    total_uncovered_ha = max(total_ha - total_covered_ha, 0.0)
    overlap_ha = overlap.area / 10000.0 if overlap else 0.0

    speeds_all = [_num(v) for v in spron.get("SPkph", pd.Series(dtype=float)).tolist() if _num(v) != 0]
    alts_all = [_num(v) for v in spron.get("ALTm", pd.Series(dtype=float)).tolist() if _num(v) != 0]
    if "__line_key" in spron_metric.columns:
        line_keys_global = {v for v in spron_metric["__line_key"].dropna().tolist() if v}
    else:
        line_keys_global = {_line_key(r) for _, r in spron.iterrows()}
        line_keys_global.discard(None)
    total_volume = float(parcelas.get("SPR_VOL", pd.Series(dtype=float)).apply(lambda v: _num(v, 0.0)).sum()) if "SPR_VOL" in parcelas.columns else 0.0

    return {
        "parcelas": _feature_collection(parcelas_metric, simplify_tolerance_m=OUTPUT_SIMPLIFY_TOLERANCE_METERS),
        # Guardamos puntos limitados para no inflar demasiado compat_db.json; las líneas correctas van en sprOnTracks/sprOffTracks.
        "sprOn": _feature_collection(spron_metric, max_point_features=MAX_SPRON_POINT_FEATURES),
        "sprOff": _feature_collection(sproff_metric, max_point_features=MAX_SPROFF_POINT_FEATURES),
        "sprOnTracks": _feature_collection(spron_tracks_metric, simplify_tolerance_m=OUTPUT_SIMPLIFY_TOLERANCE_METERS),
        "sprOffTracks": _feature_collection(sproff_tracks_metric, simplify_tolerance_m=OUTPUT_SIMPLIFY_TOLERANCE_METERS),
        "sprOnUnion": _geom_to_wgs(coverage, metric_crs),
        "overlapGeom": _geom_to_wgs(overlap, metric_crs),
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
        "speedRange": [min(speeds_all), max(speeds_all)] if speeds_all else [0, 0],
        "altitudeRange": [min(alts_all), max(alts_all)] if alts_all else [0, 0],
    }


def process_helicopter_zip(zip_path: Path, swath_width: float = DEFAULT_SWATH_WIDTH_METERS) -> Dict[str, Any]:
    if swath_width <= 0:
        swath_width = DEFAULT_SWATH_WIDTH_METERS

    with tempfile.TemporaryDirectory(prefix="dataris-heli-") as tmp_name:
        tmp = Path(tmp_name)
        with zipfile.ZipFile(zip_path) as zf:
            groups = _shp_groups(zf.namelist())
            if not groups:
                kml_bytes = _find_kml_bytes_in_zip(zf)
                if kml_bytes is not None:
                    return process_helicopter_kml(kml_bytes, swath_width)
                raise ValueError(
                    "El ZIP debe contener grupos completos Polygon.shp, SprOn.shp y SprOff.shp, "
                    "o un KML de reporte de aspersión con las carpetas Spray Areas y Spray Coverage"
                )
            _safe_extract(zf, tmp)

        parcelas_parts: List[gpd.GeoDataFrame] = []
        spron_parts: List[gpd.GeoDataFrame] = []
        sproff_parts: List[gpd.GeoDataFrame] = []
        for group in groups:
            parcelas_parts.append(_read_layer(tmp, group.polygon_shp, group.id, "Polygon"))
            spron_parts.append(_read_layer(tmp, group.spron_shp, group.id, "SprOn"))
            sproff_parts.append(_read_layer(tmp, group.sproff_shp, group.id, "SprOff"))

        parcelas = pd.concat(parcelas_parts, ignore_index=True) if parcelas_parts else gpd.GeoDataFrame()
        spron = pd.concat(spron_parts, ignore_index=True) if spron_parts else gpd.GeoDataFrame()
        sproff = pd.concat(sproff_parts, ignore_index=True) if sproff_parts else gpd.GeoDataFrame()

        parcelas = gpd.GeoDataFrame(parcelas, geometry="geometry", crs=parcelas_parts[0].crs if parcelas_parts else "EPSG:4326")
        spron = gpd.GeoDataFrame(spron, geometry="geometry", crs=spron_parts[0].crs if spron_parts else parcelas.crs)
        sproff = gpd.GeoDataFrame(sproff, geometry="geometry", crs=sproff_parts[0].crs if sproff_parts else parcelas.crs)

        parcelas = parcelas[parcelas.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        if parcelas.empty:
            raise ValueError("No se encontraron polígonos de parcela válidos en la capa Polygon")
        if spron.empty:
            raise ValueError("No se encontraron puntos SprOn válidos")

        return _assemble_result(parcelas, spron, sproff, swath_width)


def _find_kml_bytes_in_zip(zf: zipfile.ZipFile) -> Optional[bytes]:
    names = [n for n in zf.namelist() if not n.endswith("/")]
    kml_names = [n for n in names if n.lower().endswith(".kml")]
    if kml_names:
        return zf.read(kml_names[0])
    kmz_names = [n for n in names if n.lower().endswith(".kmz")]
    if kmz_names:
        with zipfile.ZipFile(io.BytesIO(zf.read(kmz_names[0]))) as inner:
            inner_kml = [n for n in inner.namelist() if n.lower().endswith(".kml")]
            if inner_kml:
                return inner.read(inner_kml[0])
    return None


_KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}
# Reportes de aspersión (Satloc/AgGPS) suelen venir en Windows-1252, sin declararlo,
# y con caracteres crudos (p.ej. el símbolo de grado) que rompen un parseo UTF-8 estricto.
_KML_DESC_FIELD_RE = re.compile(r"<b>(.*?)</b>&nbsp;:&nbsp;\s*([^&<]+)")


def _decode_kml_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _kml_child_folder(parent: ET.Element, name: str) -> Optional[ET.Element]:
    target = name.strip().lower()
    for folder in parent.findall("kml:Folder", _KML_NS):
        name_el = folder.find("kml:name", _KML_NS)
        if name_el is not None and (name_el.text or "").strip().lower() == target:
            return folder
    return None


def _kml_coords(text: Optional[str]) -> List[Tuple[float, float]]:
    if not text:
        return []
    points: List[Tuple[float, float]] = []
    # Algunos exportadores (Satloc) dejan espacios sueltos alrededor de la coma
    # dentro de una misma tupla, p.ej. "-90.829186, 14.080098" en vez de "lon,lat".
    # Se normaliza antes de separar por espacios para no partir una tupla en dos.
    normalized = re.sub(r"\s*,\s*", ",", text.strip())
    for token in normalized.split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return points


def _kml_placemark_polygon(pm: ET.Element) -> Optional[Polygon]:
    coords_el = pm.find(".//kml:Polygon//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates", _KML_NS)
    ring = _kml_coords(coords_el.text if coords_el is not None else None)
    if len(ring) < 3:
        return None
    try:
        polygon = Polygon(ring)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon if polygon is not None and not polygon.is_empty else None
    except Exception:
        return None


def _kml_placemark_point(pm: ET.Element) -> Optional[Tuple[float, float]]:
    coords_el = pm.find(".//kml:Point/kml:coordinates", _KML_NS)
    coords = _kml_coords(coords_el.text if coords_el is not None else None)
    return coords[0] if coords else None


def _parse_kml_description_fields(text: Optional[str]) -> Dict[str, str]:
    if not text:
        return {}
    return {key.strip(): value.strip() for key, value in _KML_DESC_FIELD_RE.findall(text)}


def _kml_line_number(folder_name: Optional[str], fallback: int) -> float:
    if folder_name:
        match = re.search(r"(\d+)", folder_name)
        if match:
            return float(match.group(1))
    return float(fallback)


def process_helicopter_kml(kml_bytes: bytes, swath_width: float = DEFAULT_SWATH_WIDTH_METERS) -> Dict[str, Any]:
    """Procesa un reporte de aspersión KML (formato Satloc/AgGPS "Spray Report"),

    la alternativa a los shapefiles Polygon/SprOn/SprOff que exportan los mismos
    sistemas de registro de vuelo. El KML ya trae los polígonos de cobertura por
    pasada calculados (carpeta "Spray Coverage"), así que no hace falta bufferear
    puntos crudos para esa parte; solo se usan los puntos de "Spray Points" para
    las estadísticas de altitud/velocidad/líneas por parcela.
    """
    if swath_width <= 0:
        swath_width = DEFAULT_SWATH_WIDTH_METERS

    root = ET.fromstring(_decode_kml_text(kml_bytes))
    document = root.find("kml:Document", _KML_NS)
    if document is None:
        document = root

    areas_folder = _kml_child_folder(document, "Spray Areas")
    coverage_folder = _kml_child_folder(document, "Spray Coverage")
    points_folder = _kml_child_folder(document, "Spray Points")
    if areas_folder is None or coverage_folder is None:
        raise ValueError(
            "El KML debe contener las carpetas 'Spray Areas' y 'Spray Coverage' de un reporte de aspersión de helicóptero"
        )

    area_rows: List[Dict[str, Any]] = []
    for pm in areas_folder.findall("kml:Placemark", _KML_NS):
        polygon = _kml_placemark_polygon(pm)
        if polygon is None:
            continue
        name_el = pm.find("kml:name", _KML_NS)
        area_rows.append(
            {"geometry": polygon, "Name": (name_el.text if name_el is not None else None) or "Zona de aspersión"}
        )
    if not area_rows:
        raise ValueError("No se encontró el polígono de la zona de aspersión en 'Spray Areas'")
    parcelas = gpd.GeoDataFrame(area_rows, geometry="geometry", crs="EPSG:4326")

    on_rows: List[Dict[str, Any]] = []
    off_rows: List[Dict[str, Any]] = []
    for line_idx, line_folder in enumerate(coverage_folder.findall("kml:Folder", _KML_NS), start=1):
        name_el = line_folder.find("kml:name", _KML_NS)
        line_number = _kml_line_number(name_el.text if name_el is not None else None, line_idx)
        for pm in line_folder.findall("kml:Placemark", _KML_NS):
            polygon = _kml_placemark_polygon(pm)
            if polygon is None:
                continue
            style_el = pm.find("kml:styleUrl", _KML_NS)
            style = (style_el.text or "") if style_el is not None else ""
            row = {"geometry": polygon, "LIN": line_number, "__group": "default"}
            if "sprayoff" in style.lower():
                off_rows.append(row)
            else:
                on_rows.append(row)
    if not on_rows and not off_rows:
        raise ValueError("No se encontraron polígonos de cobertura en 'Spray Coverage'")

    empty_polys = gpd.GeoDataFrame(columns=["geometry", "LIN", "__group"], geometry="geometry", crs="EPSG:4326")
    coverage_polys = gpd.GeoDataFrame(on_rows, geometry="geometry", crs="EPSG:4326") if on_rows else empty_polys
    sproff_polys = gpd.GeoDataFrame(off_rows, geometry="geometry", crs="EPSG:4326") if off_rows else empty_polys

    point_rows: List[Dict[str, Any]] = []
    if points_folder is not None:
        for pm in points_folder.findall("kml:Placemark", _KML_NS):
            coords = _kml_placemark_point(pm)
            if coords is None:
                continue
            desc_el = pm.find("kml:description", _KML_NS)
            fields = _parse_kml_description_fields(desc_el.text if desc_el is not None else None)
            point_rows.append(
                {
                    "geometry": Point(coords[0], coords[1]),
                    "ALTm": _num(fields.get("GPS Alt")),
                    "SPkph": _num(fields.get("Grnd Speed")),
                    "LIN": _num(fields.get("Line #")),
                    "__group": "default",
                }
            )
    points_gdf = (
        gpd.GeoDataFrame(point_rows, geometry="geometry", crs="EPSG:4326")
        if point_rows
        else gpd.GeoDataFrame(columns=["geometry", "ALTm", "SPkph", "LIN", "__group"], geometry="geometry", crs="EPSG:4326")
    )

    return _assemble_result(parcelas, points_gdf, sproff_polys, swath_width, coverage_polys=coverage_polys)
