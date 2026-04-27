from __future__ import annotations

import os
from datetime import timedelta

from app.core.config import settings
from app.utils.gcs import get_storage_client

PARCELS_BUCKET_NAME = os.getenv("GCS_PARCELS_BUCKET_NAME", "dataris-parcels")


def _bucket():
    """Return the parcels bucket lazily.

    Important for Vercel/serverless: never create storage.Client() at import time.
    If credentials are not configured, routes that do not use GCS should still boot.
    """
    client = get_storage_client()
    if client is None:
        raise RuntimeError(
            "Google Cloud Storage credentials are not configured. "
            "Set GCS_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS in Vercel."
        )
    return client.bucket(os.getenv("GCS_PARCELS_BUCKET_NAME") or PARCELS_BUCKET_NAME)


def upload_parcel_file(file, content_type: str, user_id: str, parcel_id: str) -> str:
    """
    Uploads a parcel file (zip/shp/etc) to GCS and returns a signed URL.
    """
    bucket = _bucket()

    ext = os.path.splitext(file.filename or "")[1].lower() or ".bin"
    filename = f"parcels/{user_id}/{parcel_id}/original{ext}"

    blob = bucket.blob(filename)
    blob.upload_from_file(
        file.file,
        content_type=content_type,
        rewind=True,
    )

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(days=7),
        method="GET",
    )


def delete_parcel_files(user_id: str, parcel_id: str) -> None:
    bucket = _bucket()
    prefix = f"parcels/{user_id}/{parcel_id}/"

    for blob in bucket.list_blobs(prefix=prefix):
        blob.delete()


def delete_all_user_parcels(user_id: str) -> None:
    bucket = _bucket()
    prefix = f"parcels/{user_id}/"

    for blob in bucket.list_blobs(prefix=prefix):
        blob.delete()
