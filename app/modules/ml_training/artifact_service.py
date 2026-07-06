"""Persistencia de artefactos de modelos entrenados.

Los binarios viven exclusivamente en Azure Blob Storage; PostgreSQL solo
guarda la referencia `azureblob://...` (mismo patrón que
app/utils/azure_blob.py::azure_blob_reference), nunca el contenido.
"""
from __future__ import annotations

import uuid
from sqlalchemy.orm import Session

from app.models.ml_training import ModelArtifact, ModelArtifactType, ModelVersion, ModelVersionStatus, TrainingTaskType
from app.modules.ml_training.upload_service import create_read_url, model_artifact_prefix


# Artefactos esperados al finalizar un entrenamiento exitoso (manifest.json
# generado por create_manifest.py en la imagen de entrenamiento declara
# cuáles se produjeron realmente; esto es solo el catálogo de tipos válidos).
EXPECTED_ARTIFACT_TYPES = (
    ModelArtifactType.WEIGHTS_BEST,
    ModelArtifactType.WEIGHTS_LAST,
    ModelArtifactType.METRICS,
    ModelArtifactType.CONFIG,
    ModelArtifactType.DATA_YAML,
    ModelArtifactType.MANIFEST,
)


def register_model_version(
    db: Session,
    *,
    user_id: str,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    dataset_id: uuid.UUID | None,
    name: str,
    version: str,
    task_type: TrainingTaskType,
    model_base: str,
    recipe: str,
    metrics: dict | None,
    docker_image_ref: str | None,
) -> ModelVersion:
    model_version = ModelVersion(
        user_id=user_id,
        project_id=project_id,
        job_id=job_id,
        dataset_id=dataset_id,
        name=name,
        version=version,
        task_type=task_type,
        model_base=model_base,
        recipe=recipe,
        metrics=metrics,
        storage_prefix=model_artifact_prefix(user_id, str(uuid.uuid4())),
        status=ModelVersionStatus.ACTIVE,
        docker_image_ref=docker_image_ref,
    )
    db.add(model_version)
    db.flush()
    return model_version


def register_artifact(
    db: Session,
    *,
    model_version: ModelVersion,
    artifact_type: ModelArtifactType,
    blob_path: str,
    size_bytes: int,
) -> ModelArtifact:
    artifact = ModelArtifact(
        model_version_id=model_version.id,
        artifact_type=artifact_type,
        blob_path=blob_path,
        size_bytes=size_bytes,
    )
    db.add(artifact)
    return artifact


def build_download_url(artifact: ModelArtifact, ttl_hours: int = 1) -> tuple[str, "object"]:
    url, expires_at = create_read_url(artifact.blob_path, ttl_hours=ttl_hours)
    return url, expires_at
