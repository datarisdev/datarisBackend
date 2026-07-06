from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

from app.models.ml_training import (
    TrainingTaskType,
    DatasetSource,
    DatasetStatus,
    TrainingJobStatus,
    ModelVersionStatus,
    ModelArtifactType,
)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class TrainingProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    task_type: TrainingTaskType
    tags: list[str] | None = None


class TrainingProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None


class TrainingProjectOut(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    description: str | None
    task_type: TrainingTaskType
    tags: list[str] | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

class DatasetUploadIntentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    task_type: TrainingTaskType
    project_id: UUID | None = None
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/zip", max_length=100)
    size_bytes: int = Field(gt=0)

    @field_validator("file_name")
    @classmethod
    def _no_path_traversal(cls, value: str) -> str:
        if "/" in value or "\\" in value or ".." in value:
            raise ValueError("Nombre de archivo inválido")
        return value


class DatasetUploadIntentResponse(BaseModel):
    dataset_id: UUID
    upload_url: str
    blob_path: str
    expires_at: datetime
    max_size_bytes: int


class DatasetRoboflowImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    task_type: TrainingTaskType
    project_id: UUID | None = None
    workspace: str = Field(min_length=1, max_length=200)
    project: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=50)
    export_format: str = Field(default="yolov8")


class DatasetValidationIssue(BaseModel):
    level: str  # "error" | "warning"
    code: str
    message: str


class DatasetValidationReport(BaseModel):
    is_valid: bool
    image_count: int
    class_count: int
    class_names: list[str]
    per_split_counts: dict[str, int]
    per_class_counts: dict[str, int]
    total_size_bytes: int
    issues: list[DatasetValidationIssue]


class DatasetOut(BaseModel):
    id: UUID
    user_id: UUID
    project_id: UUID | None
    name: str
    description: str | None
    source: DatasetSource
    status: DatasetStatus
    task_type: TrainingTaskType
    image_count: int
    class_count: int
    class_names: list[str] | None
    size_bytes: int
    roboflow_workspace: str | None
    roboflow_project: str | None
    roboflow_version: str | None
    validation_report: dict | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Training jobs
# ---------------------------------------------------------------------------

class TrainingJobConfig(BaseModel):
    """Parámetros de entrenamiento validados por backend (nunca código arbitrario)."""

    mode: str = Field(default="balanced", pattern="^(fast|balanced|accurate|advanced)$")
    epochs: int = Field(default=50, ge=1, le=500)
    batch_size: int = Field(default=16, ge=1, le=128)
    image_size: int = Field(default=640, ge=32, le=1920)
    learning_rate: float = Field(default=0.01, gt=0, le=1)
    patience: int = Field(default=20, ge=0, le=200)
    val_split: float = Field(default=0.2, ge=0.05, le=0.5)
    seed: int = Field(default=0, ge=0)
    augmentations_enabled: bool = True

    @field_validator("epochs")
    @classmethod
    def _cap_epochs_for_dev(cls, value: int) -> int:
        # Límite conservador de costo por job; ver ml_job_timeout_minutes en config.
        if value > 500:
            raise ValueError("epochs excede el máximo permitido")
        return value


class TrainingJobCreate(BaseModel):
    project_id: UUID
    dataset_id: UUID
    recipe: str = Field(min_length=1, max_length=100)
    model_base: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=200)
    config: TrainingJobConfig = Field(default_factory=TrainingJobConfig)
    timeout_minutes: int = Field(default=120, ge=5, le=720)
    confirm: bool = Field(description="Debe ser true: confirmación explícita del usuario antes de lanzar el job")

    @field_validator("confirm")
    @classmethod
    def _must_confirm(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Se requiere confirmación explícita para iniciar un entrenamiento")
        return value


class TrainingJobOut(BaseModel):
    id: UUID
    user_id: UUID
    project_id: UUID
    dataset_id: UUID
    recipe: str
    task_type: TrainingTaskType
    model_base: str
    model_name: str
    config: dict
    status: TrainingJobStatus
    azure_ml_job_id: str | None
    compute_target: str | None
    gpu_sku: str | None
    timeout_minutes: int
    progress_percent: float | None
    current_epoch: int | None
    total_epochs: int | None
    metrics: dict | None
    error_code: str | None
    error_message: str | None
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TrainingJobLogsResponse(BaseModel):
    job_id: UUID
    status: TrainingJobStatus
    lines: list[str]


class TrainingJobArtifactOut(BaseModel):
    artifact_type: ModelArtifactType
    blob_path: str
    size_bytes: int
    download_url: str | None = None


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

class ModelVersionOut(BaseModel):
    id: UUID
    user_id: UUID
    project_id: UUID
    job_id: UUID | None
    dataset_id: UUID | None
    name: str
    version: str
    task_type: TrainingTaskType
    model_base: str
    recipe: str
    metrics: dict | None
    status: ModelVersionStatus
    notes: str | None
    tags: list[str] | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ModelDownloadUrlResponse(BaseModel):
    artifact_type: ModelArtifactType
    download_url: str
    expires_at: datetime
