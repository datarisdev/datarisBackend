from shapely.geometry import shape
from shapely.ops import transform
from pyproj import Transformer
import logging

# Transformer: WGS84 → Web Mercator (meters)
transformer = Transformer.from_crs(
    "EPSG:4326",
    "EPSG:3857",
    always_xy=True,
)

def calculate_area_hectares(geojson: dict) -> float:
    """
    Calculates total area (ha) from GeoJSON FeatureCollection or Geometry
    """
    total_area_m2 = 0.0

    def project(x, y):
        return transformer.transform(x, y)

    # FeatureCollection
    if geojson.get("type") == "FeatureCollection":
        for feature in geojson.get("features", []):
            geom = shape(feature["geometry"])
            projected = transform(project, geom)
            total_area_m2 += projected.area

    # Single geometry
    else:
        geom = shape(geojson)
        projected = transform(project, geom)
        total_area_m2 = projected.area

    hectares = total_area_m2 / 10_000  # m² → ha
    return round(hectares, 4)

def extract_area_from_properties_or_geometry(geojson: dict) -> float | None:
    """
    Hybrid area extractor:
    - Uses Shape_Area (m²) when present per feature
    - Calculates geometry area when missing or zero
    - Returns hectares
    """
    if geojson.get("type") != "FeatureCollection":
        return None

    total_area_m2 = 0.0
    has_any = False

    def project(x, y):
        return transformer.transform(x, y)

    for feature in geojson.get("features", []):
        props = feature.get("properties", {}) or {}
        shape_area = props.get("Shape_Area") or props.get("shape_area")

        # ✅ 1️⃣ Use Shape_Area if valid
        if shape_area and float(shape_area) > 0:
            total_area_m2 += float(shape_area)
            has_any = True
            continue

        # ✅ 2️⃣ Otherwise calculate from geometry
        try:
            geom = shape(feature["geometry"])
            projected = transform(project, geom)
            total_area_m2 += projected.area
            has_any = True
        except Exception as e:
            logging.warning(f"Failed to calculate area for feature: {e}")

    if not has_any:
        return None

    return round(total_area_m2 / 10_000, 4)  # m² → ha


def extract_main_properties(geojson: dict) -> dict:
    if geojson.get("type") != "FeatureCollection":
        return {}

    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        if props.get("ID_lote"):
            return {
                "external_id": props.get("ID_lote"),
                "finca": props.get("Finca"),
                "lote": props.get("Lote"),
                "codigo": props.get("Codigo"),
            }
    return {}
