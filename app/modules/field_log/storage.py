"""Evidencia fotográfica de la bitácora en Blob Storage.

Las fotos suben directas del navegador al blob con una URL firmada de corta
vida: una captura de campo puede traer varias imágenes de teléfono y no tiene
sentido hacerlas pasar por el proceso de FastAPI.

En la base de datos se guarda la ruta del blob, nunca la URL firmada, para no
almacenar tokens caducados (misma convención que el resto de la plataforma).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.utils.azure_blob import (
    app_assets_container_name,
    generate_blob_read_url,
    generate_blob_write_url,
)

UPLOAD_TTL_MINUTES = 20
READ_TTL_HOURS = 6

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def field_log_container_name() -> str:
    import os

    return (os.getenv("AZURE_FIELD_LOG_STORAGE_CONTAINER") or app_assets_container_name()).strip()


def _sanitize(file_name: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", (file_name or "foto.jpg").strip())
    return cleaned[-120:] or "foto.jpg"


def build_photo_path(*, user_id: UUID, cycle_id: UUID, file_name: str) -> str:
    """Ruta aislada por usuario y ciclo, con marca de tiempo para no colisionar."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    unique = uuid.uuid4().hex[:8]
    return f"field-log/{user_id}/{cycle_id}/{stamp}-{unique}-{_sanitize(file_name)}"


def generate_urls(blob_path: str) -> tuple[str, str]:
    container = field_log_container_name()
    upload_url = generate_blob_write_url(
        container_name=container,
        object_path=blob_path,
        expires_in=timedelta(minutes=UPLOAD_TTL_MINUTES),
    )
    read_url = generate_blob_read_url(
        container_name=container,
        object_path=blob_path,
        expires_in=timedelta(hours=READ_TTL_HOURS),
    )
    return upload_url, read_url


def resolve_read_urls(paths: list[str] | None) -> list[str]:
    """Convierte rutas de blob en URLs de lectura firmadas.

    Tolera fallos por foto: que Blob Storage esté mal configurado no puede
    impedir consultar la bitácora, que es texto y números.
    """
    if not paths:
        return []

    container = field_log_container_name()
    urls: list[str] = []
    for path in paths:
        if not path:
            continue
        if path.startswith("http://") or path.startswith("https://"):
            urls.append(path)
            continue
        try:
            urls.append(
                generate_blob_read_url(
                    container_name=container,
                    object_path=path,
                    expires_in=timedelta(hours=READ_TTL_HOURS),
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return urls
