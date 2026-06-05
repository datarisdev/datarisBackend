from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from typing import Any

import numpy as np
import rasterio

from app.utils.gcs import get_storage_client

try:
    from google.api_core.exceptions import NotFound as GoogleCloudNotFound
except Exception:  # pragma: no cover - optional in local environments
    class GoogleCloudNotFound(Exception):
        pass

BUCKET_NAME = os.getenv("GCS_SATELLITE_BUCKET_NAME", "dataris-satellite")


def _bucket():
    """Return the satellite bucket lazily.

    This prevents Vercel from crashing while importing app/main.py when Google
    credentials are not configured. Only routes that actually need GCS will fail
    with a clear error.
    """
    client = get_storage_client()
    if client is None:
        raise RuntimeError(
            "Google Cloud Storage credentials are not configured. "
            "Set GCS_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS in Vercel."
        )
    return client.bucket(os.getenv("GCS_SATELLITE_BUCKET_NAME") or BUCKET_NAME)


def safe_satellite_cache_key(cache_key: str) -> str:
    """Keep deterministic hash-based cache aliases safe for local disk and GCS."""
    return "".join(ch for ch in str(cache_key).lower() if ch in "0123456789abcdef")[:80]


def _safe_manifest_key(manifest_key: str) -> str:
    safe_key = safe_satellite_cache_key(manifest_key)
    if not safe_key:
        raise ValueError("Invalid Sentinel-2 manifest key")
    return safe_key


def satellite_cache_object_path(cache_key: str) -> str:
    safe_key = safe_satellite_cache_key(cache_key)
    if not safe_key:
        raise ValueError("Invalid Sentinel-2 cache key")
    return f"cache/{safe_key}.png"


def satellite_cache_metadata_object_path(cache_key: str) -> str:
    safe_key = safe_satellite_cache_key(cache_key)
    if not safe_key:
        raise ValueError("Invalid Sentinel-2 cache key")
    return f"cache/{safe_key}.json"


def satellite_latest_manifest_object_path(manifest_key: str) -> str:
    return f"manifests/latest/{_safe_manifest_key(manifest_key)}.json"


def satellite_date_manifest_object_path(manifest_key: str) -> str:
    return f"manifests/date/{_safe_manifest_key(manifest_key)}.json"


def satellite_catalog_manifest_object_path(manifest_key: str) -> str:
    return f"manifests/catalog/{_safe_manifest_key(manifest_key)}.json"


def _download_json_blob(object_path: str) -> dict[str, Any]:
    blob = _bucket().blob(object_path)
    try:
        # download_as_bytes already returns 404 when the object does not exist.
        # Avoid blob.exists() first: it adds one extra GCS round trip to every
        # login/re-entry cache hit.
        raw = blob.download_as_bytes()
    except GoogleCloudNotFound as exc:
        raise FileNotFoundError(object_path) from exc
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid JSON object stored at {object_path}")
    return payload


def _upload_json_blob(object_path: str, payload: dict[str, Any], *, cache_control: str = "no-cache") -> None:
    blob = _bucket().blob(object_path)
    blob.cache_control = cache_control
    blob.upload_from_string(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        content_type="application/json; charset=utf-8",
    )


def download_satellite_cache_png_bytes(cache_key: str) -> bytes:
    """Read the persistent Sentinel-2 PNG alias from GCS.

    The UI loads /api/satellite-free/cache/<hash>.png. Cloud Run can run with
    several instances, so the endpoint cannot rely only on /tmp. This alias makes
    every instance able to serve the same immutable PNG.
    """
    object_path = satellite_cache_object_path(cache_key)
    blob = _bucket().blob(object_path)
    try:
        return blob.download_as_bytes()
    except GoogleCloudNotFound as exc:
        raise FileNotFoundError(object_path) from exc


def download_satellite_cache_metadata(cache_key: str) -> dict[str, Any]:
    """Read metadata for a generated immutable Sentinel-2 PNG from GCS."""
    return _download_json_blob(satellite_cache_metadata_object_path(cache_key))


def upload_satellite_cache_metadata(cache_key: str, metadata: dict[str, Any]) -> None:
    """Persist metadata next to the immutable PNG cache alias.

    Metadata is intentionally revalidatable instead of immutable so older
    deployments can backfill extra fields without changing the PNG URL.
    """
    _upload_json_blob(satellite_cache_metadata_object_path(cache_key), metadata)


def download_satellite_latest_manifest(manifest_key: str) -> dict[str, Any]:
    """Read the latest generated image pointer for one lot/layer/render size."""
    return _download_json_blob(satellite_latest_manifest_object_path(manifest_key))


def upload_satellite_latest_manifest(manifest_key: str, metadata: dict[str, Any]) -> None:
    """Persist the latest generated image pointer for fast login/re-entry loads."""
    _upload_json_blob(satellite_latest_manifest_object_path(manifest_key), metadata)


def download_satellite_date_manifest(manifest_key: str) -> dict[str, Any]:
    """Read the best generated raster pointer for one immutable Sentinel date."""
    return _download_json_blob(satellite_date_manifest_object_path(manifest_key))


def upload_satellite_date_manifest(manifest_key: str, metadata: dict[str, Any]) -> None:
    """Persist the largest generated raster pointer for one immutable date."""
    _upload_json_blob(satellite_date_manifest_object_path(manifest_key), metadata)


def download_satellite_catalog_manifest(manifest_key: str) -> dict[str, Any]:
    """Read the server-side available-date cache for one parcel geometry."""
    return _download_json_blob(satellite_catalog_manifest_object_path(manifest_key))


def upload_satellite_catalog_manifest(manifest_key: str, metadata: dict[str, Any]) -> None:
    """Persist available Sentinel-2 dates so clearing browser data is inexpensive."""
    _upload_json_blob(satellite_catalog_manifest_object_path(manifest_key), metadata)


def upload_satellite_tif(
    raster: np.ndarray,
    meta: dict,
    user_id: str,
    parcel_id: str,
    index_type: str,
    image_date: str,
) -> str:
    bucket = _bucket()

    object_path = (
        f"satellite/{user_id}/"
        f"{parcel_id}/"
        f"{index_type}/"
        f"{image_date}.tif"
    )

    fd, tmp_path = tempfile.mkstemp(suffix=".tif")
    os.close(fd)

    try:
        with rasterio.open(tmp_path, "w", **meta) as dst:
            dst.write(raster, 1)

        bucket.blob(object_path).upload_from_filename(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return object_path


def upload_satellite_png_bytes(
    content: bytes,
    user_id: str,
    parcel_id: str,
    index_type: str,
    image_date: str,
    cache_key: str | None = None,
) -> str:
    bucket = _bucket()
    object_path = (
        f"satellite/{user_id}/"
        f"{parcel_id}/"
        f"{index_type}/"
        f"{image_date}.png"
    )
    blob = bucket.blob(object_path)
    blob.cache_control = "public, max-age=31536000, immutable"
    blob.upload_from_string(content, content_type="image/png")

    # Also store by the hash exposed by /api/satellite-free/cache/<hash>.png.
    # Without this, a PNG generated in one Cloud Run instance may not be found
    # by another instance when the browser loads the image overlay.
    if cache_key:
        alias_blob = bucket.blob(satellite_cache_object_path(cache_key))
        alias_blob.cache_control = "public, max-age=31536000, immutable"
        alias_blob.upload_from_string(content, content_type="image/png")

    return object_path


def generate_signed_satellite_url(object_path: str) -> str:
    bucket = _bucket()
    blob = bucket.blob(object_path)

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=1),
        method="GET",
    )


def delete_satellite_images_for_parcel(*, user_id: str, parcel_id: str) -> int:
    bucket = _bucket()
    prefix = f"satellite/{user_id}/{parcel_id}/"

    deleted = 0
    for blob in bucket.list_blobs(prefix=prefix):
        blob.delete()
        deleted += 1

    return deleted


def delete_satellite_object(object_path: str) -> None:
    bucket = _bucket()
    bucket.blob(object_path).delete()
