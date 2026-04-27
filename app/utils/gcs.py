from __future__ import annotations

import json
import os
from datetime import timedelta
from functools import lru_cache
from typing import Any

from google.auth.exceptions import DefaultCredentialsError
from google.cloud import storage
from google.oauth2 import service_account

from app.core.config import settings


def _load_service_account_info() -> dict[str, Any] | None:
    raw = (settings.GCS_SERVICE_ACCOUNT_JSON or os.getenv("GCS_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None

    # Allows storing the JSON directly in Vercel env vars.
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        # Allows storing escaped newlines or base64-like copied text less perfectly.
        info = json.loads(raw.replace("\n", "\\n"))

    if isinstance(info.get("private_key"), str):
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    return info


@lru_cache(maxsize=1)
def get_storage_client() -> storage.Client | None:
    """Create the GCS client lazily so the app never crashes during import.

    In local/dev without Google credentials, routes that do not use GCS continue working.
    In production, set either GCS_SERVICE_ACCOUNT_JSON or normal ADC/GOOGLE_APPLICATION_CREDENTIALS.
    """
    info = _load_service_account_info()
    try:
        if info:
            credentials = service_account.Credentials.from_service_account_info(info)
            project = settings.GOOGLE_CLOUD_PROJECT or info.get("project_id")
            return storage.Client(project=project, credentials=credentials)
        return storage.Client(project=settings.GOOGLE_CLOUD_PROJECT or None)
    except DefaultCredentialsError:
        return None


def _bucket() -> storage.Bucket:
    client = get_storage_client()
    if client is None:
        raise RuntimeError(
            "Google Cloud Storage credentials are not configured. "
            "Set GCS_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS in the deployment environment."
        )
    return client.bucket(settings.GCS_BUCKET_NAME)


def upload_avatar(file, content_type: str, user_id: str) -> str:
    bucket = _bucket()
    ext = os.path.splitext(file.filename or "")[1].lower()
    filename = f"avatars/{user_id}/avatar{ext or '.bin'}"

    blob = bucket.blob(filename)
    blob.upload_from_file(file.file, content_type=content_type, rewind=True)

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(days=1500),
        method="GET",
    )


def delete_user_avatars(user_id: str) -> None:
    bucket = _bucket()
    prefix = f"avatars/{user_id}/"
    for blob in bucket.list_blobs(prefix=prefix):
        blob.delete()


def generate_avatar_url(object_path: str) -> str:
    bucket = _bucket()
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=1),
        method="GET",
    )
