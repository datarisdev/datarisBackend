"""Orquestación del módulo de entrenamiento de modelos de visión.

Este servicio es el único punto que combina DB (repository.py), Azure Blob
(upload_service.py), Roboflow (roboflow_service.py), validación de dataset
(dataset_validation.py), recetas (training_recipes.py) y Azure ML
(azure_ml_client.py). Los routers (app/api/routers/ml_training.py) deben
quedar delgados y delegar aquí, siguiendo ARCHITECTURE.md.
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.ml_training import (
    DatasetSource,
    DatasetStatus,
    MLDataset,
    TrainingJob,
    TrainingJobStatus,
    TrainingProject,
    TERMINAL_JOB_STATUSES,
)
from app.modules.ml_training import repository
from app.modules.ml_training.artifact_service import register_model_version
from app.modules.ml_training.azure_ml_client import (
    AzureMLClientError,
    AzureMLJobSpec,
    cancel_job as azure_cancel_job,
    default_compute_target,
    default_docker_image,
    get_job as azure_get_job,
    submit_training_job,
    translate_azure_status,
)
from app.modules.ml_training.dataset_validation import (
    DatasetSecurityError,
    inspect_zip_safety,
    safe_extract_zip,
    validate_yolo_dataset,
)
from app.modules.ml_training.roboflow_service import RoboflowService, RoboflowServiceError
from app.modules.ml_training.schemas import (
    DatasetRoboflowImportRequest,
    DatasetUploadIntentRequest,
    TrainingJobConfig,
    TrainingJobCreate,
    TrainingProjectCreate,
    TrainingProjectUpdate,
)
from app.modules.ml_training.training_recipes import (
    UnknownRecipeError,
    UnsupportedModelBaseError,
    apply_mode_preset,
    build_train_command,
    get_recipe,
    validate_model_base,
)
from app.modules.ml_training.upload_service import (
    create_dataset_upload_intent as build_upload_intent,
    dataset_extracted_prefix,
    job_output_prefix,
    new_id,
)
from app.utils.azure_blob import (
    download_blob_bytes,
    ml_training_container_name,
    upload_blob_stream,
)

logger = logging.getLogger(__name__)


class MLTrainingError(Exception):
    """Error de negocio sanitizado del módulo de entrenamiento (safe para el cliente)."""


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def create_project(db: Session, user_id: str, payload: TrainingProjectCreate) -> TrainingProject:
    project = TrainingProject(
        user_id=user_id,
        name=payload.name,
        description=payload.description,
        task_type=payload.task_type,
        tags=payload.tags,
    )
    db.add(project)
    db.flush()
    repository.record_audit_event(db, user_id=user_id, action="project_created", entity_type="project", entity_id=project.id)
    db.commit()
    db.refresh(project)
    return project


def update_project(db: Session, user_id: str, project_id: uuid.UUID, payload: TrainingProjectUpdate) -> TrainingProject:
    project = repository.get_project(db, user_id, project_id)
    if project is None:
        raise MLTrainingError("Proyecto no encontrado")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return project


# ---------------------------------------------------------------------------
# Datasets — subida manual
# ---------------------------------------------------------------------------

def create_dataset_upload_intent(db: Session, user_id: str, payload: DatasetUploadIntentRequest) -> dict:
    if payload.project_id is not None and repository.get_project(db, user_id, payload.project_id) is None:
        raise MLTrainingError("Proyecto no encontrado")

    dataset_id = new_id()
    intent = build_upload_intent(
        user_id=user_id,
        dataset_id=str(dataset_id),
        file_name=payload.file_name,
        size_bytes=payload.size_bytes,
    )

    dataset = MLDataset(
        id=dataset_id,
        user_id=user_id,
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
        source=DatasetSource.UPLOAD,
        status=DatasetStatus.UPLOADING,
        storage_prefix=intent["blob_path"],
        task_type=payload.task_type,
        size_bytes=payload.size_bytes,
    )
    db.add(dataset)
    repository.record_audit_event(db, user_id=user_id, action="dataset_upload_intent_created", entity_type="dataset", entity_id=dataset_id)
    db.commit()

    return {
        "dataset_id": dataset_id,
        "upload_url": intent["upload_url"],
        "blob_path": intent["blob_path"],
        "expires_at": intent["expires_at"],
        "max_size_bytes": intent["max_size_bytes"],
    }


# ---------------------------------------------------------------------------
# Datasets — importación Roboflow
# ---------------------------------------------------------------------------

def import_dataset_from_roboflow(db: Session, user_id: str, payload: DatasetRoboflowImportRequest) -> MLDataset:
    if payload.project_id is not None and repository.get_project(db, user_id, payload.project_id) is None:
        raise MLTrainingError("Proyecto no encontrado")

    dataset_id = new_id()
    dataset = MLDataset(
        id=dataset_id,
        user_id=user_id,
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
        source=DatasetSource.ROBOFLOW,
        status=DatasetStatus.UPLOADING,
        storage_prefix=f"ml/datasets/{user_id}/{dataset_id}/raw/roboflow_export.zip",
        task_type=payload.task_type,
        roboflow_workspace=payload.workspace,
        roboflow_project=payload.project,
        roboflow_version=payload.version,
        roboflow_format=payload.export_format,
    )
    db.add(dataset)
    db.commit()

    try:
        service = RoboflowService()
        export = service.request_export(payload.workspace, payload.project, payload.version, payload.export_format)
        chunks = service.stream_export(export)
        total_bytes = _upload_chunks_to_blob(chunks, dataset.storage_prefix)
        dataset.size_bytes = total_bytes
        dataset.status = DatasetStatus.VALIDATING
        db.commit()
    except RoboflowServiceError as exc:
        dataset.status = DatasetStatus.ERROR
        dataset.error_message = str(exc)
        db.commit()
        raise MLTrainingError(str(exc)) from exc

    repository.record_audit_event(db, user_id=user_id, action="dataset_roboflow_imported", entity_type="dataset", entity_id=dataset_id)
    db.commit()
    return dataset


class _ChunkStream:
    """Adapta un iterador de bytes a un objeto file-like para upload_blob_stream."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buffer = b""
        self.total_bytes = 0

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            self._buffer += chunk
            self.total_bytes += len(chunk)
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def seek(self, *args, **kwargs) -> None:
        return None


def _upload_chunks_to_blob(chunks, blob_path: str) -> int:
    stream = _ChunkStream(chunks)
    upload_blob_stream(container_name=ml_training_container_name(), object_path=blob_path, stream=stream)
    return stream.total_bytes


# ---------------------------------------------------------------------------
# Datasets — validación
# ---------------------------------------------------------------------------

def validate_dataset(db: Session, user_id: str, dataset_id: uuid.UUID) -> MLDataset:
    dataset = repository.get_dataset(db, user_id, dataset_id)
    if dataset is None:
        raise MLTrainingError("Dataset no encontrado")

    dataset.status = DatasetStatus.VALIDATING
    db.commit()

    try:
        raw_bytes = download_blob_bytes(container_name=ml_training_container_name(), object_path=dataset.storage_prefix)
    except FileNotFoundError as exc:
        dataset.status = DatasetStatus.ERROR
        dataset.error_message = "El archivo del dataset no se encontró en el almacenamiento"
        db.commit()
        raise MLTrainingError(dataset.error_message) from exc

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        zip_path = tmp_path / "dataset.zip"
        zip_path.write_bytes(raw_bytes)

        try:
            inspect_zip_safety(zip_path)
        except DatasetSecurityError as exc:
            dataset.status = DatasetStatus.ERROR
            dataset.error_message = f"Dataset rechazado por seguridad: {exc}"
            db.commit()
            raise MLTrainingError(dataset.error_message) from exc

        extract_dir = tmp_path / "extracted"
        safe_extract_zip(zip_path, extract_dir)

        report = validate_yolo_dataset(extract_dir, dataset.task_type)

    dataset.validation_report = report.model_dump(mode="json")
    dataset.image_count = report.image_count
    dataset.class_count = report.class_count
    dataset.class_names = report.class_names
    dataset.size_bytes = report.total_size_bytes or dataset.size_bytes
    dataset.status = DatasetStatus.READY if report.is_valid else DatasetStatus.ERROR
    if not report.is_valid:
        dataset.error_message = "; ".join(i.message for i in report.issues if i.level == "error")

    repository.record_audit_event(
        db, user_id=user_id, action="dataset_validated", entity_type="dataset", entity_id=dataset_id,
        metadata={"is_valid": report.is_valid, "image_count": report.image_count},
    )
    db.commit()
    db.refresh(dataset)
    return dataset


# ---------------------------------------------------------------------------
# Training jobs
# ---------------------------------------------------------------------------

def create_training_job(db: Session, user_id: str, payload: TrainingJobCreate) -> TrainingJob:
    project = repository.get_project(db, user_id, payload.project_id)
    if project is None:
        raise MLTrainingError("Proyecto no encontrado")

    dataset = repository.get_dataset(db, user_id, payload.dataset_id)
    if dataset is None:
        raise MLTrainingError("Dataset no encontrado")
    if dataset.status != DatasetStatus.READY:
        raise MLTrainingError("El dataset debe estar validado (status=ready) antes de iniciar un entrenamiento")

    try:
        recipe = get_recipe(payload.recipe)
    except UnknownRecipeError as exc:
        raise MLTrainingError(str(exc)) from exc

    if recipe.task_type != dataset.task_type or recipe.task_type != project.task_type:
        raise MLTrainingError("La receta, el dataset y el proyecto deben compartir el mismo tipo de tarea")

    try:
        validate_model_base(recipe, payload.model_base)
    except UnsupportedModelBaseError as exc:
        raise MLTrainingError(str(exc)) from exc

    limits = repository.get_training_limits(db, user_id)
    if repository.count_active_jobs(db, user_id) >= limits.max_concurrent_jobs:
        raise MLTrainingError(
            f"Ya tienes {limits.max_concurrent_jobs} entrenamiento(s) activo(s). "
            "Cancela o espera a que termine antes de lanzar otro."
        )

    timeout_minutes = min(payload.timeout_minutes, limits.max_job_duration_minutes)
    config = apply_mode_preset(payload.config)

    job_id = new_id()
    job = TrainingJob(
        id=job_id,
        user_id=user_id,
        project_id=payload.project_id,
        dataset_id=payload.dataset_id,
        recipe=recipe.key,
        task_type=recipe.task_type,
        model_base=payload.model_base,
        model_name=payload.model_name,
        config=config.model_dump(),
        status=TrainingJobStatus.READY,
        timeout_minutes=timeout_minutes,
        output_storage_prefix=job_output_prefix(user_id, str(payload.project_id), str(job_id)),
        confirmed_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    repository.record_audit_event(
        db, user_id=user_id, action="job_created", entity_type="job", entity_id=job_id,
        metadata={"recipe": recipe.key, "model_base": payload.model_base},
    )
    db.commit()

    _launch_job(db, job, recipe, config)
    return job


def _launch_job(db: Session, job: TrainingJob, recipe, config: TrainingJobConfig) -> None:
    docker_image = default_docker_image()
    compute_target = default_compute_target()
    dataset = repository.get_dataset(db, str(job.user_id), job.dataset_id)

    spec = AzureMLJobSpec(
        job_name=f"ml-{job.id}",
        display_name=job.model_name,
        command_line=build_train_command(
            recipe=recipe, model_base=job.model_base, config=config, job_id=str(job.id), project_id=str(job.project_id)
        ),
        docker_image=docker_image,
        compute_target=compute_target,
        dataset_uri=f"azureml://datastores/workspaceblobstore/paths/{dataset.storage_prefix if dataset else ''}",
        output_uri=f"azureml://datastores/workspaceblobstore/paths/{job.output_storage_prefix}",
        timeout_minutes=job.timeout_minutes,
        tags={"user_id": str(job.user_id), "project_id": str(job.project_id), "job_id": str(job.id), "app": "dataris"},
    )

    job.status = TrainingJobStatus.QUEUED
    job.compute_target = compute_target
    job.docker_image_ref = docker_image
    try:
        azure_job_name = submit_training_job(spec)
        job.azure_ml_job_id = azure_job_name
        job.azure_ml_job_name = azure_job_name
        job.started_at = datetime.now(timezone.utc)
    except AzureMLClientError as exc:
        # El módulo de Azure ML no está habilitado en este ambiente todavía
        # (AZURE_ML_ENABLED=false por defecto — ver datarisInfra). El job
        # queda registrado en DB con el motivo exacto en vez de fallar
        # silenciosamente o simular una ejecución que no ocurrió.
        job.status = TrainingJobStatus.FAILED
        job.error_code = "azure_ml_unavailable"
        job.error_message = str(exc)
        logger.warning("No se pudo enviar el job %s a Azure ML: %s", job.id, exc)
    db.commit()


def cancel_training_job(db: Session, user_id: str, job_id: uuid.UUID, is_admin: bool) -> TrainingJob:
    job = repository.get_job_for_admin(db, job_id) if is_admin else repository.get_job(db, user_id, job_id)
    if job is None:
        raise MLTrainingError("Job no encontrado")
    if job.status in TERMINAL_JOB_STATUSES:
        raise MLTrainingError("El job ya finalizó y no se puede cancelar")

    if job.azure_ml_job_id:
        try:
            azure_cancel_job(job.azure_ml_job_id)
        except AzureMLClientError as exc:
            logger.warning("No se pudo cancelar el job %s en Azure ML: %s", job.id, exc)

    job.status = TrainingJobStatus.CANCELLED
    job.cancelled_by_id = user_id
    job.cancelled_at = datetime.now(timezone.utc)
    job.finished_at = datetime.now(timezone.utc)
    repository.record_audit_event(db, user_id=user_id, action="job_cancelled", entity_type="job", entity_id=job_id)
    db.commit()
    db.refresh(job)
    return job


def refresh_job_status(db: Session, job: TrainingJob) -> TrainingJob:
    """Reconciliación de estado: traduce el estado real de Azure ML y detecta
    timeouts. Llamado desde el endpoint de refresh y desde la tarea Celery
    periódica (app/api/task.py::sync_training_job_status)."""
    if job.status in TERMINAL_JOB_STATUSES or not job.azure_ml_job_id:
        return job

    if job.started_at:
        max_duration = timedelta(minutes=job.timeout_minutes)
        if datetime.now(timezone.utc) - job.started_at > max_duration + timedelta(minutes=10):
            job.status = TrainingJobStatus.EXPIRED
            job.error_code = "timeout"
            job.error_message = "El job excedió el tiempo máximo permitido y fue marcado como expirado"
            job.finished_at = datetime.now(timezone.utc)
            try:
                azure_cancel_job(job.azure_ml_job_id)
            except AzureMLClientError:
                pass
            db.commit()
            return job

    try:
        info = azure_get_job(job.azure_ml_job_id)
    except AzureMLClientError as exc:
        logger.warning("No se pudo sincronizar el job %s: %s", job.id, exc)
        return job

    new_status = info["internal_status"]
    if new_status != job.status:
        job.status = new_status
        if new_status in TERMINAL_JOB_STATUSES:
            job.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
    return job
