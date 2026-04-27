"""
Cliente lazy/opcional de Google Cloud Storage para Vercel.

Problema que corrige:
- Evita ejecutar `storage.Client()` al momento de importar módulos.
- Si no hay credenciales de Google Cloud en Vercel, no rompe el arranque del backend.
- Permite seguir usando almacenamiento local temporal con DATARIS_COMPAT_STORAGE_DIR=/tmp/dataris-storage.

Uso recomendado:
    from app.core.gcs_client import get_gcs_client, get_gcs_bucket

    client = get_gcs_client()
    if client is None:
        # usar fallback local o responder error controlado
        ...

Variables opcionales en Vercel:
    GOOGLE_CLOUD_PROJECT=tu-project-id
    GOOGLE_APPLICATION_CREDENTIALS_JSON={"type":"service_account",...}
    GCS_STRICT=false
    DISABLE_GCS=true
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from google.cloud import storage
from google.oauth2 import service_account


_TRUE_VALUES = {"1", "true", "yes", "y", "on"}


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def get_local_storage_dir() -> Path:
    """
    Directorio local para fallback.

    En Vercel, solo /tmp es escribible y no es persistente.
    Para demo funciona. Para producción usa GCS/S3/Supabase Storage.
    """
    raw_dir = os.getenv("DATARIS_COMPAT_STORAGE_DIR", "/tmp/dataris-storage")
    path = Path(raw_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


@lru_cache(maxsize=1)
def get_gcs_client() -> Optional[storage.Client]:
    """
    Retorna un cliente de Google Cloud Storage solo si está configurado.

    Importante:
    - No lanza error si no hay credenciales, salvo que GCS_STRICT=true.
    - No debe llamarse a nivel global en otros archivos si quieres evitar errores de arranque.
    """
    if _env_true("DISABLE_GCS"):
        return None

    credentials_json = (
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        or os.getenv("GCP_SERVICE_ACCOUNT_JSON")
        or os.getenv("GOOGLE_CREDENTIALS_JSON")
    )

    project_id = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCP_PROJECT")
        or os.getenv("GOOGLE_PROJECT_ID")
    )

    try:
        if credentials_json:
            info = json.loads(credentials_json)
            credentials = service_account.Credentials.from_service_account_info(info)
            return storage.Client(
                credentials=credentials,
                project=project_id or info.get("project_id"),
            )

        # En servidores con Application Default Credentials o GOOGLE_CLOUD_PROJECT.
        if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or project_id:
            return storage.Client(project=project_id)

        return None

    except Exception:
        if _env_true("GCS_STRICT"):
            raise
        return None


def is_gcs_configured() -> bool:
    return get_gcs_client() is not None


def get_gcs_bucket(bucket_name: Optional[str] = None) -> Optional[storage.Bucket]:
    """
    Retorna un bucket si GCS está configurado; si no, retorna None.
    """
    client = get_gcs_client()
    if client is None:
        return None

    final_bucket_name = (
        bucket_name
        or os.getenv("GOOGLE_CLOUD_STORAGE_BUCKET")
        or os.getenv("GCS_BUCKET")
        or os.getenv("DATARIS_GCS_BUCKET")
    )

    if not final_bucket_name:
        if _env_true("GCS_STRICT"):
            raise RuntimeError(
                "Falta GOOGLE_CLOUD_STORAGE_BUCKET, GCS_BUCKET o DATARIS_GCS_BUCKET."
            )
        return None

    return client.bucket(final_bucket_name)
