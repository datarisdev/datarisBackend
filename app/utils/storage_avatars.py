from __future__ import annotations

import os
from datetime import timedelta

from app.utils.azure_blob import (
    avatars_container_name,
    azure_blob_reference,
    delete_blobs_with_prefix,
    generate_blob_read_url,
    is_azure_blob_reference,
    object_path_from_reference,
    upload_blob_stream,
)


def _container_name() -> str:
    return avatars_container_name()


def upload_avatar(file, content_type: str | None, user_id: str) -> str:
    """Store one private avatar and return a durable Azure Blob object reference."""
    ext = os.path.splitext(file.filename or "")[1].lower() or ".bin"
    object_path = f"avatars/{user_id}/avatar{ext}"
    upload_blob_stream(
        container_name=_container_name(),
        object_path=object_path,
        stream=file.file,
        content_type=content_type or "application/octet-stream",
        cache_control="private, max-age=0, no-store",
    )
    return azure_blob_reference(object_path)


def delete_user_avatars(user_id: str) -> int:
    return delete_blobs_with_prefix(
        container_name=_container_name(),
        prefix=f"avatars/{user_id}/",
    )


def generate_avatar_url(object_path: str) -> str:
    return generate_blob_read_url(
        container_name=_container_name(),
        object_path=object_path,
        expires_in=timedelta(hours=1),
    )


def resolve_avatar_url(value: str | None) -> str | None:
    """Resolve a durable internal reference only when serializing an API response.

    Existing external URLs are left unchanged so historical profile records are
    not corrupted. New uploads always use ``azureblob://`` references.
    """
    if not value:
        return None
    if not is_azure_blob_reference(value):
        return value
    return generate_avatar_url(object_path_from_reference(value))
