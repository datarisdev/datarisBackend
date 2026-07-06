"""Acceso a datos del módulo de entrenamiento.

Toda query filtra siempre por user_id (owner), siguiendo el mismo patrón
que app/api/routers/parcels.py — Dataris no tiene tenant/company a nivel
SQL, así que el aislamiento real es por usuario propietario.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ml_training import (
    MLAuditLog,
    MLDataset,
    MLTrainingLimit,
    ModelArtifact,
    ModelVersion,
    TrainingJob,
    TrainingJobStatus,
    TrainingProject,
    TERMINAL_JOB_STATUSES,
)

_ACTIVE_COMPUTE_STATUSES = (TrainingJobStatus.PROVISIONING_COMPUTE, TrainingJobStatus.RUNNING, TrainingJobStatus.FINALIZING)


def list_projects(db: Session, user_id: str):
    return (
        db.query(TrainingProject)
        .filter(TrainingProject.user_id == user_id, TrainingProject.deleted_at.is_(None))
        .order_by(TrainingProject.created_at.desc())
        .all()
    )


def get_project(db: Session, user_id: str, project_id: uuid.UUID) -> TrainingProject | None:
    return (
        db.query(TrainingProject)
        .filter(TrainingProject.id == project_id, TrainingProject.user_id == user_id, TrainingProject.deleted_at.is_(None))
        .first()
    )


def list_datasets(db: Session, user_id: str, project_id: uuid.UUID | None = None):
    query = db.query(MLDataset).filter(MLDataset.user_id == user_id, MLDataset.deleted_at.is_(None))
    if project_id is not None:
        query = query.filter(MLDataset.project_id == project_id)
    return query.order_by(MLDataset.created_at.desc()).all()


def get_dataset(db: Session, user_id: str, dataset_id: uuid.UUID) -> MLDataset | None:
    return (
        db.query(MLDataset)
        .filter(MLDataset.id == dataset_id, MLDataset.user_id == user_id, MLDataset.deleted_at.is_(None))
        .first()
    )


def list_jobs(db: Session, user_id: str, project_id: uuid.UUID | None = None):
    query = db.query(TrainingJob).filter(TrainingJob.user_id == user_id)
    if project_id is not None:
        query = query.filter(TrainingJob.project_id == project_id)
    return query.order_by(TrainingJob.created_at.desc()).all()


def get_job(db: Session, user_id: str, job_id: uuid.UUID) -> TrainingJob | None:
    return db.query(TrainingJob).filter(TrainingJob.id == job_id, TrainingJob.user_id == user_id).first()


def get_job_for_admin(db: Session, job_id: uuid.UUID) -> TrainingJob | None:
    """Solo para capacidades can_admin (cancelar jobs de otros usuarios)."""
    return db.query(TrainingJob).filter(TrainingJob.id == job_id).first()


def count_active_jobs(db: Session, user_id: str) -> int:
    return (
        db.query(TrainingJob)
        .filter(TrainingJob.user_id == user_id, ~TrainingJob.status.in_(TERMINAL_JOB_STATUSES))
        .count()
    )


def list_non_terminal_jobs(db: Session):
    return db.query(TrainingJob).filter(~TrainingJob.status.in_(TERMINAL_JOB_STATUSES)).all()


def any_compute_active(db: Session) -> bool:
    """True si algún job (de cualquier usuario) tiene cómputo GPU aprovisionado
    en este momento — usado por GET /ml/status para no mentir con gpu_active."""
    return (
        db.query(TrainingJob.id)
        .filter(TrainingJob.status.in_(_ACTIVE_COMPUTE_STATUSES))
        .first()
        is not None
    )


def list_model_versions(db: Session, user_id: str, project_id: uuid.UUID | None = None):
    query = db.query(ModelVersion).filter(ModelVersion.user_id == user_id)
    if project_id is not None:
        query = query.filter(ModelVersion.project_id == project_id)
    return query.order_by(ModelVersion.created_at.desc()).all()


def get_model_version(db: Session, user_id: str, model_id: uuid.UUID) -> ModelVersion | None:
    return db.query(ModelVersion).filter(ModelVersion.id == model_id, ModelVersion.user_id == user_id).first()


def list_artifacts(db: Session, model_version_id: uuid.UUID):
    return db.query(ModelArtifact).filter(ModelArtifact.model_version_id == model_version_id).all()


def get_training_limits(db: Session, user_id: str) -> MLTrainingLimit:
    limit = db.query(MLTrainingLimit).filter(MLTrainingLimit.user_id == user_id).first()
    if limit is None:
        limit = db.query(MLTrainingLimit).filter(MLTrainingLimit.user_id.is_(None)).first()
    if limit is None:
        # Defaults conservadores si no hay ninguna fila configurada todavía.
        limit = MLTrainingLimit(
            user_id=None,
            max_concurrent_jobs=settings.ML_TRAINING_DEFAULT_MAX_CONCURRENT_JOBS,
            max_dataset_size_gb=settings.ML_TRAINING_DEFAULT_MAX_DATASET_SIZE_GB,
            max_job_duration_minutes=settings.ML_TRAINING_DEFAULT_JOB_TIMEOUT_MINUTES,
        )
    return limit


def record_audit_event(
    db: Session,
    *,
    user_id: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> None:
    entry = MLAuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        event_metadata=metadata,
    )
    db.add(entry)
