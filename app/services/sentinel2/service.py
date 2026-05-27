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
from rasterio.transform import array_bounds
from rasterio.warp import reproject, transform_bounds
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
try:
    from shapely.validation import make_valid as _shapely_make_valid
except Exception:  # Shapely < 2 fallback
    _shapely_make_valid = None
from shapely.ops import unary_union

from app.models.satellite_image import ProcessingStatus, SatelliteImage
from app.services.satellite.utils import featurecollection_to_geometry
from app.services.sentinel2.catalog import available_dates, best_scene_for_date, normalize_asset_href, sign_item_if_needed
from app.services.sentinel2.indices import (
    BAND_ASSET_ALIASES,
    INDEX_DEFINITIONS,
    normalize_band_values,
    normalize_index_key,
)
from app.services.sentinel2.render import compute_statistics, render_index_png
from PIL import Image
from io import BytesIO
from app.utils.storage_satellite import generate_signed_satellite_url, upload_satellite_png_bytes

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.getenv("SENTINEL_LOCAL_CACHE_DIR", str(Path(tempfile.gettempdir()) / "dataris_sentinel2_cache")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MAX_CLOUD = float(os.getenv("SENTINEL_DEFAULT_MAX_CLOUD", "80"))
DATE_LOOKBACK_DAYS = int(os.getenv("SENTINEL_DATE_LOOKBACK_DAYS", "180"))
MAP_LOOKBACK_DAYS = int(os.getenv("SENTINEL_MAP_LOOKBACK_DAYS", "90"))
DB_CACHE_ENABLED = os.getenv("SENTINEL_DB_CACHE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
MASK_ALL_TOUCHED = os.getenv("SENTINEL_MASK_ALL_TOUCHED", "true").strip().lower() not in {"0", "false", "no", "off"}

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


def _clean_geometry(geom: BaseGeometry) -> BaseGeometry:
    """Repair common invalid rings without changing the intended lot outline."""
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


def _resize_png_if_needed(png_bytes: bytes, max_width: int, max_height: int) -> bytes:
    if max_width <= 0 or max_height <= 0:
        return png_bytes
    with Image.open(BytesIO(png_bytes)) as image:
        if image.width <= max_width and image.height <= max_height:
            return png_bytes
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
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
        all_touched=MASK_ALL_TOUCHED,
    )
    if arr.ndim == 3:
        masked = arr.astype("float32", copy=True)
        masked[:, ~inside] = np.nan
        return masked
    masked = arr.astype("float32", copy=True)
    masked[~inside] = np.nan
    return masked


def _load_required_bands(item: Any, parcel_geometry: dict, bands: tuple[str, ...]) -> tuple[dict[str, np.ndarray], dict[str, Any], dict[str, float]]:
    signed_item = sign_item_if_needed(item)
    gdf = geometry_to_gdf(parcel_geometry)
    ref_href = _asset_href(signed_item, "red" if "red" in bands else bands[0])

    with rasterio.Env(**RASTERIO_ENV):
        with rasterio.open(normalize_asset_href(ref_href)) as src:
            gdf_proj = gdf.to_crs(src.crs)
            shapes = [mapping(geom) for geom in gdf_proj.geometry]

    loaded: dict[str, np.ndarray] = {}
    metas: dict[str, dict[str, Any]] = {}
    for band in bands:
        href = _asset_href(signed_item, band)
        arr, meta = _clip_asset(href, shapes)
        loaded[band] = arr
        metas[band] = meta

    ref_band = "red" if "red" in loaded else next(iter(loaded.keys()))
    ref_meta = metas[ref_band]
    ref_shape = loaded[ref_band].shape

    for band, arr in list(loaded.items()):
        meta = metas[band]
        if arr.shape == ref_shape and meta.get("transform") == ref_meta.get("transform") and meta.get("crs") == ref_meta.get("crs"):
            continue
        resampled = np.full(ref_shape, np.nan, dtype="float32")
        reproject(
            arr,
            resampled,
            src_transform=meta["transform"],
            src_crs=meta["crs"],
            dst_transform=ref_meta["transform"],
            dst_crs=ref_meta["crs"],
            resampling=Resampling.bilinear,
            src_nodata=np.nan,
            dst_nodata=np.nan,
        )
        loaded[band] = resampled

    return normalize_band_values(loaded), ref_meta, _bounds_from_raster_meta(ref_meta)


def _cache_key(*, user_id: str, parcel_id: str, index_key: str, image_date: str, width: int, height: int, geometry_hash: str = "") -> str:
    payload = {
        "v": 3,
        "geometry_hash": geometry_hash,
        "user_id": user_id,
        "parcel_id": parcel_id,
        "index_key": index_key,
        "image_date": image_date,
        "width": width,
        "height": height,
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
    return f"/api/satellite-free/cache/{cache_key}.png"


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
    if object_path.startswith("local://"):
        cache_key = object_path.split("local://", 1)[1]
        return local_png_url(cache_key)
    return generate_signed_satellite_url(object_path)


def _store_png(png_bytes: bytes, *, user_id: str, parcel_id: str, index_key: str, image_date: str, width: int, height: int, geometry_hash: str = "", metadata: dict[str, Any] | None = None) -> tuple[str, str]:
    cache_key = _cache_key(user_id=user_id, parcel_id=parcel_id, index_key=index_key, image_date=image_date, width=width, height=height, geometry_hash=geometry_hash)
    path = local_png_path(cache_key)
    path.write_bytes(png_bytes)
    if metadata is not None:
        _write_local_metadata(cache_key, metadata)

    try:
        object_path = upload_satellite_png_bytes(png_bytes, user_id, parcel_id, index_key, f"{image_date}-{geometry_hash or cache_key[:12]}")
        return object_path, generate_signed_satellite_url(object_path)
    except Exception:
        # GCS is optional for local/dev. The frontend can load the public cache
        # route without Authorization headers, which is required for imageOverlay.
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
) -> dict[str, Any]:
    index_key = normalize_index_key(index_key)
    geometry_fingerprint = _geometry_hash(parcel_geometry)
    definition = INDEX_DEFINITIONS.get(index_key)
    if not definition:
        raise ValueError(f"Índice no soportado: {index_key}")

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
    if cached and cached.image_object_path:
        return _json_safe({
            "available": True,
            "date": cached.image_date.date().isoformat(),
            "cloud_coverage": cached.cloud_coverage,
            "bounds": cached.bounds,
            "statistics": cached.statistics or {},
            "image_url": image_url_from_object_path(cached.image_object_path),
            "object_path": cached.image_object_path,
            "source": "cache",
        })

    bbox = bbox_from_geometry(parcel_geometry)
    scene = best_scene_for_date(bbox=bbox, target_date=target_date, max_cloud=max_cloud, lookback_days=MAP_LOOKBACK_DAYS)
    if not scene:
        return {
            "available": False,
            "reason": "No se encontraron escenas Sentinel-2 L2A con el nivel de nubosidad solicitado.",
        }

    image_date = scene.datetime.date()
    cache_key = _cache_key(
        user_id=user_id,
        parcel_id=parcel_id,
        index_key=index_key,
        image_date=image_date.isoformat(),
        width=width,
        height=height,
        geometry_hash=geometry_fingerprint,
    )
    local_path = local_png_path(cache_key)
    if not force_refresh and local_path.exists():
        cached_meta = _read_local_metadata(cache_key)
        return _json_safe({
            "available": True,
            "date": cached_meta.get("date") or image_date.isoformat(),
            "cloud_coverage": cached_meta.get("cloud_coverage", scene.cloud_cover),
            "bounds": cached_meta.get("bounds") or bounds_from_gdf(geometry_to_gdf(parcel_geometry)),
            "statistics": cached_meta.get("statistics") or {},
            "image_url": local_png_url(cache_key),
            "object_path": f"local://{cache_key}",
            "source": "local-cache",
            "scene_id": cached_meta.get("scene_id") or scene.id,
        })

    bands, _meta, bounds = _load_required_bands(scene.item, parcel_geometry, definition.bands)
    arr = definition.compute(bands)
    arr = _apply_precise_geometry_mask(arr, _meta, parcel_geometry)
    stats = compute_statistics(arr)
    png_bytes = _resize_png_if_needed(render_index_png(index_key, arr), width, height)
    local_metadata = {
        "available": True,
        "date": image_date.isoformat(),
        "cloud_coverage": scene.cloud_cover,
        "bounds": bounds,
        "statistics": stats,
        "source": "sentinel-2-l2a",
        "scene_id": scene.id,
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
                    cloud_coverage=scene.cloud_cover,
                    bounds=_json_safe(bounds),
                    statistics=_json_safe(stats),
                )
                db_session.add(db_obj)
            else:
                db_obj.image_object_path = object_path
                db_obj.processing_status = ProcessingStatus.completed
                db_obj.cloud_coverage = scene.cloud_cover
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
        "cloud_coverage": scene.cloud_cover,
        "bounds": bounds,
        "statistics": stats,
        "image_url": image_url,
        "object_path": object_path,
        "source": "sentinel-2-l2a",
        "scene_id": scene.id,
    })
