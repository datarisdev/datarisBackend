from __future__ import annotations

import os
from datetime import timedelta

from app.utils.azure_blob import (
    azure_blob_reference,
    delete_blobs_with_prefix,
    generate_blob_read_url,
    parcels_container_name,
    upload_blob_stream,
)


def _container_name() -> str:
    return parcels_container_name()


def upload_parcel_file(file, content_type: str, user_id: str, parcel_id: str) -> str:
    """Upload a parcel source file to the private Azure Blob container.

    The database should store the returned ``azureblob://`` reference, not a
    short-lived SAS URL. Resolve it at the HTTP boundary when a browser needs a
    temporary download URL.
    """
    ext = os.path.splitext(file.filename or "")[1].lower() or ".bin"
    object_path = f"parcels/{user_id}/{parcel_id}/original{ext}"
    upload_blob_stream(
        container_name=_container_name(),
        object_path=object_path,
        stream=file.file,
        content_type=content_type or "application/octet-stream",
        cache_control="private, max-age=0, no-store",
    )
    return azure_blob_reference(object_path)


def generate_parcel_file_url(object_path: str) -> str:
    return generate_blob_read_url(
        container_name=_container_name(),
        object_path=object_path,
        expires_in=timedelta(hours=1),
    )


def delete_parcel_files(user_id: str, parcel_id: str) -> int:
    return delete_blobs_with_prefix(
        container_name=_container_name(),
        prefix=f"parcels/{user_id}/{parcel_id}/",
    )


def delete_all_user_parcels(user_id: str) -> int:
    return delete_blobs_with_prefix(
        container_name=_container_name(),
        prefix=f"parcels/{user_id}/",
    )
