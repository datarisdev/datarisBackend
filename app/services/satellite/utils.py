# app/processors/satellite/stats.py
import numpy as np

from shapely.geometry import shape, GeometryCollection
from shapely.ops import unary_union

def featurecollection_to_geometry(fc: dict):
    """
    Converts GeoJSON FeatureCollection to a single Shapely geometry
    """
    geometries = []

    for feature in fc.get("features", []):
        geom = feature.get("geometry")
        if geom:
            geometries.append(shape(geom))

    if not geometries:
        raise ValueError("No valid geometries in FeatureCollection")

    if len(geometries) == 1:
        return geometries[0]

    return unary_union(geometries)

def compute_stats(arr):
    return {
        "min": float(np.nanmin(arr)),
        "max": float(np.nanmax(arr)),
        "mean": float(np.nanmean(arr)),
    }

def build_stats_json(db_rows):
    output = {"images": []}

    grouped = {}
    for row in db_rows:
        key = row.image_date.isoformat()
        if key not in grouped:
            grouped[key] = {
                "date": key,
                "cloud_cover": row.cloud_coverage,
            }
        grouped[key][row.index_type.lower()] = row.statistics

    output["images"] = list(grouped.values())
    return output
