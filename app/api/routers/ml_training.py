"""Router delgado del módulo de entrenamiento de modelos de visión.

Solo valida entradas HTTP, aplica policies (permisos) y delega a
app/modules/ml_training/service.py — nunca contiene lógica de negocio,
siguiendo ARCHITECTURE.md.
"""
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.modules.ml_training import artifact_service, repository, service
from app.modules.ml_training.policies import (
    MLTrainingCapabilities,
    require_ml_manage,
    require_ml_view,
)
from app.modules.ml_training.schemas import (
    DatasetOut,
    DatasetRoboflowImportRequest,
    DatasetUploadIntentRequest,
    DatasetUploadIntentResponse,
    ModelDownloadUrlResponse,
    ModelVersionOut,
    TrainingJobArtifactOut,
    TrainingJobCreate,
    TrainingJobLogsResponse,
    TrainingJobOut,
    TrainingProjectCreate,
    TrainingProjectOut,
    TrainingProjectUpdate,
)
from app.modules.ml_training.service import MLTrainingError

router = APIRouter(prefix="/ml", tags=["ML Training"])


def _handle_business_error(exc: MLTrainingError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/status")
def get_module_status(caps: MLTrainingCapabilities = Depends(require_ml_view)):
    """Estado del módulo para el dashboard: nunca enciende GPU, solo informa."""
    return {
        "module_enabled": settings.ML_TRAINING_ENABLED,
        "azure_ml_configured": False,
        "can_manage": caps.can_manage,
        "can_admin": caps.can_admin,
        "gpu_active": False,
        "note": "Abrir este módulo nunca enciende cómputo GPU. El compute solo se aprovisiona al confirmar un entrenamiento.",
    }


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects", response_model=list[TrainingProjectOut])
def list_projects(
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    return repository.list_projects(db, current_user["id"])


@router.post("/projects", response_model=TrainingProjectOut)
def create_project(
    payload: TrainingProjectCreate,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    return service.create_project(db, current_user["id"], payload)


@router.get("/projects/{project_id}", response_model=TrainingProjectOut)
def get_project(
    project_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    project = repository.get_project(db, current_user["id"], project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    return project


@router.patch("/projects/{project_id}", response_model=TrainingProjectOut)
def update_project(
    project_id: UUID,
    payload: TrainingProjectUpdate,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    try:
        return service.update_project(db, current_user["id"], project_id, payload)
    except MLTrainingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

@router.post("/datasets/upload-intent", response_model=DatasetUploadIntentResponse)
def create_dataset_upload_intent(
    payload: DatasetUploadIntentRequest,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    try:
        return service.create_dataset_upload_intent(db, current_user["id"], payload)
    except MLTrainingError as exc:
        raise _handle_business_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/datasets/import-roboflow", response_model=DatasetOut)
def import_dataset_from_roboflow(
    payload: DatasetRoboflowImportRequest,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    try:
        return service.import_dataset_from_roboflow(db, current_user["id"], payload)
    except MLTrainingError as exc:
        raise _handle_business_error(exc) from exc


@router.post("/datasets/{dataset_id}/validate", response_model=DatasetOut)
def validate_dataset(
    dataset_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    try:
        return service.validate_dataset(db, current_user["id"], dataset_id)
    except MLTrainingError as exc:
        raise _handle_business_error(exc) from exc


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(
    dataset_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    dataset = repository.get_dataset(db, current_user["id"], dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset no encontrado")
    return dataset


@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    return repository.list_datasets(db, current_user["id"], project_id)


# ---------------------------------------------------------------------------
# Training jobs
# ---------------------------------------------------------------------------

@router.post("/jobs", response_model=TrainingJobOut)
def create_job(
    payload: TrainingJobCreate,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    try:
        return service.create_training_job(db, current_user["id"], payload)
    except MLTrainingError as exc:
        raise _handle_business_error(exc) from exc


@router.get("/jobs", response_model=list[TrainingJobOut])
def list_jobs(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    return repository.list_jobs(db, current_user["id"], project_id)


@router.get("/jobs/{job_id}", response_model=TrainingJobOut)
def get_job(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    job = repository.get_job(db, current_user["id"], job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=TrainingJobOut)
def cancel_job(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    caps: MLTrainingCapabilities = Depends(require_ml_manage),
):
    try:
        return service.cancel_training_job(db, current_user["id"], job_id, is_admin=caps.can_admin)
    except MLTrainingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/refresh", response_model=TrainingJobOut)
def refresh_job(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    job = repository.get_job(db, current_user["id"], job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return service.refresh_job_status(db, job)


@router.get("/jobs/{job_id}/logs", response_model=TrainingJobLogsResponse)
def get_job_logs(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    job = repository.get_job(db, current_user["id"], job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    # Los logs detallados de Azure ML requieren streaming desde el SDK; en
    # esta fase se expone el estado consolidado. Ver azure_ml_client.get_job.
    lines = [f"status={job.status.value}"]
    if job.error_message:
        lines.append(f"error={job.error_message}")
    return TrainingJobLogsResponse(job_id=job.id, status=job.status, lines=lines)


@router.get("/jobs/{job_id}/metrics")
def get_job_metrics(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    job = repository.get_job(db, current_user["id"], job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job.metrics or {}


@router.get("/jobs/{job_id}/artifacts", response_model=list[TrainingJobArtifactOut])
def get_job_artifacts(
    job_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    job = repository.get_job(db, current_user["id"], job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    model_versions = [mv for mv in repository.list_model_versions(db, current_user["id"]) if mv.job_id == job_id]
    artifacts = []
    for mv in model_versions:
        for artifact in repository.list_artifacts(db, mv.id):
            artifacts.append(TrainingJobArtifactOut(artifact_type=artifact.artifact_type, blob_path=artifact.blob_path, size_bytes=artifact.size_bytes))
    return artifacts


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

@router.get("/models", response_model=list[ModelVersionOut])
def list_models(
    project_id: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    return repository.list_model_versions(db, current_user["id"], project_id)


@router.get("/models/{model_id}", response_model=ModelVersionOut)
def get_model(
    model_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    model = repository.get_model_version(db, current_user["id"], model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
    return model


@router.get("/models/{model_id}/download-url", response_model=list[ModelDownloadUrlResponse])
def get_model_download_url(
    model_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_view),
):
    model = repository.get_model_version(db, current_user["id"], model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
    repository.record_audit_event(db, user_id=current_user["id"], action="model_download_requested", entity_type="model", entity_id=model_id)
    db.commit()
    responses = []
    for artifact in repository.list_artifacts(db, model.id):
        url, expires_at = artifact_service.build_download_url(artifact)
        responses.append(ModelDownloadUrlResponse(artifact_type=artifact.artifact_type, download_url=url, expires_at=expires_at))
    return responses


@router.post("/models/{model_id}/archive", response_model=ModelVersionOut)
def archive_model(
    model_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    from app.models.ml_training import ModelVersionStatus

    model = repository.get_model_version(db, current_user["id"], model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
    model.status = ModelVersionStatus.ARCHIVED
    db.commit()
    db.refresh(model)
    return model


@router.post("/models/{model_id}/restore", response_model=ModelVersionOut)
def restore_model(
    model_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
    _: MLTrainingCapabilities = Depends(require_ml_manage),
):
    from app.models.ml_training import ModelVersionStatus

    model = repository.get_model_version(db, current_user["id"], model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
    model.status = ModelVersionStatus.ACTIVE
    db.commit()
    db.refresh(model)
    return model
