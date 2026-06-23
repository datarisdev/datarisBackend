from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from typing import Any

import numpy as np
import rasterio

from app.utils.azure_blob import (
    delete_blob,
    delete_blobs_with_prefix,
    download_blob_bytes,
    generate_blob_read_url,
    satellite_container_name,
    upload_blob_bytes,
    upload_blob_file,
)


def _container_name() -> str:
    """Resolve the Azure Blob container dedicated to persistent Sentinel-2 data."""
    return satellite_container_name()


def safe_satellite_cache_key(cache_key: str) -> str:
    """Keep deterministic hash-based cache aliases safe for local disk and Azure Blob."""
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
    raw = download_blob_bytes(container_name=_container_name(), object_path=object_path)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid JSON object stored at {object_path}")
    return payload


def _upload_json_blob(object_path: str, payload: dict[str, Any], *, cache_control: str = "no-cache") -> None:
    upload_blob_bytes(
        container_name=_container_name(),
        object_path=object_path,
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        content_type="application/json; charset=utf-8",
        cache_control=cache_control,
    )


def download_satellite_cache_png_bytes(cache_key: str) -> bytes:
    """Read a persistent Sentinel-2 PNG alias from private Azure Blob Storage."""
    return download_blob_bytes(
        container_name=_container_name(),
        object_path=satellite_cache_object_path(cache_key),
    )


def download_satellite_cache_metadata(cache_key: str) -> dict[str, Any]:
    """Read metadata for a generated immutable Sentinel-2 PNG from Azure Blob."""
    return _download_json_blob(satellite_cache_metadata_object_path(cache_key))


def upload_satellite_cache_metadata(cache_key: str, metadata: dict[str, Any]) -> None:
    """Persist metadata next to the immutable PNG cache alias."""
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
    object_path = f"satellite/{user_id}/{parcel_id}/{index_type}/{image_date}.tif"

    fd, tmp_path = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        with rasterio.open(tmp_path, "w", **meta) as dst:
            dst.write(raster, 1)
        upload_blob_file(
            container_name=_container_name(),
            object_path=object_path,
            file_path=tmp_path,
            content_type="image/tiff",
            cache_control="private, max-age=31536000, immutable",
        )
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
    object_path = f"satellite/{user_id}/{parcel_id}/{index_type}/{image_date}.png"
    immutable_cache = "private, max-age=31536000, immutable"
    container_name = _container_name()

    upload_blob_bytes(
        container_name=container_name,
        object_path=object_path,
        content=content,
        content_type="image/png",
        cache_control=immutable_cache,
    )

    # Alias exposed through /api/satellite-free/cache/<hash>.png. It lets every
    # Container Apps replica serve an immutable rendered PNG after scale-out or
    # instance replacement.
    if cache_key:
        upload_blob_bytes(
            container_name=container_name,
            object_path=satellite_cache_object_path(cache_key),
            content=content,
            content_type="image/png",
            cache_control=immutable_cache,
        )

    return object_path


def generate_signed_satellite_url(object_path: str) -> str:
    """Create a short-lived read URL for a private Sentinel Blob object."""
    return generate_blob_read_url(
        container_name=_container_name(),
        object_path=object_path,
        expires_in=timedelta(hours=1),
    )


def delete_satellite_images_for_parcel(*, user_id: str, parcel_id: str) -> int:
    prefix = f"satellite/{user_id}/{parcel_id}/"
    return delete_blobs_with_prefix(container_name=_container_name(), prefix=prefix)


def delete_satellite_object(object_path: str) -> None:
    delete_blob(container_name=_container_name(), object_path=object_path)
