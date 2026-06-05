from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.mask import mask
from rasterio.transform import array_bounds, from_bounds
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
try:
    from shapely.validation import make_valid as _shapely_make_valid
except Exception:  # Shapely < 2 fallback
    _shapely_make_valid = None
from shapely.ops import unary_union

from app.models.satellite_image import ProcessingStatus, SatelliteImage
from app.services.satellite.utils import featurecollection_to_geometry
from app.services.sentinel2.catalog import SentinelScene, available_dates, best_scene_for_date, normalize_asset_href, scenes_for_date, sign_item_if_needed
from app.services.sentinel2.indices import (
    BAND_ASSET_ALIASES,
    INDEX_DEFINITIONS,
    normalize_band_values,
    normalize_index_key,
)
from app.services.sentinel2.render import compute_statistics, render_index_png
from PIL import Image
from io import BytesIO
from app.utils.storage_satellite import (
    download_satellite_cache_metadata,
    download_satellite_cache_png_bytes,
    download_satellite_date_manifest,
    download_satellite_latest_manifest,
    generate_signed_satellite_url,
    upload_satellite_cache_metadata,
    upload_satellite_date_manifest,
    upload_satellite_latest_manifest,
    upload_satellite_png_bytes,
)

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.getenv("SENTINEL_LOCAL_CACHE_DIR", str(Path(tempfile.gettempdir()) / "dataris_sentinel2_cache")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SENTINEL_CACHE_URL_VERSION = os.getenv("SENTINEL_CACHE_URL_VERSION", "sentinel-gcs-20260605-persistent-manifest-cache-2")


DEFAULT_MAX_CLOUD = float(os.getenv("SENTINEL_DEFAULT_MAX_CLOUD", "80"))
DATE_LOOKBACK_DAYS = int(os.getenv("SENTINEL_DATE_LOOKBACK_DAYS", "180"))
MAP_LOOKBACK_DAYS = int(os.getenv("SENTINEL_MAP_LOOKBACK_DAYS", "90"))
DB_CACHE_ENABLED = os.getenv("SENTINEL_DB_CACHE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
MASK_ALL_TOUCHED = os.getenv("SENTINEL_MASK_ALL_TOUCHED", "true").strip().lower() not in {"0", "false", "no", "off"}
MAX_MOSAIC_SCENES = max(1, int(os.getenv("SENTINEL_MAX_MOSAIC_SCENES", "24")))
MAX_MOSAIC_GRID_SIZE = max(256, int(os.getenv("SENTINEL_MAX_MOSAIC_GRID_SIZE", "4096")))
MOSAIC_CATALOG_LIMIT = max(MAX_MOSAIC_SCENES, int(os.getenv("SENTINEL_MOSAIC_CATALOG_LIMIT", "240")))
MOSAIC_PROCESSING_VERSION = "sentinel2-quality-mosaic-v2"
SCL_UNAVAILABLE_CLASSES = frozenset({0, 1, 3, 8, 9, 10, 11})

RASTERIO_ENV = {
    "AWS_NO_SIGN_REQUEST": os.getenv("AWS_NO_SIGN_REQUEST", "YES"),
    "AWS_REGION": os.getenv("SENTINEL_AWS_REGION", os.getenv("AWS_REGION", "us-west-2")),
    "GDAL_DISABLE_READDIR_ON_OPEN": os.getenv("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR"),
    "CPL_VSIL_CURL_USE_HEAD": os.getenv("CPL_VSIL_CURL_USE_HEAD", "NO"),
    "GDAL_HTTP_MAX_RETRY": os.getenv("GDAL_HTTP_MAX_RETRY", "2"),
    "GDAL_HTTP_RETRY_DELAY": os.getenv("GDAL_HTTP_RETRY_DELAY", "1"),
    "VSI_CACHE": os.getenv("VSI_CACHE", "TRUE"),
    "VSI_CACHE_SIZE": os.getenv("VSI_CACHE_SIZE", "50000000"),
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def list_index_layers() -> list[dict[str, Any]]:
    return [
        {
            "id": definition.key,
            "key": definition.key,
            "name": definition.key,
            "displayed_name": definition.label,
            "label": definition.label,
            "wms_layer": definition.key,
            "layer_resolution": definition.resolution,
            "layer_resolution_key": definition.resolution,
            "resolution": definition.resolution,
            "resolution_key": definition.resolution,
            "resolution_id": definition.resolution,
            "is_sentinel": True,
            "is_active": True,
            "is_experimental": False,
            "is_init_visible": definition.key == "NDVI",
            "menu_priority": definition.priority,
            "raw": {
                "source": "dataris-sentinel2-free",
                "provider": os.getenv("SENTINEL_STAC_PROVIDER", "earthsearch"),
                "description": definition.description,
                "rgb": definition.rgb,
                "min": definition.min_value,
                "max": definition.max_value,
            },
        }
        for definition in sorted(INDEX_DEFINITIONS.values(), key=lambda item: item.priority)
    ]


def _extract_polygonal_geometry(geom: BaseGeometry) -> BaseGeometry:
    """Keep only polygonal surfaces while preserving rings/holes.

    make_valid can return GeometryCollections with LineStrings/Points for invalid
    self-intersections. Raster masks must use only Polygon/MultiPolygon surfaces;
    otherwise GDAL/rasterio can include unexpected pixels or fail silently.
    """
    if geom.is_empty:
        return geom
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        polygons: list[BaseGeometry] = []
        for child in geom.geoms:
            cleaned = _extract_polygonal_geometry(child)
            if cleaned and not cleaned.is_empty:
                polygons.append(cleaned)
        if not polygons:
            return geom
        return unary_union(polygons)
    return geom


def _clean_geometry(geom: BaseGeometry) -> BaseGeometry:
    """Repair invalid parcel rings and keep only real polygon surfaces."""
    if geom.is_empty:
        return geom
    try:
        if not geom.is_valid:
            if _shapely_make_valid is not None:
                geom = _shapely_make_valid(geom)
            else:
                geom = geom.buffer(0)
    except Exception:
        # Keep the original geometry; rasterio will raise a clear error if it is unusable.
        pass
    try:
        geom = _extract_polygonal_geometry(geom)
    except Exception:
        pass
    return geom


def _parcel_geometry_to_shape(parcel_geometry: Any) -> BaseGeometry:
    """Accept all geometry shapes used by Dataris parcels.

    Supports FeatureCollection, Feature, Polygon, MultiPolygon and GeometryCollection,
    including holes/rings and multiple divisions. FeatureCollections are dissolved
    into one mask so every valid subpolygon is retained while internal boundaries
    do not leak raster pixels outside the real lot geometry.
    """
    if isinstance(parcel_geometry, str):
        parcel_geometry = json.loads(parcel_geometry)
    if isinstance(parcel_geometry, list):
        geometries = [_parcel_geometry_to_shape(item) for item in parcel_geometry if item]
        if not geometries:
            raise ValueError("El lote no contiene geometrías válidas")
        return _clean_geometry(unary_union(geometries))
    if not isinstance(parcel_geometry, dict):
        raise ValueError("La geometría del lote no es un GeoJSON válido")

    geo_type = str(parcel_geometry.get("type") or "").lower()
    if geo_type == "featurecollection":
        features = parcel_geometry.get("features") or []
        geometries = [
            _clean_geometry(shape(feature.get("geometry")))
            for feature in features
            if isinstance(feature, dict) and feature.get("geometry")
        ]
        geometries = [geom for geom in geometries if not geom.is_empty]
        if not geometries:
            raise ValueError("El lote no contiene geometrías válidas")
        return _clean_geometry(geometries[0] if len(geometries) == 1 else unary_union(geometries))
    if geo_type == "feature":
        geometry = parcel_geometry.get("geometry")
        if not geometry:
            raise ValueError("El Feature del lote no contiene geometry")
        return _clean_geometry(shape(geometry))
    if geo_type in {"polygon", "multipolygon", "geometrycollection"}:
        return _clean_geometry(shape(parcel_geometry))

    # Backwards compatibility with the previous utility.
    return _clean_geometry(featurecollection_to_geometry(parcel_geometry))


def geometry_to_gdf(parcel_geometry: dict) -> gpd.GeoDataFrame:
    geom = _parcel_geometry_to_shape(parcel_geometry)
    if geom.is_empty:
        raise ValueError("La geometría del lote está vacía")
    return gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")


def bounds_from_gdf(gdf: gpd.GeoDataFrame) -> dict[str, float]:
    minx, miny, maxx, maxy = [float(v) for v in gdf.total_bounds]
    return {"south": miny, "north": maxy, "west": minx, "east": maxx}


def bbox_from_geometry(parcel_geometry: dict) -> list[float]:
    gdf = geometry_to_gdf(parcel_geometry)
    minx, miny, maxx, maxy = [float(v) for v in gdf.total_bounds]
    return [minx, miny, maxx, maxy]


def catalog_dates_for_geometry(parcel_geometry: dict, *, max_cloud: float = DEFAULT_MAX_CLOUD) -> list[dict[str, Any]]:
    end = date.today()
    start = end - timedelta(days=DATE_LOOKBACK_DAYS)
    bbox = bbox_from_geometry(parcel_geometry)
    return available_dates(bbox=bbox, start_date=start, end_date=end, max_cloud=max_cloud, limit=140)


def _asset_href(item: Any, canonical_band: str) -> str:
    aliases = BAND_ASSET_ALIASES.get(canonical_band, (canonical_band,))
    for alias in aliases:
        asset = item.assets.get(alias)
        if asset is not None and getattr(asset, "href", None):
            return normalize_asset_href(asset.href)
    available = ", ".join(sorted(item.assets.keys()))
    raise ValueError(f"No se encontró la banda {canonical_band}. Assets disponibles: {available}")


def _clip_asset(href: str, shapes: list[dict[str, Any]]) -> tuple[np.ndarray, dict[str, Any]]:
    normalized_href = normalize_asset_href(href)
    with rasterio.Env(**RASTERIO_ENV):
        with rasterio.open(normalized_href) as src:
            out_img, out_transform = mask(src, shapes=shapes, crop=True, filled=False, all_touched=MASK_ALL_TOUCHED)
            raw = out_img[0].astype("float32")
            arr = raw.filled(np.nan) if hasattr(raw, "filled") else raw
            meta = src.meta.copy()
            meta.update(
                {
                    "height": arr.shape[0],
                    "width": arr.shape[1],
                    "transform": out_transform,
                    "count": 1,
                    "dtype": "float32",
                    "nodata": np.nan,
                }
            )
            return arr.astype("float32"), meta


def _bounds_from_raster_meta(meta: dict[str, Any]) -> dict[str, float]:
    south, west, north, east = 0.0, 0.0, 0.0, 0.0
    try:
        left, bottom, right, top = array_bounds(int(meta["height"]), int(meta["width"]), meta["transform"])
        crs = meta.get("crs")
        if crs and str(crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
            left, bottom, right, top = transform_bounds(crs, "EPSG:4326", left, bottom, right, top, densify_pts=21)
        south, west, north, east = float(bottom), float(left), float(top), float(right)
    except Exception:
        return {"south": south, "north": north, "west": west, "east": east}
    return {"south": south, "north": north, "west": west, "east": east}



def _reproject_array_to_web_mercator(arr: np.ndarray, meta: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any], dict[str, float]]:
    """Reproject the computed index to EPSG:3857 before rendering.

    Leaflet/Mapbox displays image overlays in Web Mercator. If we render a
    Sentinel-2 crop in the native UTM grid and only attach WGS84 corner bounds,
    the image can look slightly shifted or scaled compared with parcel polygons.
    This normalizes the raster to the same projection used by the map, then the
    final geometry mask is applied on that output grid.
    """
    src_crs = meta.get("crs")
    src_transform = meta.get("transform")
    if not src_crs or src_transform is None:
        return arr, meta, _bounds_from_raster_meta(meta)

    try:
        src_crs_text = str(src_crs).upper()
        if src_crs_text in {"EPSG:3857", "EPSG:900913"}:
            return arr, meta, _bounds_from_raster_meta(meta)

        height = int(meta["height"])
        width = int(meta["width"])
        left, bottom, right, top = array_bounds(height, width, src_transform)
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs,
            "EPSG:3857",
            width,
            height,
            left,
            bottom,
            right,
            top,
        )
        if dst_width <= 0 or dst_height <= 0:
            return arr, meta, _bounds_from_raster_meta(meta)

        if arr.ndim == 3:
            dst_arr = np.full((arr.shape[0], dst_height, dst_width), np.nan, dtype="float32")
            for band_idx in range(arr.shape[0]):
                reproject(
                    arr[band_idx].astype("float32", copy=False),
                    dst_arr[band_idx],
                    src_transform=src_transform,
                    src_crs=src_crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:3857",
                    resampling=Resampling.bilinear,
                    src_nodata=np.nan,
                    dst_nodata=np.nan,
                )
        else:
            dst_arr = np.full((dst_height, dst_width), np.nan, dtype="float32")
            reproject(
                arr.astype("float32", copy=False),
                dst_arr,
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs="EPSG:3857",
                resampling=Resampling.bilinear,
                src_nodata=np.nan,
                dst_nodata=np.nan,
            )

        dst_meta = meta.copy()
        dst_meta.update({
            "crs": "EPSG:3857",
            "transform": dst_transform,
            "width": int(dst_width),
            "height": int(dst_height),
            "dtype": "float32",
            "nodata": np.nan,
        })
        return dst_arr.astype("float32", copy=False), dst_meta, _bounds_from_raster_meta(dst_meta)
    except Exception:
        logger.exception("No se pudo reproyectar Sentinel-2 a EPSG:3857; se usará el grid original")
        return arr, meta, _bounds_from_raster_meta(meta)

def _fit_png_to_display_size(
    png_bytes: bytes,
    target_width: int,
    target_height: int,
    *,
    preserve_pixels: bool = True,
) -> bytes:
    """Scale the PNG to a stable display size without blurring index pixels.

    Sentinel-2 keeps its native 10m/20m information content. Upscaling does not
    invent detail, but nearest-neighbour scaling prevents the browser from
    smearing adjacent index cells when the user zooms into a lot. RGB previews
    use Lanczos because photographic layers benefit from smooth interpolation.
    """
    if target_width <= 0 or target_height <= 0:
        return png_bytes
    with Image.open(BytesIO(png_bytes)) as image:
        width, height = image.size
        if width <= 0 or height <= 0:
            return png_bytes
        scale = min(target_width / width, target_height / height)
        if not math.isfinite(scale) or scale <= 0:
            return png_bytes
        output_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        if output_size == image.size:
            return png_bytes
        resampling = Image.Resampling.NEAREST if preserve_pixels else Image.Resampling.LANCZOS
        resized = image.resize(output_size, resampling)
        buffer = BytesIO()
        resized.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def _geometry_hash(parcel_geometry: Any) -> str:
    try:
        geom = mapping(_parcel_geometry_to_shape(parcel_geometry))
        payload = json.dumps(geom, sort_keys=True, separators=(",", ":"))
    except Exception:
        payload = json.dumps(parcel_geometry, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _geometry_shapes_for_meta(parcel_geometry: Any, meta: dict[str, Any]) -> list[dict[str, Any]]:
    gdf = geometry_to_gdf(parcel_geometry)
    crs = meta.get("crs")
    if crs:
        gdf = gdf.to_crs(crs)
    return [mapping(geom) for geom in gdf.geometry if geom and not geom.is_empty]


def _apply_precise_geometry_mask(arr: np.ndarray, meta: dict[str, Any], parcel_geometry: Any) -> np.ndarray:
    """Force transparent pixels outside the exact lot geometry after all resampling.

    rasterio.mask is applied when reading every band, but mixed-resolution indices
    such as NDRE/NDMI resample 20m/10m bands and can reintroduce tiny edge pixels.
    This final mask uses the selected lot geometry on the output grid so the PNG
    alpha always follows the lote, including holes, rings and MultiPolygons.
    """
    shapes = _geometry_shapes_for_meta(parcel_geometry, meta)
    if not shapes:
        return arr
    inside = geometry_mask(
        shapes,
        out_shape=(int(meta["height"]), int(meta["width"])),
        transform=meta["transform"],
        invert=True,
        # Final alpha mask must be strict. all_touched=True expands the lot by
        # one raster pixel on every edge, which is visible at high zoom and makes
        # the satellite layer look larger than the parcel outline.
        all_touched=False,
    )
    if arr.ndim == 3:
        masked = arr.astype("float32", copy=True)
        masked[:, ~inside] = np.nan
        return masked
    masked = arr.astype("float32", copy=True)
    masked[~inside] = np.nan
    return masked


def _resolution_meters(resolution: str | int | float | None) -> float:
    raw = str(resolution or "10").strip().lower().replace("meters", "").replace("meter", "").replace("m", "")
    try:
        value = float(raw)
    except Exception:
        value = 10.0
    return max(1.0, value)


def _build_mosaic_grid(parcel_geometry: dict, resolution: str | int | float | None) -> dict[str, Any]:
    """Build one Web Mercator grid covering the complete lot.

    Every Sentinel tile is reprojected into this same grid before indices are
    calculated. This is what prevents a parcel crossing an MGRS tile boundary
    from appearing only partially colored on some dates.
    """
    gdf = geometry_to_gdf(parcel_geometry).to_crs("EPSG:3857")
    left, bottom, right, top = [float(value) for value in gdf.total_bounds]
    if not (left < right and bottom < top):
        raise ValueError("No se pudo construir una grilla válida para el lote")

    pixel_size = _resolution_meters(resolution)
    width = max(1, int(math.ceil((right - left) / pixel_size)))
    height = max(1, int(math.ceil((top - bottom) / pixel_size)))
    if width > MAX_MOSAIC_GRID_SIZE or height > MAX_MOSAIC_GRID_SIZE:
        scale = max(width / MAX_MOSAIC_GRID_SIZE, height / MAX_MOSAIC_GRID_SIZE)
        pixel_size *= scale
        width = max(1, int(math.ceil((right - left) / pixel_size)))
        height = max(1, int(math.ceil((top - bottom) / pixel_size)))

    transform = from_bounds(left, bottom, right, top, width, height)
    return {
        "crs": "EPSG:3857",
        "transform": transform,
        "width": width,
        "height": height,
        "count": 1,
        "dtype": "float32",
        "nodata": np.nan,
        "pixel_size_meters": pixel_size,
    }


def _reproject_scene_band(
    scene: SentinelScene,
    band: str,
    meta: dict[str, Any],
) -> np.ndarray:
    """Reproject one STAC asset into the common parcel grid.

    Reflectance bands use bilinear interpolation. SCL is categorical and must
    use nearest-neighbour interpolation so cloud classes are not blended.
    """
    signed_item = sign_item_if_needed(scene.item)
    href = _asset_href(signed_item, band)
    tile = np.full((int(meta["height"]), int(meta["width"])), np.nan, dtype="float32")
    with rasterio.Env(**RASTERIO_ENV):
        with rasterio.open(normalize_asset_href(href)) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=tile,
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=meta["transform"],
                dst_crs=meta["crs"],
                dst_nodata=np.nan,
                resampling=Resampling.nearest if band == "scl" else Resampling.bilinear,
                init_dest_nodata=True,
                num_threads=2,
            )
    return tile


def _scl_usable_mask(scl: np.ndarray) -> np.ndarray:
    """Return usable Sentinel-2 L2A pixels from the Scene Classification Layer.

    The excluded classes are no-data, saturated/defective pixels, cloud shadow,
    medium/high probability cloud, cirrus and snow/ice. Water and bare soil stay
    valid because they are real observations and may matter for NDWI/NDMI.
    """
    finite = np.isfinite(scl)
    classes = np.full(scl.shape, -1, dtype="int16")
    if np.any(finite):
        classes[finite] = np.rint(scl[finite]).astype("int16")
    return finite & ~np.isin(classes, tuple(SCL_UNAVAILABLE_CLASSES))


def _merge_scene_into_mosaic(
    scene: SentinelScene,
    bands: tuple[str, ...],
    destinations: dict[str, np.ndarray],
    meta: dict[str, Any],
) -> tuple[bool, bool, list[str]]:
    """Fill missing mosaic cells from one scene without mixing invalid pixels.

    A complete set of required bands is loaded first. When SCL exists, cloudy or
    otherwise unusable pixels are discarded before merge so a later same-day
    tile can fill the overlap with a cleaner observation.
    """
    warnings: list[str] = []
    scene_bands: dict[str, np.ndarray] = {}
    for band in bands:
        try:
            scene_bands[band] = _reproject_scene_band(scene, band, meta)
        except Exception as exc:
            warnings.append(f"{scene.id} · {band}: {exc}")
            logger.warning("No se pudo incorporar banda %s de escena %s al mosaico: %s", band, scene.id, exc)
            return False, False, warnings

    valid = np.ones((int(meta["height"]), int(meta["width"])), dtype=bool)
    for tile in scene_bands.values():
        valid &= np.isfinite(tile)

    quality_mask_applied = False
    try:
        scl = _reproject_scene_band(scene, "scl", meta)
        valid &= _scl_usable_mask(scl)
        quality_mask_applied = True
    except Exception as exc:
        # Some compatible STAC providers do not expose SCL. The image remains
        # usable, but callers receive a warning so quality expectations are clear.
        warnings.append(f"{scene.id} · SCL no disponible: {exc}")
        logger.info("SCL no disponible para escena %s: %s", scene.id, exc)

    existing = np.ones(valid.shape, dtype=bool)
    for destination in destinations.values():
        existing &= np.isfinite(destination)
    take = valid & ~existing
    if not np.any(take):
        return False, quality_mask_applied, warnings

    for band, tile in scene_bands.items():
        destinations[band][take] = tile[take]
    return True, quality_mask_applied, warnings


def _dedupe_warnings(warnings: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for warning in warnings:
        normalized = str(warning).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _load_required_bands_mosaic(
    scenes: list[SentinelScene],
    parcel_geometry: dict,
    bands: tuple[str, ...],
    resolution: str | int | float | None,
) -> tuple[dict[str, np.ndarray], dict[str, Any], list[str], list[str], int]:
    """Load all same-day tiles into one parcel grid with optional SCL masking.

    Missing observations intentionally remain NaN. The renderer later paints
    those NaNs in neutral gray inside the lot instead of leaving visual holes.
    """
    if not scenes:
        raise ValueError("No se encontraron escenas Sentinel-2 para construir el mosaico")

    meta = _build_mosaic_grid(parcel_geometry, resolution)
    loaded = {band: np.full((int(meta["height"]), int(meta["width"])), np.nan, dtype="float32") for band in bands}
    used_scene_ids: set[str] = set()
    warnings: list[str] = []
    quality_mask_scene_count = 0

    for scene in scenes[:MAX_MOSAIC_SCENES]:
        scene_used, quality_mask_applied, scene_warnings = _merge_scene_into_mosaic(scene, bands, loaded, meta)
        warnings.extend(scene_warnings)
        if quality_mask_applied:
            quality_mask_scene_count += 1
        if scene_used:
            used_scene_ids.add(scene.id)

    missing_bands = [band for band, arr in loaded.items() if not np.any(np.isfinite(arr))]
    if missing_bands:
        warnings.insert(0, f"No se encontraron píxeles utilizables para: {', '.join(missing_bands)}. El lote se mostrará en gris.")

    return normalize_band_values(loaded), meta, sorted(used_scene_ids), _dedupe_warnings(warnings), quality_mask_scene_count


def _inside_geometry_mask(meta: dict[str, Any], parcel_geometry: Any) -> np.ndarray:
    shapes = _geometry_shapes_for_meta(parcel_geometry, meta)
    if not shapes:
        return np.zeros((int(meta["height"]), int(meta["width"])), dtype=bool)
    return geometry_mask(
        shapes,
        out_shape=(int(meta["height"]), int(meta["width"])),
        transform=meta["transform"],
        invert=True,
        all_touched=False,
    )


def _coverage_summary(
    arr: np.ndarray,
    meta: dict[str, Any],
    parcel_geometry: Any,
    *,
    inside: np.ndarray | None = None,
) -> dict[str, Any]:
    inside_mask = inside if inside is not None else _inside_geometry_mask(meta, parcel_geometry)
    valid = np.all(np.isfinite(arr), axis=0) if arr.ndim == 3 else np.isfinite(arr)
    total_pixels = int(np.count_nonzero(inside_mask))
    valid_pixels = int(np.count_nonzero(valid & inside_mask))
    unavailable_pixels = max(total_pixels - valid_pixels, 0)
    coverage_percent = round((valid_pixels / total_pixels) * 100, 2) if total_pixels else 0.0
    unavailable_percent = round((unavailable_pixels / total_pixels) * 100, 2) if total_pixels else 0.0
    return {
        "coverage_percent": coverage_percent,
        "unavailable_percent": unavailable_percent,
        "is_partial": coverage_percent < 99.5,
        "quality_placeholder_applied": unavailable_pixels > 0,
        "valid_pixels": valid_pixels,
        "unavailable_pixels": unavailable_pixels,
        "total_parcel_pixels": total_pixels,
    }


def _cache_key(*, user_id: str, parcel_id: str, index_key: str, image_date: str, width: int, height: int, geometry_hash: str = "") -> str:
    payload = {
        "v": 12,
        "processing_version": MOSAIC_PROCESSING_VERSION,
        "geometry_hash": geometry_hash,
        "user_id": user_id,
        "parcel_id": parcel_id,
        "index_key": index_key,
        "image_date": image_date,
        "width": width,
        "height": height,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _latest_manifest_key(*, user_id: str, parcel_id: str, index_key: str, geometry_hash: str = "") -> str:
    """Stable pointer for the best recent immutable raster of a lot/layer.

    The key deliberately excludes width and height. An image rendered once at a
    high enough resolution can be reused by the normal view, comparison and
    zoning maps instead of reading Sentinel bands again for each screen size.
    """
    payload = {
        "v": 4,
        "processing_version": MOSAIC_PROCESSING_VERSION,
        "geometry_hash": geometry_hash,
        "user_id": user_id,
        "parcel_id": parcel_id,
        "index_key": index_key,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _legacy_latest_manifest_key(*, user_id: str, parcel_id: str, index_key: str, width: int, height: int, geometry_hash: str = "") -> str:
    """Pointer format published by the previous cache delivery.

    Keep this read-only migration path so already generated images become
    instantly available after deployment without waiting for another STAC read.
    """
    payload = {
        "v": 3,
        "processing_version": MOSAIC_PROCESSING_VERSION,
        "geometry_hash": geometry_hash,
        "user_id": user_id,
        "parcel_id": parcel_id,
        "index_key": index_key,
        "width": width,
        "height": height,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _date_manifest_key(*, user_id: str, parcel_id: str, index_key: str, image_date: str, geometry_hash: str = "") -> str:
    """Stable pointer for the largest generated raster of one immutable date."""
    payload = {
        "v": 1,
        "processing_version": MOSAIC_PROCESSING_VERSION,
        "geometry_hash": geometry_hash,
        "user_id": user_id,
        "parcel_id": parcel_id,
        "index_key": index_key,
        "image_date": image_date,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _safe_cache_key(cache_key: str) -> str:
    return "".join(ch for ch in cache_key.lower() if ch in "0123456789abcdef")[:80]


def local_png_path(cache_key: str) -> Path:
    return CACHE_DIR / f"{_safe_cache_key(cache_key)}.png"


def local_meta_path(cache_key: str) -> Path:
    return CACHE_DIR / f"{_safe_cache_key(cache_key)}.json"


def _read_local_metadata(cache_key: str) -> dict[str, Any]:
    path = local_meta_path(cache_key)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_local_metadata(cache_key: str, metadata: dict[str, Any]) -> None:
    try:
        local_meta_path(cache_key).write_text(json.dumps(_json_safe(metadata), sort_keys=True))
    except Exception:
        logger.exception("No se pudo escribir metadata local Sentinel-2")


def local_png_url(cache_key: str) -> str:
    version = os.getenv("SENTINEL_CACHE_URL_VERSION", SENTINEL_CACHE_URL_VERSION)
    safe_version = "".join(ch for ch in str(version) if ch.isalnum() or ch in "-_.")[:80] or "v1"
    return f"/api/satellite-free/cache/{cache_key}.png?v={safe_version}"


def _metadata_render_size(metadata: dict[str, Any]) -> tuple[int, int]:
    try:
        width = int(metadata.get("render_width") or metadata.get("width") or 0)
        height = int(metadata.get("render_height") or metadata.get("height") or 0)
    except Exception:
        return 0, 0
    return max(width, 0), max(height, 0)


def _metadata_has_enough_resolution(metadata: dict[str, Any], *, min_width: int, min_height: int) -> bool:
    render_width, render_height = _metadata_render_size(metadata)
    return render_width >= min_width and render_height >= min_height


def _metadata_cloud_is_acceptable(metadata: dict[str, Any], max_cloud: float | None) -> bool:
    if max_cloud is None:
        return True
    try:
        cloud = float(metadata.get("cloud_coverage"))
    except (TypeError, ValueError):
        return True
    return cloud <= float(max_cloud)


def _persistent_result_from_metadata(cache_key: str, metadata: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    """Convert persisted GCS metadata into the regular map-layer result."""
    if not isinstance(metadata, dict):
        return None
    if metadata.get("processing_version") != MOSAIC_PROCESSING_VERSION:
        return None
    if not metadata.get("available") or not metadata.get("bounds") or not metadata.get("date"):
        return None
    result = dict(metadata)
    result.update({
        "available": True,
        "image_url": local_png_url(cache_key),
        "object_path": metadata.get("object_path") or f"cache://{cache_key}",
        "cache_key": cache_key,
        "source": source,
        "processing_version": MOSAIC_PROCESSING_VERSION,
    })
    return _json_safe(result)


def _read_persistent_cache_result(cache_key: str) -> dict[str, Any] | None:
    """Read a generated immutable raster from GCS without touching STAC bands."""
    try:
        metadata = download_satellite_cache_metadata(cache_key)
    except FileNotFoundError:
        return None
    except Exception:
        logger.info("Cache persistente Sentinel-2 no disponible; se continúa con cache local/STAC", exc_info=True)
        return None
    return _persistent_result_from_metadata(cache_key, metadata, source="persistent-cache")


def _result_from_manifest(manifest: dict[str, Any], *, source: str, min_width: int, min_height: int, max_cloud: float | None = None) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    if not _metadata_has_enough_resolution(manifest, min_width=min_width, min_height=min_height):
        return None
    if not _metadata_cloud_is_acceptable(manifest, max_cloud):
        return None
    cache_key = str(manifest.get("cache_key") or "")
    if not cache_key:
        return None
    # Prefer the complete adjacent metadata object. The pointer also carries a
    # copy so a temporary metadata-read failure does not force expensive raster
    # work. Both objects are stored in GCS and survive instance replacement.
    persisted = _read_persistent_cache_result(cache_key)
    if persisted and _metadata_has_enough_resolution(persisted, min_width=min_width, min_height=min_height):
        if _metadata_cloud_is_acceptable(persisted, max_cloud):
            persisted["source"] = source
            return persisted
    return _persistent_result_from_metadata(cache_key, manifest, source=source)


def _read_latest_persistent_result(*, user_id: str, parcel_id: str, index_key: str, width: int, height: int, geometry_hash: str, max_cloud: float | None = None) -> dict[str, Any] | None:
    """Return a recent generated raster pointer from GCS for instant first paint.

    The second lookup migrates manifests created by the previous delivery, whose
    latest pointer still included render dimensions. That lets already processed
    imagery become instant immediately after this deployment.
    """
    manifest_key = _latest_manifest_key(
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        geometry_hash=geometry_hash,
    )
    manifest: dict[str, Any] | None = None
    try:
        manifest = download_satellite_latest_manifest(manifest_key)
    except FileNotFoundError:
        manifest = None
    except Exception:
        logger.info("Manifest latest Sentinel-2 no disponible; se continúa con catálogo", exc_info=True)

    if manifest:
        result = _result_from_manifest(
            manifest,
            source="persistent-latest-cache",
            min_width=width,
            min_height=height,
            max_cloud=max_cloud,
        )
        if result:
            return result

    legacy_manifest_key = _legacy_latest_manifest_key(
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        width=width,
        height=height,
        geometry_hash=geometry_hash,
    )
    try:
        legacy_manifest = download_satellite_latest_manifest(legacy_manifest_key)
    except FileNotFoundError:
        return None
    except Exception:
        logger.info("Manifest legacy Sentinel-2 no disponible; se continúa con catálogo", exc_info=True)
        return None

    promoted = _json_safe({
        **legacy_manifest,
        "render_width": legacy_manifest.get("render_width") or width,
        "render_height": legacy_manifest.get("render_height") or height,
    })
    result = _result_from_manifest(
        promoted,
        source="persistent-latest-cache-migrated",
        min_width=width,
        min_height=height,
        max_cloud=max_cloud,
    )
    if not result:
        return None
    try:
        _publish_persistent_cache_metadata(
            cache_key=str(result.get("cache_key") or promoted.get("cache_key") or ""),
            metadata=promoted,
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            width=width,
            height=height,
            geometry_hash=geometry_hash,
        )
    except Exception:
        logger.info("No se pudo promover manifest legacy Sentinel-2; la imagen se devolverá igual", exc_info=True)
    return result

def _read_date_persistent_result(*, user_id: str, parcel_id: str, index_key: str, image_date: str, width: int, height: int, geometry_hash: str) -> dict[str, Any] | None:
    """Reuse a previously rendered image for the date when it is large enough.

    This makes comparison and zoning instant after the normal 3072px satellite
    image has already been generated. A smaller render never replaces a larger
    one and is not reused when the caller explicitly needs more resolution.
    """
    manifest_key = _date_manifest_key(
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        image_date=image_date,
        geometry_hash=geometry_hash,
    )
    try:
        manifest = download_satellite_date_manifest(manifest_key)
    except FileNotFoundError:
        return None
    except Exception:
        logger.info("Manifest por fecha Sentinel-2 no disponible; se continúa con cache exacta/STAC", exc_info=True)
        return None
    return _result_from_manifest(
        manifest,
        source="persistent-date-cache",
        min_width=width,
        min_height=height,
    )


def _manifest_rank(metadata: dict[str, Any]) -> tuple[str, int]:
    image_date = str(metadata.get("date") or "")[:10]
    width, height = _metadata_render_size(metadata)
    return image_date, width * height


def _publish_best_manifest(*, read_manifest, write_manifest, manifest_key: str, metadata: dict[str, Any]) -> None:
    """Advance one GCS pointer only when date/resolution improves."""
    try:
        previous = read_manifest(manifest_key)
    except Exception:
        previous = None
    if previous and _manifest_rank(previous) >= _manifest_rank(metadata):
        return
    write_manifest(manifest_key, metadata)


def _publish_latest_manifest(*, manifest_key: str, metadata: dict[str, Any]) -> None:
    _publish_best_manifest(
        read_manifest=download_satellite_latest_manifest,
        write_manifest=upload_satellite_latest_manifest,
        manifest_key=manifest_key,
        metadata=metadata,
    )


def _publish_date_manifest(*, manifest_key: str, metadata: dict[str, Any]) -> None:
    _publish_best_manifest(
        read_manifest=download_satellite_date_manifest,
        write_manifest=upload_satellite_date_manifest,
        manifest_key=manifest_key,
        metadata=metadata,
    )


def _publish_persistent_cache_metadata(*, cache_key: str, metadata: dict[str, Any], user_id: str, parcel_id: str, index_key: str, width: int, height: int, geometry_hash: str) -> None:
    """Persist metadata and shared pointers after the PNG alias is safely stored."""
    full_metadata = _json_safe({
        **metadata,
        "available": True,
        "cache_key": cache_key,
        "image_url": local_png_url(cache_key),
        "geometry_hash": geometry_hash,
        "render_width": width,
        "render_height": height,
        "processing_version": MOSAIC_PROCESSING_VERSION,
    })
    upload_satellite_cache_metadata(cache_key, full_metadata)
    latest_manifest_key = _latest_manifest_key(
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        geometry_hash=geometry_hash,
    )
    date_manifest_key = _date_manifest_key(
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        image_date=str(full_metadata.get("date") or "")[:10],
        geometry_hash=geometry_hash,
    )
    _publish_latest_manifest(manifest_key=latest_manifest_key, metadata=full_metadata)
    _publish_date_manifest(manifest_key=date_manifest_key, metadata=full_metadata)


def _backfill_persistent_result_pointers(result: dict[str, Any], *, user_id: str, parcel_id: str, index_key: str, width: int, height: int, geometry_hash: str) -> dict[str, Any]:
    """Promote old cache metadata into shared latest/date pointers once."""
    metadata = _json_safe({
        **result,
        "render_width": result.get("render_width") or width,
        "render_height": result.get("render_height") or height,
    })
    cache_key = str(metadata.get("cache_key") or "")
    if not cache_key:
        return result
    # New metadata already has dimensions and was published by `_store_png`.
    # Only older entries need the one-time promotion write.
    if result.get("render_width") and result.get("render_height"):
        return result
    try:
        _publish_persistent_cache_metadata(
            cache_key=cache_key,
            metadata=metadata,
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            width=width,
            height=height,
            geometry_hash=geometry_hash,
        )
        promoted = _persistent_result_from_metadata(cache_key, metadata, source=str(result.get("source") or "persistent-cache-migrated"))
        return promoted or result
    except Exception:
        logger.info("No se pudo promover metadata Sentinel-2 heredada; la imagen se devolverá igual", exc_info=True)
        return result


def _backfill_existing_png_alias(*, cache_key: str, user_id: str, parcel_id: str, index_key: str, image_date: str, width: int, height: int, geometry_hash: str, parcel_geometry: dict, cloud_coverage: float | None) -> dict[str, Any] | None:
    """Recover an older immutable PNG alias without reading Sentinel COG bands.

    Deliveries before the JSON manifests already uploaded `cache/<hash>.png`.
    Download it once into the instance, synthesize lightweight metadata and
    publish the new pointers. This is still much cheaper than raster generation.
    """
    try:
        content = download_satellite_cache_png_bytes(cache_key)
    except FileNotFoundError:
        return None
    except Exception:
        logger.info("No se pudo comprobar alias PNG Sentinel-2 heredado", exc_info=True)
        return None
    try:
        local_png_path(cache_key).write_bytes(content)
        bounds = bounds_from_gdf(geometry_to_gdf(parcel_geometry))
        metadata = _json_safe({
            "available": True,
            "cache_key": cache_key,
            "date": image_date,
            "cloud_coverage": cloud_coverage,
            "bounds": bounds,
            "statistics": {},
            "object_path": f"cache://{cache_key}",
            "source": "persistent-png-alias-migrated",
            "render_width": width,
            "render_height": height,
            "geometry_hash": geometry_hash,
            "processing_version": MOSAIC_PROCESSING_VERSION,
            "warnings": ["Se reutilizó un PNG Sentinel-2 heredado. Las estadísticas se completarán al regenerar voluntariamente la fecha."],
        })
        _write_local_metadata(cache_key, metadata)
        _publish_persistent_cache_metadata(
            cache_key=cache_key,
            metadata=metadata,
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            width=width,
            height=height,
            geometry_hash=geometry_hash,
        )
        return _persistent_result_from_metadata(cache_key, metadata, source="persistent-png-alias-migrated")
    except Exception:
        logger.info("No se pudo promover alias PNG Sentinel-2 heredado", exc_info=True)
        return None


def get_cached_db_image(db_session, *, user_id: str, parcel_id: str, index_key: str, target_date: date | None) -> SatelliteImage | None:
    if not DB_CACHE_ENABLED:
        return None
    query = db_session.query(SatelliteImage).filter(
        SatelliteImage.user_id == user_id,
        SatelliteImage.parcel_id == parcel_id,
        SatelliteImage.index_type == index_key,
        SatelliteImage.processing_status == ProcessingStatus.completed,
    )
    if target_date:
        start = datetime.combine(target_date, datetime.min.time())
        end = start + timedelta(days=1)
        query = query.filter(SatelliteImage.image_date >= start, SatelliteImage.image_date < end)
    return query.order_by(SatelliteImage.image_date.desc(), SatelliteImage.created_at.desc()).first()


def image_url_from_object_path(object_path: str) -> str:
    if object_path.startswith("local://") or object_path.startswith("cache://"):
        cache_key = object_path.split("://", 1)[1]
        return local_png_url(cache_key)
    return generate_signed_satellite_url(object_path)


def _store_png(png_bytes: bytes, *, user_id: str, parcel_id: str, index_key: str, image_date: str, width: int, height: int, geometry_hash: str = "", metadata: dict[str, Any] | None = None) -> tuple[str, str]:
    cache_key = _cache_key(user_id=user_id, parcel_id=parcel_id, index_key=index_key, image_date=image_date, width=width, height=height, geometry_hash=geometry_hash)
    path = local_png_path(cache_key)
    path.write_bytes(png_bytes)
    base_metadata = _json_safe({
        **(metadata or {}),
        "available": True,
        "cache_key": cache_key,
        "date": image_date,
        "geometry_hash": geometry_hash,
        "render_width": width,
        "render_height": height,
        "processing_version": MOSAIC_PROCESSING_VERSION,
    })
    _write_local_metadata(cache_key, base_metadata)

    try:
        object_path = upload_satellite_png_bytes(
            png_bytes,
            user_id,
            parcel_id,
            index_key,
            f"{image_date}-{geometry_hash or cache_key[:12]}",
            cache_key=cache_key,
        )
        persisted_metadata = _json_safe({**base_metadata, "object_path": object_path})
        _write_local_metadata(cache_key, persisted_metadata)
        _publish_persistent_cache_metadata(
            cache_key=cache_key,
            metadata=persisted_metadata,
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            width=width,
            height=height,
            geometry_hash=geometry_hash,
        )
        # Return the app cache endpoint instead of a signed GCS URL so CORS,
        # cache busting and auth-free <img> loading stay under our control.
        return object_path, local_png_url(cache_key)
    except Exception:
        # GCS is optional for local/dev. The frontend can load the public cache
        # route without Authorization headers, which is required for imageOverlay.
        logger.info("No se pudo publicar cache persistente Sentinel-2; se conserva cache local", exc_info=True)
        return f"local://{cache_key}", local_png_url(cache_key)


def generate_or_get_layer(
    db_session,
    *,
    parcel_geometry: dict,
    user_id: str,
    parcel_id: str,
    index_key: str,
    target_date: date | None,
    max_cloud: float = DEFAULT_MAX_CLOUD,
    width: int = 1024,
    height: int = 1024,
    force_refresh: bool = False,
    revalidate_latest: bool = False,
) -> dict[str, Any]:
    index_key = normalize_index_key(index_key)
    geometry_fingerprint = _geometry_hash(parcel_geometry)
    definition = INDEX_DEFINITIONS.get(index_key)
    if not definition:
        raise ValueError(f"Índice no soportado: {index_key}")

    # Persistent GCS manifests make generated rasters reusable after logout,
    # browser-data deletion and Cloud Run instance replacement. For latest
    # requests the UI gets an instant first paint; a lightweight background
    # revalidation can later ask STAC whether a newer date exists.
    if not force_refresh and target_date is None and not revalidate_latest:
        latest_cached = _read_latest_persistent_result(
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            width=width,
            height=height,
            geometry_hash=geometry_fingerprint,
            max_cloud=max_cloud,
        )
        if latest_cached:
            return latest_cached

    # An exact date is immutable. Resolve it from the persistent hash alias
    # before any STAC catalog or remote COG request.
    if not force_refresh and target_date is not None:
        exact_cache_key = _cache_key(
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            image_date=target_date.isoformat(),
            width=width,
            height=height,
            geometry_hash=geometry_fingerprint,
        )
        exact_cached = _read_persistent_cache_result(exact_cache_key)
        if exact_cached:
            return _backfill_persistent_result_pointers(
                exact_cached,
                user_id=user_id,
                parcel_id=parcel_id,
                index_key=index_key,
                width=width,
                height=height,
                geometry_hash=geometry_fingerprint,
            )
        reusable_date_cached = _read_date_persistent_result(
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            image_date=target_date.isoformat(),
            width=width,
            height=height,
            geometry_hash=geometry_fingerprint,
        )
        if reusable_date_cached:
            return reusable_date_cached

    cached = None
    if not force_refresh and DB_CACHE_ENABLED:
        try:
            cached = get_cached_db_image(
                db_session,
                user_id=user_id,
                parcel_id=parcel_id,
                index_key=index_key,
                target_date=target_date,
            )
        except Exception:
            logger.exception("No se pudo leer cache DB Sentinel-2; se continúa sin cache DB")
            try:
                db_session.rollback()
            except Exception:
                pass
    cached_stats = cached.statistics if cached and isinstance(cached.statistics, dict) else {}
    if (
        cached
        and cached.image_object_path
        and cached_stats.get("processing_version") == MOSAIC_PROCESSING_VERSION
        and _metadata_has_enough_resolution(cached_stats, min_width=width, min_height=height)
    ):
        return _json_safe({
            "available": True,
            "date": cached.image_date.date().isoformat(),
            "cloud_coverage": cached.cloud_coverage,
            "bounds": cached.bounds,
            "statistics": cached_stats,
            "image_url": image_url_from_object_path(cached.image_object_path),
            "object_path": cached.image_object_path,
            "source": "cache",
            "coverage_percent": cached_stats.get("coverage_percent"),
            "unavailable_percent": cached_stats.get("unavailable_percent"),
            "is_partial": bool(cached_stats.get("is_partial", False)),
            "quality_placeholder_applied": bool(cached_stats.get("quality_placeholder_applied", False)),
            "quality_mask_applied": bool(cached_stats.get("quality_mask_applied", False)),
            "source_count": cached_stats.get("source_count", 1),
            "scene_ids": cached_stats.get("scene_ids") or [],
            "processing_version": MOSAIC_PROCESSING_VERSION,
        })

    bbox = bbox_from_geometry(parcel_geometry)
    primary_scene = best_scene_for_date(bbox=bbox, target_date=target_date, max_cloud=max_cloud, lookback_days=MAP_LOOKBACK_DAYS)
    if not primary_scene:
        return {
            "available": False,
            "reason": "No se encontraron escenas Sentinel-2 L2A con el nivel de nubosidad solicitado.",
        }

    image_date = primary_scene.datetime.date()
    cache_key = _cache_key(
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        image_date=image_date.isoformat(),
        width=width,
        height=height,
        geometry_hash=geometry_fingerprint,
    )

    # Revalidation may need one STAC catalog lookup to discover the latest date,
    # but it must still avoid remote band reads when that immutable date was
    # already generated by any previous Cloud Run instance.
    if not force_refresh:
        persistent_cached = _read_persistent_cache_result(cache_key)
        if persistent_cached:
            return _backfill_persistent_result_pointers(
                persistent_cached,
                user_id=user_id,
                parcel_id=parcel_id,
                index_key=index_key,
                width=width,
                height=height,
                geometry_hash=geometry_fingerprint,
            )
        reusable_date_cached = _read_date_persistent_result(
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            image_date=image_date.isoformat(),
            width=width,
            height=height,
            geometry_hash=geometry_fingerprint,
        )
        if reusable_date_cached:
            return reusable_date_cached

    local_path = local_png_path(cache_key)
    if not force_refresh and local_path.exists():
        cached_meta = _read_local_metadata(cache_key)
        local_result = _json_safe({
            "available": True,
            "date": cached_meta.get("date") or image_date.isoformat(),
            "cloud_coverage": cached_meta.get("cloud_coverage", primary_scene.cloud_cover),
            "bounds": cached_meta.get("bounds") or bounds_from_gdf(geometry_to_gdf(parcel_geometry)),
            "statistics": cached_meta.get("statistics") or {},
            "image_url": local_png_url(cache_key),
            "object_path": cached_meta.get("object_path") or f"local://{cache_key}",
            "source": "local-cache",
            "scene_id": cached_meta.get("scene_id") or primary_scene.id,
            "scene_ids": cached_meta.get("scene_ids") or [primary_scene.id],
            "source_count": cached_meta.get("source_count") or 1,
            "coverage_percent": cached_meta.get("coverage_percent"),
            "unavailable_percent": cached_meta.get("unavailable_percent"),
            "is_partial": bool(cached_meta.get("is_partial", False)),
            "quality_placeholder_applied": bool(cached_meta.get("quality_placeholder_applied", False)),
            "quality_mask_applied": bool(cached_meta.get("quality_mask_applied", False)),
            "processing_version": cached_meta.get("processing_version") or MOSAIC_PROCESSING_VERSION,
            "warnings": cached_meta.get("warnings") or [],
            "render_width": cached_meta.get("render_width") or width,
            "render_height": cached_meta.get("render_height") or height,
        })
        # Backfill the GCS hash alias and metadata for local-cache hits. This is
        # important when an older instance generated the PNG before persistent
        # manifests were enabled.
        try:
            object_path = upload_satellite_png_bytes(
                local_path.read_bytes(),
                user_id,
                parcel_id,
                index_key,
                f"{image_date.isoformat()}-{geometry_fingerprint or cache_key[:12]}",
                cache_key=cache_key,
            )
            published = _json_safe({**local_result, "object_path": object_path})
            _write_local_metadata(cache_key, published)
            _publish_persistent_cache_metadata(
                cache_key=cache_key,
                metadata=published,
                user_id=user_id,
                parcel_id=parcel_id,
                index_key=index_key,
                width=width,
                height=height,
                geometry_hash=geometry_fingerprint,
            )
            return published
        except Exception:
            logger.info("No se pudo respaldar local-cache Sentinel-2 en GCS; se usará cache local", exc_info=True)
        return local_result

    if not force_refresh:
        migrated_alias = _backfill_existing_png_alias(
            cache_key=cache_key,
            user_id=user_id,
            parcel_id=parcel_id,
            index_key=index_key,
            image_date=image_date.isoformat(),
            width=width,
            height=height,
            geometry_hash=geometry_fingerprint,
            parcel_geometry=parcel_geometry,
            cloud_coverage=primary_scene.cloud_cover,
        )
        if migrated_alias:
            return migrated_alias

    # Once the date is chosen, collect every intersecting same-day tile even if
    # one tile exceeds the cloud threshold. Excluding a cloudy boundary tile
    # creates a transparent half-lot. SCL masking below marks its unusable pixels
    # in gray while retaining any valid observations.
    scenes = scenes_for_date(bbox=bbox, target_date=image_date, max_cloud=None, limit=MOSAIC_CATALOG_LIMIT)
    if not scenes:
        scenes = [primary_scene]

    bands, _meta, used_scene_ids, mosaic_warnings, quality_mask_scene_count = _load_required_bands_mosaic(
        scenes,
        parcel_geometry,
        definition.bands,
        definition.resolution,
    )
    arr = definition.compute(bands)
    # Normalize the computed index to Web Mercator, the projection used by the
    # map renderer. This removes the small but visible shift/scale difference
    # caused by displaying a native UTM Sentinel crop as a Leaflet image overlay.
    arr, output_meta, bounds = _reproject_array_to_web_mercator(arr, _meta)
    arr = _apply_precise_geometry_mask(arr, output_meta, parcel_geometry)
    inside_mask = _inside_geometry_mask(output_meta, parcel_geometry)
    coverage = _coverage_summary(arr, output_meta, parcel_geometry, inside=inside_mask)
    quality_mask_applied = quality_mask_scene_count > 0
    if coverage["quality_placeholder_applied"]:
        mosaic_warnings.insert(
            0,
            f"{coverage['unavailable_percent']:.2f}% del lote no tiene observaciones utilizables para esta fecha y se muestra en gris.",
        )
    stats = compute_statistics(arr, index_key=index_key)
    stats.update({
        **coverage,
        "processing_version": MOSAIC_PROCESSING_VERSION,
        "source_count": len(used_scene_ids),
        "scene_ids": used_scene_ids,
        "candidate_scene_count": len(scenes),
        "quality_mask_applied": quality_mask_applied,
        "quality_mask_scene_count": quality_mask_scene_count,
        "render_width": width,
        "render_height": height,
    })
    png_bytes = _fit_png_to_display_size(
        render_index_png(index_key, arr, unavailable_mask=inside_mask),
        width,
        height,
        preserve_pixels=not definition.rgb,
    )
    local_metadata = {
        "available": True,
        "date": image_date.isoformat(),
        "cloud_coverage": primary_scene.cloud_cover,
        "bounds": bounds,
        "statistics": stats,
        "source": "sentinel-2-l2a-mosaic",
        "scene_id": primary_scene.id,
        "scene_ids": used_scene_ids,
        "source_count": len(used_scene_ids),
        "candidate_scene_count": len(scenes),
        "coverage_percent": coverage["coverage_percent"],
        "unavailable_percent": coverage["unavailable_percent"],
        "is_partial": coverage["is_partial"],
        "quality_placeholder_applied": coverage["quality_placeholder_applied"],
        "quality_mask_applied": quality_mask_applied,
        "processing_version": MOSAIC_PROCESSING_VERSION,
        "warnings": _dedupe_warnings(mosaic_warnings)[:6],
        "geometry_hash": geometry_fingerprint,
    }
    object_path, image_url = _store_png(
        png_bytes,
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        image_date=image_date.isoformat(),
        width=width,
        height=height,
        geometry_hash=geometry_fingerprint,
        metadata=local_metadata,
    )

    if DB_CACHE_ENABLED:
        try:
            start_dt = datetime.combine(image_date, datetime.min.time())
            end_dt = start_dt + timedelta(days=1)
            db_obj = db_session.query(SatelliteImage).filter(
                SatelliteImage.user_id == user_id,
                SatelliteImage.parcel_id == parcel_id,
                SatelliteImage.index_type == index_key,
                SatelliteImage.image_date >= start_dt,
                SatelliteImage.image_date < end_dt,
            ).first()
            if not db_obj:
                db_obj = SatelliteImage(
                    user_id=user_id,
                    parcel_id=parcel_id,
                    image_date=start_dt,
                    index_type=index_key,
                    image_object_path=object_path,
                    processing_status=ProcessingStatus.completed,
                    cloud_coverage=primary_scene.cloud_cover,
                    bounds=_json_safe(bounds),
                    statistics=_json_safe(stats),
                )
                db_session.add(db_obj)
            else:
                db_obj.image_object_path = object_path
                db_obj.processing_status = ProcessingStatus.completed
                db_obj.cloud_coverage = primary_scene.cloud_cover
                db_obj.bounds = _json_safe(bounds)
                db_obj.statistics = _json_safe(stats)
            db_session.commit()
        except Exception:
            logger.exception("No se pudo guardar cache DB Sentinel-2; la imagen se devolverá igual")
            try:
                db_session.rollback()
            except Exception:
                pass

    return _json_safe({
        "available": True,
        "date": image_date.isoformat(),
        "cloud_coverage": primary_scene.cloud_cover,
        "bounds": bounds,
        "statistics": stats,
        "image_url": image_url,
        "object_path": object_path,
        "source": "sentinel-2-l2a-mosaic",
        "scene_id": primary_scene.id,
        "scene_ids": used_scene_ids,
        "source_count": len(used_scene_ids),
        "candidate_scene_count": len(scenes),
        "coverage_percent": coverage["coverage_percent"],
        "unavailable_percent": coverage["unavailable_percent"],
        "is_partial": coverage["is_partial"],
        "quality_placeholder_applied": coverage["quality_placeholder_applied"],
        "quality_mask_applied": quality_mask_applied,
        "processing_version": MOSAIC_PROCESSING_VERSION,
        "warnings": _dedupe_warnings(mosaic_warnings)[:6],
    })
