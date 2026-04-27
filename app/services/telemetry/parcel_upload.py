from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import geopandas as gpd
from shapely.geometry import Polygon


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


def _feature_collection(gdf: gpd.GeoDataFrame) -> Dict[str, Any]:
    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326", allow_override=True)
    gdf = gdf.to_crs("EPSG:4326")
    return json.loads(gdf.to_json())


def _area_ha(gdf: gpd.GeoDataFrame) -> float:
    if gdf.empty:
        return 0.0
    try:
        metric = gdf.to_crs(gdf.estimate_utm_crs() or "EPSG:3857")
    except Exception:
        metric = gdf.to_crs("EPSG:3857") if gdf.crs else gdf.set_crs("EPSG:4326", allow_override=True).to_crs("EPSG:3857")
    return float(metric.geometry.area.sum() / 10000.0)


def _parse_kml_text(text: str) -> gpd.GeoDataFrame:
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
            features.append({"name": name, "geometry": Polygon(coords)})
    if not features:
        raise ValueError("El KML/KMZ no contiene polígonos válidos")
    return gpd.GeoDataFrame(features, geometry="geometry", crs="EPSG:4326")


def _read_shapefile_zip(zip_path: Path) -> gpd.GeoDataFrame:
    with tempfile.TemporaryDirectory(prefix="dataris-parcel-") as tmp_name:
        tmp = Path(tmp_name)
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp)
        shp_files = [p for p in tmp.rglob("*.shp") if not p.name.lower().endswith(("spron.shp", "sproff.shp"))]
        if not shp_files:
            raise ValueError("El ZIP no contiene archivo .shp válido")
        # Prioridad a capas Polygon/lote/parcela si existen.
        shp_files.sort(key=lambda p: (0 if any(k in p.name.lower() for k in ["polygon", "parcela", "lote", "parcel"]) else 1, str(p)))
        errors: List[str] = []
        for shp in shp_files:
            try:
                gdf = gpd.read_file(shp)
                if gdf.crs is None:
                    gdf = gdf.set_crs("EPSG:4326", allow_override=True)
                gdf = gdf[gdf.geometry.notna() & gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
                if not gdf.empty:
                    return gdf
            except Exception as exc:
                errors.append(f"{shp.name}: {exc}")
        raise ValueError("No se encontraron polígonos válidos en el shapefile. " + "; ".join(errors[:2]))


def parse_parcel_file(file_path: Path, original_name: Optional[str] = None) -> Dict[str, Any]:
    name = (original_name or file_path.name).lower()
    is_zip = name.endswith(".zip") or name.endswith(".kmz")

    if name.endswith(".kml"):
        gdf = _parse_kml_text(file_path.read_text(encoding="utf-8", errors="ignore"))
    elif is_zip:
        with zipfile.ZipFile(file_path) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if kml_names:
                text = zf.read(kml_names[0]).decode("utf-8", errors="ignore")
                gdf = _parse_kml_text(text)
            else:
                gdf = _read_shapefile_zip(file_path)
    else:
        gdf = gpd.read_file(file_path)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326", allow_override=True)
        gdf = gdf[gdf.geometry.notna() & gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()

    if gdf.empty:
        raise ValueError("El archivo no contiene geometrías de lote/parcela válidas")
    return {"geometry": _feature_collection(gdf), "area": _area_ha(gdf)}
