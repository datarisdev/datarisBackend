from __future__ import annotations

from datetime import datetime
from typing import BinaryIO, List, Tuple

from app.utils.azure_blob import (
    compat_container_name,
    delete_blob,
    download_blob_bytes,
    get_container_client,
    upload_blob_stream,
)


def _object_path(bucket: str, path: str) -> str:
    return f"compat/{bucket}/{path}"


def upload_compat_object(bucket: str, path: str, stream: BinaryIO, content_type: str | None) -> None:
    upload_blob_stream(
        container_name=compat_container_name(),
        object_path=_object_path(bucket, path),
        stream=stream,
        content_type=content_type or "application/octet-stream",
    )


def read_compat_object(bucket: str, path: str) -> bytes:
    return download_blob_bytes(
        container_name=compat_container_name(),
        object_path=_object_path(bucket, path),
    )


def delete_compat_objects(bucket: str, paths: List[str]) -> List[str]:
    removed = []
    for raw in paths:
        if delete_blob(container_name=compat_container_name(), object_path=_object_path(bucket, raw)):
            removed.append(raw)
    return removed


def list_compat_objects(bucket: str, prefix: str) -> List[Tuple[str, datetime]]:
    container = get_container_client(compat_container_name())
    base_prefix = _object_path(bucket, prefix)
    items = []
    for item in container.list_blobs(name_starts_with=base_prefix):
        name = item.name.rsplit("/", 1)[-1]
        items.append((name, item.last_modified))
    return items
