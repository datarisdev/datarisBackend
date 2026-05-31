from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import quote

from pystac_client import Client

# Earth Search usually exposes public COGs through s3:// hrefs. In Cloud Run,
# unsigned S3 reads can fail unless GDAL receives these flags. We also normalize
# s3:// hrefs to HTTPS in the raster service, which avoids AWS credentials.
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("AWS_REGION", os.getenv("SENTINEL_AWS_REGION", "us-west-2"))
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "2")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")

_RUNTIME_CACHE: dict[str, tuple[float, Any]] = {}

DEFAULT_STAC_URL = os.getenv("SENTINEL_STAC_URL", "https://earth-search.aws.element84.com/v1")
DEFAULT_COLLECTION = os.getenv("SENTINEL_STAC_COLLECTION", "sentinel-2-l2a")
DEFAULT_COLLECTIONS = [c.strip() for c in os.getenv("SENTINEL_STAC_COLLECTIONS", DEFAULT_COLLECTION).split(",") if c.strip()]
DEFAULT_PROVIDER = os.getenv("SENTINEL_STAC_PROVIDER", "earthsearch").lower()
CATALOG_TTL_SECONDS = int(os.getenv("SENTINEL_CATALOG_CACHE_TTL_SECONDS", str(60 * 60 * 6)))


def _cache_get(key: str) -> Any | None:
    item = _RUNTIME_CACHE.get(key)
    if not item:
        return None
    expires_at, value = item
    if expires_at <= time.time():
        _RUNTIME_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl: int = CATALOG_TTL_SECONDS) -> Any:
    if len(_RUNTIME_CACHE) > 500:
        now = time.time()
        for stale_key, (expires_at, _) in list(_RUNTIME_CACHE.items()):
            if expires_at <= now:
                _RUNTIME_CACHE.pop(stale_key, None)
        if len(_RUNTIME_CACHE) > 500:
            for stale_key in list(_RUNTIME_CACHE.keys())[:100]:
                _RUNTIME_CACHE.pop(stale_key, None)
    _RUNTIME_CACHE[key] = (time.time() + max(ttl, 1), value)
    return value


@dataclass(frozen=True)
class SentinelScene:
    id: str
    datetime: datetime
    cloud_cover: float | None
    item: Any


def _normalize_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.utcnow()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def open_stac_client() -> Client:
    return Client.open(DEFAULT_STAC_URL)


def normalize_asset_href(href: str) -> str:
    """Return a rasterio/GDAL friendly URL for public STAC assets.

    Earth Search may return s3://sentinel-cogs/... hrefs. Those are public, but
    Cloud Run containers usually do not have AWS credentials and GDAL may try to
    sign S3 requests. Converting to HTTPS makes the request anonymous and avoids
    the common 500 generated while opening the raster.
    """
    raw = str(href or "").strip()
    if not raw.lower().startswith("s3://"):
        return raw

    remainder = raw[5:]
    if "/" not in remainder:
        return raw
    bucket, key = remainder.split("/", 1)
    safe_key = "/".join(quote(part) for part in key.split("/"))

    # The public Sentinel COG bucket used by Earth Search is in us-west-2.
    # For unknown buckets the generic endpoint normally redirects correctly.
    region = os.getenv("SENTINEL_AWS_REGION", "us-west-2").strip() or "us-west-2"
    if bucket in {"sentinel-cogs", "sentinel-s2-l2a-cogs"}:
        return f"https://{bucket}.s3.{region}.amazonaws.com/{safe_key}"
    return f"https://{bucket}.s3.amazonaws.com/{safe_key}"


def sign_item_if_needed(item: Any) -> Any:
    # Microsoft Planetary Computer requires SAS signing. Earth Search and CDSE
    # STAC do not use planetary_computer signing. Keeping this optional makes the
    # service provider-switchable by environment variables.
    if DEFAULT_PROVIDER in {"planetary", "planetary_computer", "microsoft"}:
        import planetary_computer
        return planetary_computer.sign(item)
    return item


def search_scenes(
    *,
    bbox: Iterable[float],
    start_date: date,
    end_date: date,
    max_cloud: float | None = 80.0,
    limit: int = 40,
) -> list[SentinelScene]:
    bbox_list = [float(v) for v in bbox]
    cache_key = f"scenes:{DEFAULT_STAC_URL}:{','.join(DEFAULT_COLLECTIONS)}:{bbox_list}:{start_date}:{end_date}:{max_cloud}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = open_stac_client()
    query: dict[str, Any] = {
        "collections": DEFAULT_COLLECTIONS,
        "bbox": bbox_list,
        "datetime": f"{start_date.isoformat()}/{(end_date + timedelta(days=1)).isoformat()}",
        "limit": limit,
    }
    if max_cloud is not None:
        query["query"] = {"eo:cloud_cover": {"lte": float(max_cloud)}}

    items = list(client.search(**query).items())
    scenes: list[SentinelScene] = []
    for item in items:
        props = item.properties or {}
        dt = _normalize_datetime(props.get("datetime"))
        cloud = props.get("eo:cloud_cover")
        try:
            cloud_value = None if cloud is None else float(cloud)
        except Exception:
            cloud_value = None
        scenes.append(SentinelScene(id=item.id, datetime=dt, cloud_cover=cloud_value, item=item))

    scenes.sort(key=lambda scene: (scene.datetime, -(scene.cloud_cover or 0)), reverse=True)
    return _cache_set(cache_key, scenes)


def available_dates(
    *,
    bbox: Iterable[float],
    start_date: date,
    end_date: date,
    max_cloud: float | None = 80.0,
    limit: int = 120,
) -> list[dict[str, Any]]:
    scenes = search_scenes(bbox=bbox, start_date=start_date, end_date=end_date, max_cloud=max_cloud, limit=limit)
    by_date: dict[str, dict[str, Any]] = {}
    for scene in scenes:
        key = scene.datetime.date().isoformat()
        current = by_date.get(key)
        if current is None:
            current = {
                "date": key,
                "cloudCoverage": scene.cloud_cover,
                "cloud_coverage": scene.cloud_cover,
                "scene_id": scene.id,
                "scene_count": 0,
                "source": "Sentinel-2 L2A",
            }
            by_date[key] = current
        current["scene_count"] = int(current.get("scene_count") or 0) + 1
        current_cloud = current.get("cloudCoverage")
        if (scene.cloud_cover if scene.cloud_cover is not None else 999.0) < (current_cloud if current_cloud is not None else 999.0):
            current["cloudCoverage"] = scene.cloud_cover
            current["cloud_coverage"] = scene.cloud_cover
            current["scene_id"] = scene.id
    return sorted(by_date.values(), key=lambda item: item["date"], reverse=True)



def _scene_tile_key(scene: SentinelScene) -> str:
    """Return a stable tile identifier so same-day duplicate catalog items do not repeat work."""
    props = scene.item.properties or {}
    for key in ("s2:mgrs_tile", "mgrs:tile", "grid:code"):
        value = props.get(key)
        if value:
            return f"{key}:{value}"

    mgrs_parts = [
        props.get("mgrs:utm_zone"),
        props.get("mgrs:latitude_band"),
        props.get("mgrs:grid_square"),
    ]
    if any(value not in (None, "") for value in mgrs_parts):
        return "mgrs:" + ":".join(str(value or "") for value in mgrs_parts)

    # Earth Search Sentinel-2 item ids normally include the MGRS tile. Keep the
    # complete id as a safe fallback rather than accidentally merging different tiles.
    return scene.id


def scenes_for_date(
    *,
    bbox: Iterable[float],
    target_date: date,
    max_cloud: float | None = 80.0,
    limit: int = 80,
) -> list[SentinelScene]:
    """Return every useful Sentinel-2 tile intersecting the parcel on one date.

    A parcel can cross the edge of two Sentinel MGRS tiles. Selecting a single
    catalog item makes only part of the lot visible. This helper keeps the best
    item per tile and lets the raster service build a real same-day mosaic.
    """
    scenes = search_scenes(
        bbox=bbox,
        start_date=target_date,
        end_date=target_date,
        max_cloud=max_cloud,
        limit=limit,
    )
    same_day = [scene for scene in scenes if scene.datetime.date() == target_date]
    by_tile: dict[str, SentinelScene] = {}
    for scene in same_day:
        tile_key = _scene_tile_key(scene)
        current = by_tile.get(tile_key)
        scene_cloud = scene.cloud_cover if scene.cloud_cover is not None else 999.0
        current_cloud = current.cloud_cover if current and current.cloud_cover is not None else 999.0
        if current is None or scene_cloud < current_cloud:
            by_tile[tile_key] = scene
    return sorted(
        by_tile.values(),
        key=lambda scene: (scene.cloud_cover if scene.cloud_cover is not None else 999.0, scene.id),
    )

def best_scene_for_date(
    *,
    bbox: Iterable[float],
    target_date: date | None,
    max_cloud: float | None = 80.0,
    lookback_days: int = 90,
) -> SentinelScene | None:
    end = target_date or date.today()
    start = end if target_date else end - timedelta(days=lookback_days)
    # For a manually selected date, search a small window around that date to
    # handle catalog/provider date-time edge cases while still returning the date
    # the user selected when it exists.
    if target_date:
        start = target_date - timedelta(days=1)
        end = target_date + timedelta(days=1)

    scenes = search_scenes(bbox=bbox, start_date=start, end_date=end, max_cloud=max_cloud, limit=30)
    if target_date:
        same_day = [s for s in scenes if s.datetime.date() == target_date]
        if same_day:
            return sorted(same_day, key=lambda s: (s.cloud_cover if s.cloud_cover is not None else 999))[0]
    if not scenes:
        return None
    return sorted(scenes, key=lambda s: (abs(((target_date or date.today()) - s.datetime.date()).days), s.cloud_cover if s.cloud_cover is not None else 999))[0]
