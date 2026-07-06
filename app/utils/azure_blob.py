"""Azure Blob Storage utilities for Dataris.

This module centralizes all persistent object storage used by the API:
- Sentinel-2 PNG/TIFF cache and manifests.
- Parcel source files.
- User avatars.

Authentication is passwordless in Azure Container Apps through the configured
user-assigned managed identity. Local development uses ``DefaultAzureCredential``
(Azure CLI, VS Code sign-in, workload identity, etc.). No storage account key or
connection string is required.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import BinaryIO, Iterable
from urllib.parse import urlparse


_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_AZURE_SDK_IMPORT_ERROR: Exception | None = None

try:  # Keep import-time failures explicit when the dependency was not installed.
    from azure.core.exceptions import AzureError, ResourceNotFoundError
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    from azure.storage.blob import (
        BlobSasPermissions,
        BlobServiceClient,
        ContentSettings,
        generate_blob_sas,
    )
except Exception as exc:  # pragma: no cover - exercised only without dependencies.
    _AZURE_SDK_IMPORT_ERROR = exc
    AzureError = Exception  # type: ignore[misc,assignment]
    ResourceNotFoundError = Exception  # type: ignore[misc,assignment]
    DefaultAzureCredential = None  # type: ignore[assignment]
    ManagedIdentityCredential = None  # type: ignore[assignment]
    BlobSasPermissions = None  # type: ignore[assignment]
    BlobServiceClient = None  # type: ignore[assignment]
    ContentSettings = None  # type: ignore[assignment]
    generate_blob_sas = None  # type: ignore[assignment]


class AzureBlobStorageError(RuntimeError):
    """Raised when Azure Blob Storage is required but unavailable or misconfigured."""


_DELEGATION_KEY_LOCK = threading.RLock()
_DELEGATION_KEY = None
_DELEGATION_KEY_EXPIRES_AT: datetime | None = None


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def azure_blob_storage_disabled() -> bool:
    return _env_true("DISABLE_AZURE_BLOB_STORAGE", default=False)


def azure_blob_storage_strict() -> bool:
    return _env_true("AZURE_BLOB_STORAGE_STRICT", default=False)


def _require_azure_sdk() -> None:
    if _AZURE_SDK_IMPORT_ERROR is not None:
        raise AzureBlobStorageError(
            "Azure Blob Storage dependencies are missing. Install azure-identity and azure-storage-blob."
        ) from _AZURE_SDK_IMPORT_ERROR


def _account_url() -> str:
    explicit_url = (os.getenv("AZURE_STORAGE_ACCOUNT_URL") or "").strip().rstrip("/")
    if explicit_url:
        return explicit_url

    account_name = (
        os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
        or os.getenv("DATARIS_AZURE_STORAGE_ACCOUNT")
        or ""
    ).strip()
    if account_name:
        return f"https://{account_name}.blob.core.windows.net"

    raise AzureBlobStorageError(
        "Azure Blob Storage is not configured. Set AZURE_STORAGE_ACCOUNT_URL "
        "or AZURE_STORAGE_ACCOUNT_NAME."
    )


def _account_name() -> str:
    explicit_name = (
        os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
        or os.getenv("DATARIS_AZURE_STORAGE_ACCOUNT")
        or ""
    ).strip()
    if explicit_name:
        return explicit_name

    hostname = urlparse(_account_url()).hostname or ""
    account_name = hostname.split(".", 1)[0]
    if not account_name:
        raise AzureBlobStorageError("Could not derive the Azure Storage account name.")
    return account_name


def _is_azure_hosted_runtime() -> bool:
    """Detect Azure-hosted runtimes without breaking local CLI authentication."""
    return any(
        os.getenv(name)
        for name in (
            "IDENTITY_ENDPOINT",
            "MSI_ENDPOINT",
            "CONTAINER_APP_NAME",
            "WEBSITE_SITE_NAME",
            "WEBSITE_INSTANCE_ID",
        )
    )


@lru_cache(maxsize=1)
def get_token_credential():
    """Return the right Azure Identity credential for Azure or local development."""
    _require_azure_sdk()

    client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip() or None
    if _is_azure_hosted_runtime():
        if client_id:
            return ManagedIdentityCredential(client_id=client_id)
        return ManagedIdentityCredential()

    # Locally DefaultAzureCredential supports Azure CLI, VS Code and other safe
    # credential chains. Keeping interactive browser excluded avoids a server
    # process hanging while a browser credential is attempted.
    kwargs: dict[str, object] = {"exclude_interactive_browser_credential": True}
    if client_id:
        kwargs["managed_identity_client_id"] = client_id
    return DefaultAzureCredential(**kwargs)


@lru_cache(maxsize=1)
def get_blob_service_client():
    """Create the Blob service client lazily; never authenticate at module import."""
    if azure_blob_storage_disabled():
        return None

    _require_azure_sdk()
    return BlobServiceClient(account_url=_account_url(), credential=get_token_credential())


def reset_azure_blob_clients() -> None:
    """Clear cached SDK clients. Intended for tests and controlled local reloads."""
    global _DELEGATION_KEY, _DELEGATION_KEY_EXPIRES_AT
    get_token_credential.cache_clear()
    get_blob_service_client.cache_clear()
    with _DELEGATION_KEY_LOCK:
        _DELEGATION_KEY = None
        _DELEGATION_KEY_EXPIRES_AT = None


def _required_service_client():
    client = get_blob_service_client()
    if client is None:
        raise AzureBlobStorageError(
            "Azure Blob Storage is disabled by DISABLE_AZURE_BLOB_STORAGE=true."
        )
    return client


def app_assets_container_name() -> str:
    value = (os.getenv("AZURE_STORAGE_CONTAINER_NAME") or "").strip()
    if not value:
        raise AzureBlobStorageError("AZURE_STORAGE_CONTAINER_NAME is not configured.")
    return value


def parcels_container_name() -> str:
    return (os.getenv("AZURE_PARCELS_STORAGE_CONTAINER") or app_assets_container_name()).strip()


def satellite_container_name() -> str:
    return (os.getenv("AZURE_SATELLITE_STORAGE_CONTAINER") or app_assets_container_name()).strip()


def avatars_container_name() -> str:
    return (os.getenv("AZURE_AVATARS_STORAGE_CONTAINER") or app_assets_container_name()).strip()


def ml_training_container_name() -> str:
    return (os.getenv("AZURE_ML_TRAINING_STORAGE_CONTAINER") or app_assets_container_name()).strip()


def get_container_client(container_name: str):
    if not container_name:
        raise AzureBlobStorageError("Azure Blob container name cannot be empty.")
    return _required_service_client().get_container_client(container_name)


def upload_blob_bytes(
    *,
    container_name: str,
    object_path: str,
    content: bytes,
    content_type: str | None = None,
    cache_control: str | None = None,
) -> None:
    """Upload bytes and atomically overwrite the object path when it exists."""
    settings = None
    if content_type or cache_control:
        settings = ContentSettings(content_type=content_type, cache_control=cache_control)

    blob = get_container_client(container_name).get_blob_client(object_path)
    blob.upload_blob(content, overwrite=True, content_settings=settings)


def upload_blob_stream(
    *,
    container_name: str,
    object_path: str,
    stream: BinaryIO,
    content_type: str | None = None,
    cache_control: str | None = None,
) -> None:
    settings = None
    if content_type or cache_control:
        settings = ContentSettings(content_type=content_type, cache_control=cache_control)

    try:
        stream.seek(0)
    except Exception:
        pass

    blob = get_container_client(container_name).get_blob_client(object_path)
    blob.upload_blob(stream, overwrite=True, content_settings=settings)


def upload_blob_file(
    *,
    container_name: str,
    object_path: str,
    file_path: str,
    content_type: str | None = None,
    cache_control: str | None = None,
) -> None:
    with open(file_path, "rb") as handle:
        upload_blob_stream(
            container_name=container_name,
            object_path=object_path,
            stream=handle,
            content_type=content_type,
            cache_control=cache_control,
        )


def download_blob_bytes(*, container_name: str, object_path: str) -> bytes:
    """Download bytes, converting an expected missing blob into FileNotFoundError."""
    blob = get_container_client(container_name).get_blob_client(object_path)
    try:
        return blob.download_blob().readall()
    except ResourceNotFoundError as exc:
        raise FileNotFoundError(object_path) from exc


def delete_blob(*, container_name: str, object_path: str) -> bool:
    blob = get_container_client(container_name).get_blob_client(object_path)
    try:
        blob.delete_blob(delete_snapshots="include")
        return True
    except ResourceNotFoundError:
        return False


def delete_blobs_with_prefix(*, container_name: str, prefix: str) -> int:
    container = get_container_client(container_name)
    deleted = 0
    for item in container.list_blobs(name_starts_with=prefix):
        try:
            container.delete_blob(item.name, delete_snapshots="include")
            deleted += 1
        except ResourceNotFoundError:
            continue
    return deleted


def list_blob_names(*, container_name: str, prefix: str) -> Iterable[str]:
    container = get_container_client(container_name)
    for item in container.list_blobs(name_starts_with=prefix):
        yield item.name


def _delegation_key_for(expiry: datetime):
    """Get and cache a user delegation key for short-lived read SAS URLs."""
    global _DELEGATION_KEY, _DELEGATION_KEY_EXPIRES_AT

    now = datetime.now(timezone.utc)
    with _DELEGATION_KEY_LOCK:
        if _DELEGATION_KEY is not None and _DELEGATION_KEY_EXPIRES_AT:
            if _DELEGATION_KEY_EXPIRES_AT > now + timedelta(minutes=15):
                return _DELEGATION_KEY

        service = _required_service_client()
        key_start = now - timedelta(minutes=5)
        # User delegation keys support a maximum seven-day validity. Keep this
        # below that limit and longer than the generated read SAS itself.
        key_expiry = max(expiry + timedelta(hours=1), now + timedelta(hours=24))
        key_expiry = min(key_expiry, now + timedelta(days=6, hours=23))
        _DELEGATION_KEY = service.get_user_delegation_key(key_start, key_expiry)
        _DELEGATION_KEY_EXPIRES_AT = key_expiry
        return _DELEGATION_KEY


def generate_blob_read_url(
    *,
    container_name: str,
    object_path: str,
    expires_in: timedelta | None = None,
) -> str:
    """Return a short-lived read URL for one private Azure Blob.

    The application stores object paths, not SAS URLs. This avoids expired
    tokens in PostgreSQL while keeping the Blob container private.
    """
    _require_azure_sdk()
    now = datetime.now(timezone.utc)
    ttl_hours = int((os.getenv("AZURE_BLOB_READ_SAS_TTL_HOURS") or "1").strip() or "1")
    ttl_hours = max(1, min(ttl_hours, 24))
    expiry = now + (expires_in or timedelta(hours=ttl_hours))
    delegation_key = _delegation_key_for(expiry)
    sas = generate_blob_sas(
        account_name=_account_name(),
        container_name=container_name,
        blob_name=object_path,
        user_delegation_key=delegation_key,
        permission=BlobSasPermissions(read=True),
        start=now - timedelta(minutes=5),
        expiry=expiry,
    )
    blob_url = get_container_client(container_name).get_blob_client(object_path).url
    return f"{blob_url}?{sas}"


def generate_blob_write_url(
    *,
    container_name: str,
    object_path: str,
    expires_in: timedelta | None = None,
    max_ttl_minutes: int = 30,
) -> str:
    """Return a short-lived write-only URL for one private Azure Blob.

    Used exclusively by the ml_training module upload-intent flow so large
    dataset ZIPs go straight to Blob Storage without transiting the FastAPI
    process. Deliberately capped much shorter than the read SAS TTL and
    restricted to create+write (no read/delete) to limit blast radius if the
    URL leaks (e.g. browser history, proxy logs).
    """
    _require_azure_sdk()
    now = datetime.now(timezone.utc)
    ttl_minutes = max(1, min(max_ttl_minutes, 30))
    expiry = now + (expires_in or timedelta(minutes=ttl_minutes))
    delegation_key = _delegation_key_for(expiry)
    sas = generate_blob_sas(
        account_name=_account_name(),
        container_name=container_name,
        blob_name=object_path,
        user_delegation_key=delegation_key,
        permission=BlobSasPermissions(write=True, create=True),
        start=now - timedelta(minutes=5),
        expiry=expiry,
    )
    blob_url = get_container_client(container_name).get_blob_client(object_path).url
    return f"{blob_url}?{sas}"


def is_azure_blob_reference(value: str | None) -> bool:
    return bool(value and value.startswith("azureblob://"))


def azure_blob_reference(object_path: str) -> str:
    return f"azureblob://{object_path.lstrip('/')}"


def object_path_from_reference(value: str) -> str:
    if not is_azure_blob_reference(value):
        raise ValueError("Not an Azure Blob reference")
    object_path = value.split("://", 1)[1].lstrip("/")
    if not object_path:
        raise ValueError("Azure Blob reference has no object path")
    return object_path
