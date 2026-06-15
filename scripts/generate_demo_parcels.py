#!/usr/bin/env python3
"""One-time script: read the two real-customer shapefiles and write compact JSON
files consumed by commercial_demo_seed.py at every demo login.

Usage (from datarisBackend/ directory):
    python scripts/generate_demo_parcels.py

Outputs:
    app/data/demo/lotes_salgado.json
    app/data/demo/palo_gordo.json

Dependencies (install in the backend venv):
    pip install pyshp shapely pyproj
"""

from __future__ import annotations

import json
import math
import sys
import zipfile
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import shapefile
except ImportError:
    sys.exit("Missing: pip install pyshp")

try:
    from shapely.geometry import shape, mapping
    from shapely.ops import transform as shp_transform
except ImportError:
    sys.exit("Missing: pip install shapely")

try:
    from pyproj import Transformer, CRS
except ImportError:
    sys.exit("Missing: pip install pyproj")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent
OUT_DIR = BACKEND_DIR / "app" / "data" / "demo"

LOTES_SALGADO_ZIP = PROJECT_ROOT / "LOTES SALGADO.zip"
PALO_GORDO_ZIP = PROJECT_ROOT / "PALO GORDO SHP.zip"

# Only include this finca from Palo Gordo SHP (203 lots, manageable for demo)
PALO_GORDO_FINCA_FILTER = "PALO GORDO"


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _strip_z(coords: List) -> List:
    """Recursively drop Z coordinate from coordinate lists."""
    if not coords:
        return coords
    if isinstance(coords[0], (int, float)):
        return coords[:2]
    return [_strip_z(c) for c in coords]


def _round_coords(coords: List, precision: int = 7) -> List:
    """Round coordinate values to reduce JSON size."""
    if not coords:
        return coords
    if isinstance(coords[0], (int, float)):
        return [round(v, precision) for v in coords]
    return [_round_coords(c, precision) for c in coords]


def _calc_bounds(geojson_geometry: Dict) -> Dict[str, float]:
    """Return {south, north, west, east} from a GeoJSON geometry."""
    geom = shape(geojson_geometry)
    minx, miny, maxx, maxy = geom.bounds
    return {
        "south": round(miny, 7),
        "north": round(maxy, 7),
        "west": round(minx, 7),
        "east": round(maxx, 7),
    }


def _calc_center(bounds: Dict[str, float]) -> Dict[str, float]:
    return {
        "lat": round((bounds["south"] + bounds["north"]) / 2, 7),
        "lng": round((bounds["west"] + bounds["east"]) / 2, 7),
    }


def _calc_area_ha(geojson_geometry: Dict, source_crs_wkt: Optional[str] = None) -> float:
    """Area in hectares.  For reprojected (WGS84) geometries we re-project to
    an equal-area CRS for the calculation."""
    geom = shape(geojson_geometry)
    try:
        # Use an equal-area projection for accurate area
        proj = Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
        geom_ea = shp_transform(proj.transform, geom)
        return round(abs(geom_ea.area) / 10_000, 4)
    except Exception:
        # Fallback: approximate using the bounds (less accurate)
        minx, miny, maxx, maxy = geom.bounds
        lat_c = (miny + maxy) / 2
        m_per_deg_lat = 111_320.0
        m_per_deg_lng = 111_320.0 * math.cos(math.radians(lat_c))
        width_m = abs(maxx - minx) * m_per_deg_lng
        height_m = abs(maxy - miny) * m_per_deg_lat
        return round(width_m * height_m / 10_000, 4)


def _shape_to_geojson(shape_obj: Any, transformer: Optional[Any] = None) -> Optional[Dict]:
    """Convert a pyshp shape to a GeoJSON-compatible geometry dict.

    Strips Z coordinates.  Optionally reprojects using a pyproj Transformer.
    Returns None for null/empty shapes.
    """
    if shape_obj.shapeType == 0:
        return None

    geom_dict = shape_obj.__geo_interface__
    # Strip Z
    geom_dict = {**geom_dict, "coordinates": _strip_z(geom_dict["coordinates"])}

    if transformer:
        geom = shape(geom_dict)
        geom = shp_transform(transformer.transform, geom)
        geom_dict = mapping(geom)

    geom_dict = {**geom_dict, "coordinates": _round_coords(geom_dict["coordinates"])}
    return geom_dict


def _build_feature_collection(name: str, finca: str, source: str, geom: Dict) -> Dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": name, "lote": name, "finca": finca, "source": source},
                "geometry": geom,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------

def process_lotes_salgado(zip_path: Path) -> List[Dict]:
    """Extract all Lotes Salgado polygons.

    Each shapefile record becomes an individual parcel.  The record index is
    included in the key so that records sharing the same Name field remain
    distinct (the shapefile contains duplicated names for sub-lots).
    CRS is WGS84 geographic (no .prj file present).
    """
    parcels: List[Dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        shp_path = next(Path(tmp).rglob("*.shp"))
        sf = shapefile.Reader(str(shp_path))
        shapes = sf.shapes()
        records = sf.records()

        for idx, (shp, rec) in enumerate(zip(shapes, records)):
            rec_dict = rec.as_dict()
            lot_name = (rec_dict.get("Name") or "").strip() or f"Lote-{idx + 1}"
            finca = (rec_dict.get("FINCA") or "").strip() or "Lotes Salgado"

            geom = _shape_to_geojson(shp)
            if geom is None:
                continue

            bounds = _calc_bounds(geom)
            center = _calc_center(bounds)
            area_ha = _calc_area_ha(geom)
            display_name = f"Lote {lot_name}" if not lot_name.lower().startswith("lote") else lot_name

            # Use a unique internal lot code (LS-001, LS-002, …) so that the
            # compat deduplication layer (which keys on parcel.lote) does not
            # collapse records that share the same shapefile Name field.
            internal_lote = f"LS-{idx + 1:03d}"

            fc = _build_feature_collection(display_name, finca, "lotes_salgado", geom)
            parcels.append(
                {
                    "key": f"lotes-salgado:{idx}",
                    "name": display_name,
                    "finca": finca,
                    "lote": internal_lote,
                    "codigo": None,
                    "area": area_ha,
                    "geometry": fc,
                    "bounds": bounds,
                    "center": center,
                    "source": "lotes_salgado",
                }
            )

    print(f"  LOTES SALGADO: processed {len(parcels)} parcels")
    return parcels


def process_palo_gordo(zip_path: Path, finca_filter: str) -> List[Dict]:
    """Extract lots from the specified finca in the Palo Gordo shapefile.

    Source CRS is WGS84 UTM Zone 15N (EPSG:32615); reprojected to WGS84 geo.

    Multiple shapefile records share the same ID_lote because the shapefile
    stores each agricultural sub-block as a separate polygon.  Records with the
    same ID_lote are merged into one parcel with a multi-feature FeatureCollection
    so the frontend receives a single logical lot (one map card) with all its
    polygon parts.
    """
    transformer = Transformer.from_crs("EPSG:32615", "EPSG:4326", always_xy=True)

    # Group records by ID_lote, preserving insertion order
    groups: Dict[str, Dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        shp_path = next(Path(tmp).rglob("*.shp"))
        sf = shapefile.Reader(str(shp_path))
        shapes = sf.shapes()
        records = sf.records()

        for idx, (shp, rec) in enumerate(zip(shapes, records)):
            rec_dict = rec.as_dict()
            finca = (rec_dict.get("Finca") or "").strip()
            if finca != finca_filter:
                continue

            external_id = (rec_dict.get("ID_lote") or "").strip() or f"pgidx-{idx}"
            geom = _shape_to_geojson(shp, transformer=transformer)
            if geom is None:
                continue

            if external_id not in groups:
                lot_name = (rec_dict.get("Lote") or "").strip() or external_id
                codigo = (rec_dict.get("Codigo") or "").strip() or None
                area_m2 = float(rec_dict.get("Shape_Area") or 0)
                groups[external_id] = {
                    "external_id": external_id,
                    "lot_name": lot_name,
                    "finca": finca,
                    "codigo": codigo,
                    "area_m2": area_m2,
                    "features": [],
                }
            else:
                groups[external_id]["area_m2"] += float(rec_dict.get("Shape_Area") or 0)

            groups[external_id]["features"].append(
                {
                    "type": "Feature",
                    "properties": {
                        "name": groups[external_id]["lot_name"],
                        "lote": groups[external_id]["lot_name"],
                        "finca": finca,
                        "source": "palo_gordo",
                        "id_lote": external_id,
                    },
                    "geometry": geom,
                }
            )

    parcels: List[Dict] = []
    for external_id, group in groups.items():
        features = group["features"]
        area_ha = round(group["area_m2"] / 10_000, 4)
        display_name = f"{group['finca']} {group['lot_name']}".strip()

        fc: Dict = {"type": "FeatureCollection", "features": features}

        # Compute overall bounds from all features
        all_geoms = [shape(f["geometry"]) for f in features]
        union_bounds = (
            min(g.bounds[0] for g in all_geoms),  # west
            min(g.bounds[1] for g in all_geoms),  # south
            max(g.bounds[2] for g in all_geoms),  # east
            max(g.bounds[3] for g in all_geoms),  # north
        )
        bounds = {
            "south": round(union_bounds[1], 7),
            "north": round(union_bounds[3], 7),
            "west": round(union_bounds[0], 7),
            "east": round(union_bounds[2], 7),
        }
        center = _calc_center(bounds)

        parcels.append(
            {
                "key": f"palo-gordo:{external_id}",
                "name": display_name,
                "finca": group["finca"],
                # Use external_id (= ID_lote) as the lote key so compat
                # deduplication does not collapse lots with the same lot number.
                "lote": external_id,
                "codigo": group["codigo"],
                "external_id": external_id,
                "area": area_ha,
                "geometry": fc,
                "bounds": bounds,
                "center": center,
                "source": "palo_gordo",
            }
        )

    print(f"  PALO GORDO (finca='{finca_filter}'): {len(parcels)} lots from {sum(len(g['features']) for g in groups.values())} polygons")
    return parcels


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not LOTES_SALGADO_ZIP.exists():
        print(f"WARNING: {LOTES_SALGADO_ZIP} not found — skipping")
    else:
        print("Processing LOTES SALGADO...")
        ls_parcels = process_lotes_salgado(LOTES_SALGADO_ZIP)
        out = OUT_DIR / "lotes_salgado.json"
        out.write_text(json.dumps(ls_parcels, ensure_ascii=False, separators=(",", ":")))
        print(f"  Saved {len(ls_parcels)} parcels → {out.relative_to(BACKEND_DIR)}")

    if not PALO_GORDO_ZIP.exists():
        print(f"WARNING: {PALO_GORDO_ZIP} not found — skipping")
    else:
        print(f"Processing PALO GORDO (finca='{PALO_GORDO_FINCA_FILTER}')...")
        pg_parcels = process_palo_gordo(PALO_GORDO_ZIP, PALO_GORDO_FINCA_FILTER)
        out = OUT_DIR / "palo_gordo.json"
        out.write_text(json.dumps(pg_parcels, ensure_ascii=False, separators=(",", ":")))
        print(f"  Saved {len(pg_parcels)} parcels → {out.relative_to(BACKEND_DIR)}")

    print("Done.")


if __name__ == "__main__":
    main()
