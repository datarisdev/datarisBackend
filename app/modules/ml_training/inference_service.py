"""Orquestación de inferencia ("probar modelo").

Espejo deliberado del entrenamiento (service.py): una execution del mismo
Container App Job de CPU, la misma imagen Docker
(datarisBackend/ml-training/, ahora con predict.py además de train.py), y el
mismo patrón de reconciliación de estado por polling periódico (ver
refresh_job_status en service.py y app/api/task.py::sync_training_job_status).
No hay runtime de inferencia nuevo ni cómputo separado — se reutilizan
exactamente la misma infraestructura y las mismas limitaciones (incluido el
tope de 2 vCPU/4Gi del plan Consumption) que el entrenamiento.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.ml_training import (
    InferenceInputFormat,
    InferenceJob,
    InferenceJobStatus,
    ModelArtifactType,
    ModelVersion,
    ModelVersionStatus,
    TERMINAL_INFERENCE_STATUSES,
)
from app.modules.ml_training import repository
from app.modules.ml_training.training_job_client import (
    TrainingJobClientError,
    TrainingJobSpec,
    build_blob_io_env,
    cancel_job as azure_cancel_job,
    default_docker_image,
    default_job_resource_name,
    get_job as azure_get_job,
    submit_command_job,
    translate_azure_status,
)
from app.modules.ml_training.schemas import InferenceUploadIntentRequest
from app.modules.ml_training.training_recipes import build_predict_command
from app.modules.ml_training.upload_service import inference_input_blob_path, inference_output_prefix, new_id
from app.utils.azure_blob import download_blob_bytes, generate_blob_write_url, ml_training_container_name

logger = logging.getLogger(__name__)

MAX_INFERENCE_IMAGE_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
DEFAULT_TILE_SIZE = 640
DEFAULT_TIMEOUT_MINUTES = 30
_TILE_OVERLAP = 0.25

_EXTENSION_FORMAT = {
    "png": InferenceInputFormat.PNG,
    "jpg": InferenceInputFormat.JPG,
    "jpeg": InferenceInputFormat.JPG,
    "tif": InferenceInputFormat.TIFF,
    "tiff": InferenceInputFormat.TIFF,
}


class MLInferenceError(Exception):
    """Error de negocio sanitizado del flujo de inferencia (safe para el cliente)."""


def _input_format(file_name: str) -> InferenceInputFormat:
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    fmt = _EXTENSION_FORMAT.get(ext)
    if fmt is None:
        raise MLInferenceError("Formato de imagen no soportado. Usa PNG, JPG o TIFF.")
    return fmt


def _require_testable_model(db: Session, user_id: str, model_version_id: uuid.UUID) -> ModelVersion:
    model_version = repository.get_model_version(db, user_id, model_version_id)
    if model_version is None:
        raise MLInferenceError("Modelo no encontrado")
    if model_version.status != ModelVersionStatus.ACTIVE:
        raise MLInferenceError("Este modelo está archivado; restáuralo antes de probarlo")
    has_weights = any(a.artifact_type == ModelArtifactType.WEIGHTS_BEST for a in repository.list_artifacts(db, model_version.id))
    if not has_weights:
        raise MLInferenceError("Este modelo no tiene pesos entrenados disponibles todavía")
    return model_version


def create_inference_upload_intent(db: Session, user_id: str, payload: InferenceUploadIntentRequest) -> dict:
    if payload.size_bytes > MAX_INFERENCE_IMAGE_UPLOAD_BYTES:
        raise MLInferenceError(
            f"El archivo excede el tamaño máximo permitido ({MAX_INFERENCE_IMAGE_UPLOAD_BYTES // (1024 ** 3)} GB)"
        )
    _require_testable_model(db, user_id, payload.model_version_id)
    input_format = _input_format(payload.file_name)

    job_id = new_id()
    blob_path = inference_input_blob_path(user_id, str(job_id), payload.file_name)
    ttl_minutes = 30
    upload_url = generate_blob_write_url(
        container_name=ml_training_container_name(),
        object_path=blob_path,
        expires_in=timedelta(minutes=ttl_minutes),
    )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

    job = InferenceJob(
        id=job_id,
        user_id=user_id,
        model_version_id=payload.model_version_id,
        input_blob_path=blob_path,
        input_file_name=payload.file_name,
        input_format=input_format,
        confidence_threshold=payload.confidence_threshold,
        iou_threshold=payload.iou_threshold,
        status=InferenceJobStatus.DRAFT,
        timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
        output_storage_prefix=inference_output_prefix(user_id, str(job_id)),
    )
    db.add(job)
    db.commit()

    return {
        "inference_job_id": job_id,
        "upload_url": upload_url,
        "blob_path": blob_path,
        "expires_at": expires_at,
        "max_size_bytes": MAX_INFERENCE_IMAGE_UPLOAD_BYTES,
    }


def run_inference_job(db: Session, user_id: str, job_id: uuid.UUID) -> InferenceJob:
    job = repository.get_inference_job(db, user_id, job_id)
    if job is None:
        raise MLInferenceError("Prueba de inferencia no encontrada")
    if job.status != InferenceJobStatus.DRAFT:
        raise MLInferenceError("Esta prueba ya fue iniciada")

    limits = repository.get_training_limits(db, user_id)
    if repository.count_active_inference_jobs(db, user_id) >= max(1, limits.max_concurrent_jobs):
        raise MLInferenceError("Ya tienes una prueba de inferencia activa. Espera a que termine antes de lanzar otra.")

    model_version = _require_testable_model(db, user_id, job.model_version_id)

    tile_size = DEFAULT_TILE_SIZE
    if model_version.job_id is not None:
        training_job = repository.get_job_for_admin(db, model_version.job_id)
        if training_job is not None:
            configured_size = (training_job.config or {}).get("image_size")
            if isinstance(configured_size, int) and 32 <= configured_size <= 4096:
                tile_size = configured_size

    _launch_inference_job(db, job, model_version, tile_size)
    return job


def _launch_inference_job(db: Session, job: InferenceJob, model_version: ModelVersion, tile_size: int) -> None:
    docker_image = default_docker_image()
    job_resource_name = default_job_resource_name()
    input_prefix = job.input_blob_path.rsplit("/", 1)[0] + "/"

    command, args = build_predict_command(
        image_file_name=job.input_file_name,
        tile_size=tile_size,
        overlap=_TILE_OVERLAP,
        confidence_threshold=job.confidence_threshold,
        iou_threshold=job.iou_threshold,
        job_id=str(job.id),
    )
    spec = TrainingJobSpec(
        job_name=f"ml-inference-{job.id}",
        command=command,
        args=args,
        environment_variables=build_blob_io_env(
            inputs={
                "model": model_version.storage_prefix,
                "image": input_prefix,
            },
            output_prefix=job.output_storage_prefix,
        ),
    )

    job.status = InferenceJobStatus.QUEUED
    job.compute_target = job_resource_name
    job.docker_image_ref = docker_image
    try:
        azure_job_name = submit_command_job(spec)
        job.azure_ml_job_id = azure_job_name
        job.azure_ml_job_name = azure_job_name
        job.started_at = datetime.now(timezone.utc)
    except TrainingJobClientError as exc:
        # Mismo manejo que el entrenamiento: si el módulo está deshabilitado,
        # el job queda registrado con el motivo exacto en vez de fallar
        # silenciosamente o simular una ejecución que no ocurrió.
        job.status = InferenceJobStatus.FAILED
        job.error_code = "azure_ml_unavailable"
        job.error_message = str(exc)
        logger.warning("No se pudo enviar el job de inferencia %s a Azure Container Apps: %s", job.id, exc)
    db.commit()


def cancel_inference_job(db: Session, user_id: str, job_id: uuid.UUID, is_admin: bool) -> InferenceJob:
    job = repository.get_inference_job_for_admin(db, job_id) if is_admin else repository.get_inference_job(db, user_id, job_id)
    if job is None:
        raise MLInferenceError("Prueba de inferencia no encontrada")
    if job.status in TERMINAL_INFERENCE_STATUSES:
        raise MLInferenceError("Esta prueba ya finalizó y no se puede cancelar")

    if job.azure_ml_job_id:
        try:
            azure_cancel_job(job.azure_ml_job_id)
        except TrainingJobClientError as exc:
            logger.warning("No se pudo cancelar el job de inferencia %s en Azure ML: %s", job.id, exc)

    job.status = InferenceJobStatus.CANCELLED
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job


def _sync_inference_progress(db: Session, job: InferenceJob) -> None:
    """Lee progress.json (escrito incrementalmente por predict.py tras cada
    tile) desde el blob de salida. Best-effort, mismo patrón que
    service.py::_sync_job_progress."""
    progress_path = f"{job.output_storage_prefix}progress.json"
    try:
        raw = download_blob_bytes(container_name=ml_training_container_name(), object_path=progress_path)
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.debug("No se pudo leer progress.json del job de inferencia %s: %s", job.id, exc)
        return

    try:
        payload = json.loads(raw)
    except ValueError:
        return

    tiles_processed = payload.get("tiles_processed")
    tile_count = payload.get("tile_count")
    if isinstance(tiles_processed, int):
        job.tiles_processed = tiles_processed
    if isinstance(tile_count, int):
        job.tile_count = tile_count
    if isinstance(tiles_processed, int) and isinstance(tile_count, int) and tile_count > 0:
        job.progress_percent = round(min(tiles_processed / tile_count, 1.0) * 100, 1)


def _finalize_completed_inference_job(db: Session, job: InferenceJob) -> None:
    """Al completar, lee detections.json del blob de salida y puebla los
    resultados directamente en la fila — más simple que el entrenamiento
    (service.py::_finalize_completed_job) porque no hay que registrar
    ModelVersion/ModelArtifact nuevos, solo mostrar el resultado de la
    prueba."""
    detections_path = f"{job.output_storage_prefix}detections.json"
    try:
        raw = download_blob_bytes(container_name=ml_training_container_name(), object_path=detections_path)
    except FileNotFoundError:
        logger.warning("Job de inferencia %s completado pero no se encontró detections.json en %s", job.id, detections_path)
        return
    except Exception as exc:
        logger.warning("No se pudo leer detections.json del job de inferencia %s: %s", job.id, exc)
        return

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        logger.warning("detections.json del job de inferencia %s no es JSON válido: %s", job.id, exc)
        return

    job.detections = payload.get("detections") or []
    job.detection_count = payload.get("detection_count")
    job.image_width_px = payload.get("image_width")
    job.image_height_px = payload.get("image_height")
    job.tile_count = payload.get("tile_count") or job.tile_count
    job.progress_percent = 100.0
    job.output_preview_blob_path = f"{job.output_storage_prefix}preview.png"


def refresh_inference_job_status(db: Session, job: InferenceJob) -> InferenceJob:
    """Reconciliación de estado: calco de service.py::refresh_job_status.
    Llamada desde el endpoint de refresh y desde la tarea Celery periódica
    (app/api/task.py::sync_inference_job_status)."""
    if job.status in TERMINAL_INFERENCE_STATUSES or not job.azure_ml_job_id:
        return job

    if job.started_at:
        max_duration = timedelta(minutes=job.timeout_minutes)
        if datetime.now(timezone.utc) - job.started_at > max_duration + timedelta(minutes=10):
            job.status = InferenceJobStatus.EXPIRED
            job.error_code = "timeout"
            job.error_message = "La prueba excedió el tiempo máximo permitido y fue marcada como expirada"
            job.finished_at = datetime.now(timezone.utc)
            try:
                azure_cancel_job(job.azure_ml_job_id)
            except TrainingJobClientError:
                pass
            db.commit()
            return job

    try:
        info = azure_get_job(job.azure_ml_job_id)
    except TrainingJobClientError as exc:
        logger.warning("No se pudo sincronizar el job de inferencia %s: %s", job.id, exc)
        return job

    # translate_azure_status devuelve TrainingJobStatus, pero para el
    # subconjunto de estados que realmente produce (QUEUED,
    # PROVISIONING_COMPUTE, RUNNING, FINALIZING, COMPLETED, FAILED,
    # CANCELLED) los .value coinciden 1:1 con InferenceJobStatus — nunca
    # devuelve DRAFT/DATASET_*/READY, que no aplican aquí.
    new_status = InferenceJobStatus(translate_azure_status(info["status"]).value)
    if new_status != job.status:
        job.status = new_status
        if new_status in TERMINAL_INFERENCE_STATUSES:
            job.finished_at = datetime.now(timezone.utc)
        if new_status == InferenceJobStatus.FAILED and not job.error_message:
            # Mismo gap que en service.py::refresh_job_status: la API de
            # Container Apps Jobs no expone el motivo real de una falla
            # detectada por polling, solo el estado.
            job.error_code = job.error_code or "azure_ml_job_failed"
            studio_url = info.get("studio_url")
            job.error_message = (
                f"La execution de Azure Container Apps falló (estado: {info.get('status')})."
                + (f" Detalle: {studio_url}" if studio_url else "")
            )

    if job.status == InferenceJobStatus.RUNNING:
        _sync_inference_progress(db, job)
    elif job.status == InferenceJobStatus.COMPLETED:
        _finalize_completed_inference_job(db, job)

    db.commit()
    db.refresh(job)
    return job
