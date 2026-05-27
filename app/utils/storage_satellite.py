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
) -> str:
    bucket = _bucket()
    object_path = (
        f"satellite/{user_id}/"
        f"{parcel_id}/"
        f"{index_type}/"
        f"{image_date}.png"
    )
    blob = bucket.blob(object_path)
    blob.upload_from_string(content, content_type="image/png")
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
