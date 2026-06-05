from __future__ import annotations

import os
import tempfile
from datetime import timedelta

import numpy as np
import rasterio

from app.utils.gcs import get_storage_client

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
    """Keep cache aliases deterministic and safe for both local disk and GCS."""
    return "".join(ch for ch in str(cache_key).lower() if ch in "0123456789abcdef")[:80]


def satellite_cache_object_path(cache_key: str) -> str:
    safe_key = safe_satellite_cache_key(cache_key)
    if not safe_key:
        raise ValueError("Invalid Sentinel-2 cache key")
    return f"cache/{safe_key}.png"


def download_satellite_cache_png_bytes(cache_key: str) -> bytes:
    """Read the public Sentinel-2 cache alias from GCS.

    The UI loads /api/satellite-free/cache/<hash>.png. Cloud Run can run with
    several instances, so the endpoint cannot rely only on /tmp. This alias makes
    every instance able to serve the same PNG.
    """
    blob = _bucket().blob(satellite_cache_object_path(cache_key))
    if not blob.exists():
        raise FileNotFoundError(satellite_cache_object_path(cache_key))
    return blob.download_as_bytes()


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
