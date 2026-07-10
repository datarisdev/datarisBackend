"""Generación de rutas de almacenamiento y URLs de carga/descarga seguras
para el módulo de entrenamiento.

Convención de rutas dentro del container `ml-training` (o el fallback
`app_assets` si AZURE_ML_TRAINING_STORAGE_CONTAINER no está configurado):

    ml/datasets/{user_id}/{dataset_id}/raw/{file_name}
    ml/datasets/{user_id}/{dataset_id}/extracted/...
    ml/jobs/{user_id}/{project_id}/{job_id}/
    ml/models/{user_id}/{model_id}/
    ml/inference/{user_id}/{job_id}/input/{file_name}
    ml/inference/{user_id}/{job_id}/output/

Aislada por user_id (el único límite de aislamiento real que existe hoy en
Dataris — ver Contexto del plan aprobado).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from app.utils.azure_blob import (
    generate_blob_read_url,
    generate_blob_write_url,
    ml_training_container_name,
)

MAX_DATASET_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB, alineado con ml_max_dataset_size_gb

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_file_name(file_name: str) -> str:
    """Nombre de archivo seguro para blob path y para pasar como argumento de
    proceso (ver training_recipes.py::build_predict_command) — nunca texto
    libre sin filtrar."""
    name = file_name.replace("/", "_").replace("\\", "_")
    return _UNSAFE_FILENAME_CHARS.sub("_", name) or "archivo"


def dataset_raw_blob_path(user_id: str, dataset_id: str, file_name: str) -> str:
    safe_name = file_name.replace("/", "_").replace("\\", "_")
    return f"ml/datasets/{user_id}/{dataset_id}/raw/{safe_name}"


def dataset_extracted_prefix(user_id: str, dataset_id: str) -> str:
    return f"ml/datasets/{user_id}/{dataset_id}/extracted/"


def job_output_prefix(user_id: str, project_id: str, job_id: str) -> str:
    return f"ml/jobs/{user_id}/{project_id}/{job_id}/"


def model_artifact_prefix(user_id: str, model_id: str) -> str:
    return f"ml/models/{user_id}/{model_id}/"


def inference_input_prefix(user_id: str, job_id: str) -> str:
    return f"ml/inference/{user_id}/{job_id}/input/"


def inference_input_blob_path(user_id: str, job_id: str, file_name: str) -> str:
    return f"{inference_input_prefix(user_id, job_id)}{_sanitize_file_name(file_name)}"


def inference_output_prefix(user_id: str, job_id: str) -> str:
    return f"ml/inference/{user_id}/{job_id}/output/"


def create_dataset_upload_intent(*, user_id: str, dataset_id: str, file_name: str, size_bytes: int) -> dict:
    if size_bytes > MAX_DATASET_UPLOAD_BYTES:
        raise ValueError(
            f"El archivo excede el tamaño máximo permitido ({MAX_DATASET_UPLOAD_BYTES // (1024 ** 3)} GB)"
        )

    blob_path = dataset_raw_blob_path(user_id, dataset_id, file_name)
    ttl_minutes = 30
    upload_url = generate_blob_write_url(
        container_name=ml_training_container_name(),
        object_path=blob_path,
        expires_in=timedelta(minutes=ttl_minutes),
    )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    return {
        "blob_path": blob_path,
        "upload_url": upload_url,
        "expires_at": expires_at,
        "max_size_bytes": MAX_DATASET_UPLOAD_BYTES,
    }


def create_read_url(blob_path: str, ttl_hours: int = 1) -> tuple[str, datetime]:
    url = generate_blob_read_url(
        container_name=ml_training_container_name(),
        object_path=blob_path,
        expires_in=timedelta(hours=ttl_hours),
    )
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    return url, expires_at


def new_id() -> uuid.UUID:
    return uuid.uuid4()
