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

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union


MAX_POINT_GAP_METERS = 120.0
DEFAULT_SWATH_WIDTH_METERS = 16.0

FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "parcel_name": (
        "__dataris_parcel_name",
        "AREA_NAME",
        "AREA",
        "NAME",
        "NOMBRE",
        "NOMBRE_LOTE",
        "LOTE",
        "LOT",
        "LOT_NAME",
        "PARCELA",
        "PARCEL",
        "CAMPO",
        "BLOCK",
        "BLOQUE",
        "SECTOR",
        "FINCA",
        "CODIGO",
        "CODIGO_LOTE",
        "ID",
    ),
    "line": (
        "LIN",
        "LINE",
        "LINEA",
        "LÍNEA",
        "LINE_ID",
        "ID_LINEA",
        "IDLINEA",
        "TRACK",
        "TRACK_ID",
        "PASADA",
        "PASS",
        "TRAMO",
        "RUTA",
        "NUMERO",
        "NUMBER",
        "N",
        "ID",
    ),
    "time": (
        "TIMEGPS",
        "GPS_TIME",
        "TIME",
        "HORA",
        "TIMESTAMP",
        "DATE_TIME",
        "DATETIME",
        "FECHA_HORA",
        "FECHAHORA",
    ),
    "altitude": (
        "ALTm",
        "ALT",
        "ALT_M",
        "ALTITUDE",
        "ALTITUD",
        "HEIGHT",
        "ALTURA",
        "ALTURA_M",
        "ALTURAM",
    ),
    "speed": (
        "SPkph",
        "SPEED",
        "SPEED_KPH",
        "VELOCIDAD",
        "VEL",
        "KMH",
        "KPH",
        "VEL_KPH",
    ),
    "width": (
        "SW_WIDTHm",
        "SWATH_WIDTH",
        "SW_WIDTH",
        "WIDTH",
        "ANCHO",
        "ANCHO_M",
        "ANCHOM",
        "ANCHO_FAJA",
        "ANCHOFAJA",
        "FAJA",
    ),
    "volume": (
        "SPR_VOL",
        "SPRAY_VOL",
        "VOLUME",
        "VOLUMEN",
        "VOL",
        "LITROS",
        "LITERS",
        "LTS",
    ),
}

CANONICAL_FIELDS: Dict[str, str] = {
    "line": "LIN",
    "time": "TIMEGPS",
    "altitude": "ALTm",
    "speed": "SPkph",
    "width": "SW_WIDTHm",
    "volume": "SPR_VOL",
}


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



def _norm_key(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-zA-Z0-9]", "", text).lower()


def _column_lookup(columns: Iterable[str]) -> Dict[str, str]:
    return {_norm_key(col): col for col in columns}


def _find_column(columns: Iterable[str], aliases: Iterable[str]) -> Optional[str]:
    cols = list(columns)
    for alias in aliases:
        for col in cols:
            if str(col).lower() == str(alias).lower():
                return col
    lookup = _column_lookup(cols)
    for alias in aliases:
        found = lookup.get(_norm_key(alias))
        if found:
            return found
    return None


def _get_any(row: pd.Series, aliases: Iterable[str], default: Any = None) -> Any:
    col = _find_column(row.index, aliases)
    if not col:
        return default
    value = row.get(col, default)
    try:
        if value is None or pd.isna(value):
            return default
    except Exception:
        pass
    return value


def _row_json_props(row: pd.Series) -> Dict[str, Any]:
    return {k: _jsonable_value(v) for k, v in row.drop(labels=["geometry"], errors="ignore").to_dict().items()}


def _add_canonical_fields(gdf: gpd.GeoDataFrame, source_name: str) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf

    gdf = gdf.copy()
    original_columns = [str(c) for c in gdf.columns if c != "geometry"]
    field_map: Dict[str, str] = {}

    for semantic, canonical in CANONICAL_FIELDS.items():
        source_col = _find_column(original_columns, FIELD_ALIASES[semantic])
        if source_col:
            field_map[canonical] = source_col
            if canonical not in gdf.columns:
                gdf[canonical] = gdf[source_col]

    if source_name.lower() == "polygon":
        name_col = _find_column(original_columns, FIELD_ALIASES["parcel_name"])
        if name_col:
            field_map["__dataris_parcel_name"] = name_col
            gdf["__dataris_parcel_name"] = gdf[name_col].astype(str)

    if field_map:
        gdf["__dataris_field_map"] = json.dumps(field_map, ensure_ascii=False)
    gdf["__dataris_original_fields"] = ", ".join(original_columns)
    return gdf

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
            dst.write(src.read())


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
    gdf = _add_canonical_fields(gdf, source_name)
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
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return value
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def _feature_collection(gdf: gpd.GeoDataFrame, max_point_features: Optional[int] = None) -> Dict[str, Any]:
    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}
    gdf_wgs = _to_wgs(gdf).copy()
    if max_point_features and len(gdf_wgs) > max_point_features:
        stride = max(1, math.ceil(len(gdf_wgs) / max_point_features))
        gdf_wgs = gdf_wgs.iloc[::stride].copy()
    features: List[Dict[str, Any]] = []
    for _, row in gdf_wgs.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        props = {k: _jsonable_value(v) for k, v in row.drop(labels=["geometry"]).to_dict().items()}
        features.append({"type": "Feature", "properties": props, "geometry": mapping(geom)})
    return {"type": "FeatureCollection", "features": features}


def _geom_to_wgs(geom: Optional[BaseGeometry], from_crs: Any, props: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if geom is None or geom.is_empty:
        return None
    geom = _polygonal(geom)
    if geom is None or geom.is_empty:
        return None
    transformer = Transformer.from_crs(from_crs, "EPSG:4326", always_xy=True)
    converted = transform(transformer.transform, geom)
    return {"type": "Feature", "properties": props or {}, "geometry": mapping(converted)}


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


def process_helicopter_zip(zip_path: Path, swath_width: float = DEFAULT_SWATH_WIDTH_METERS) -> Dict[str, Any]:
    if swath_width <= 0:
        swath_width = DEFAULT_SWATH_WIDTH_METERS

    with tempfile.TemporaryDirectory(prefix="dataris-heli-") as tmp_name:
        tmp = Path(tmp_name)
        with zipfile.ZipFile(zip_path) as zf:
            groups = _shp_groups(zf.namelist())
            if not groups:
                raise ValueError("El ZIP debe contener grupos completos Polygon.shp, SprOn.shp y SprOff.shp")
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

        metric_crs = _metric_crs(parcelas)
        parcelas_metric = parcelas.to_crs(metric_crs)
        spron_metric = spron.to_crs(metric_crs)
        sproff_metric = sproff.to_crs(metric_crs) if not sproff.empty else sproff

        parcel_union = _polygonal(unary_union(list(parcelas_metric.geometry)))
        line_infos_raw, spron_tracks_metric = _build_line_polygons(spron_metric, swath_width)
        line_infos = _clip_line_polys(line_infos_raw, parcel_union)
        coverage = _coverage_union(line_infos)
        overlap = _overlap_union(line_infos)

        _, sproff_tracks_metric = _build_line_polygons(sproff_metric, swath_width) if not sproff_metric.empty else ([], gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=metric_crs))

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

            try:
                inside = spron_metric[spron_metric.geometry.within(geom)]
            except Exception:
                inside = spron_metric.iloc[0:0]
            altitudes = [_num(v) for v in inside.get("ALTm", pd.Series(dtype=float)).tolist() if _num(v) != 0]
            speeds = [_num(v) for v in inside.get("SPkph", pd.Series(dtype=float)).tolist() if _num(v) != 0]
            line_keys = {_line_key(r) for _, r in inside.iterrows()}
            line_keys.discard(None)

            source_row = parcelas.loc[idx] if idx in parcelas.index else row
            name = (
                _get_any(source_row, FIELD_ALIASES["parcel_name"])
                or source_row.get("__group")
                or f"Área {len(parcel_results) + 1}"
            )
            source_props = _row_json_props(source_row)
            source_props["__dataris_parcel_name"] = str(name)
            vol = _num(_get_any(source_row, FIELD_ALIASES["volume"]), 0.0)

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
                    "sourceProperties": source_props,
                    "coveredGeom": _geom_to_wgs(covered_geom, metric_crs, {**source_props, "tipo_capa": "Zona aplicada", "hectareas_aplicadas": round(covered_ha, 4), "cobertura_pct": round((covered_ha / area_ha) * 100 if area_ha else 0.0, 2)}),
                    "uncoveredGeom": _geom_to_wgs(uncovered_geom, metric_crs, {**source_props, "tipo_capa": "Sin cubrir", "hectareas_sin_cubrir": round(max(area_ha - covered_ha, 0.0), 4)}),
                    "overlapGeom": _geom_to_wgs(overlap_geom, metric_crs, {**source_props, "tipo_capa": "Sobre-aplicado", "hectareas_sobre_aplicadas": round(overlap_ha, 4)}),
                    "geometry": _geom_to_wgs(geom, metric_crs, source_props),
                }
            )

        total_ha = sum(p["totalHa"] for p in parcel_results)
        total_covered_ha = sum(p["coveredHa"] for p in parcel_results)
        total_uncovered_ha = max(total_ha - total_covered_ha, 0.0)
        overlap_ha = overlap.area / 10000.0 if overlap else 0.0

        speeds_all = [_num(v) for v in spron.get("SPkph", pd.Series(dtype=float)).tolist() if _num(v) != 0]
        alts_all = [_num(v) for v in spron.get("ALTm", pd.Series(dtype=float)).tolist() if _num(v) != 0]
        line_keys_global = {_line_key(r) for _, r in spron.iterrows()}
        line_keys_global.discard(None)
        total_volume = sum(_num(_get_any(r, FIELD_ALIASES["volume"]), 0.0) for _, r in parcelas.iterrows())

        return {
            "parcelas": _feature_collection(parcelas),
            # Guardamos puntos limitados para no inflar demasiado compat_db.json; las líneas correctas van en sprOnTracks/sprOffTracks.
            "sprOn": _feature_collection(spron, max_point_features=4000),
            "sprOff": _feature_collection(sproff, max_point_features=2000),
            "sprOnTracks": _feature_collection(spron_tracks_metric),
            "sprOffTracks": _feature_collection(sproff_tracks_metric),
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
