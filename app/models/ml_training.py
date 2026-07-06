import uuid
from enum import Enum
from datetime import datetime
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, BigInteger, String, Text, ForeignKey, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, Mapped, mapped_column
from app.models.base import Base


class TrainingTaskType(str, Enum):
    DETECTION = "detection"
    SEGMENTATION = "segmentation"
    CLASSIFICATION = "classification"


class DatasetSource(str, Enum):
    UPLOAD = "upload"
    ROBOFLOW = "roboflow"
    EXISTING = "existing"


class DatasetStatus(str, Enum):
    UPLOADING = "uploading"
    VALIDATING = "validating"
    READY = "ready"
    ERROR = "error"


class TrainingJobStatus(str, Enum):
    DRAFT = "draft"
    DATASET_UPLOADING = "dataset_uploading"
    DATASET_VALIDATING = "dataset_validating"
    READY = "ready"
    QUEUED = "queued"
    PROVISIONING_COMPUTE = "provisioning_compute"
    RUNNING = "running"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_JOB_STATUSES = {
    TrainingJobStatus.COMPLETED,
    TrainingJobStatus.FAILED,
    TrainingJobStatus.CANCELLED,
    TrainingJobStatus.EXPIRED,
}


class ModelVersionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ModelArtifactType(str, Enum):
    WEIGHTS_BEST = "weights_best"
    WEIGHTS_LAST = "weights_last"
    METRICS = "metrics"
    CONFIG = "config"
    DATA_YAML = "data_yaml"
    MANIFEST = "manifest"
    CURVE = "curve"
    CONFUSION_MATRIX = "confusion_matrix"
    PREVIEW = "preview"
    ONNX = "onnx"
    LOG = "log"


class TrainingProject(Base):
    __tablename__ = "ml_training_projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_type: Mapped[TrainingTaskType] = mapped_column(
        SAEnum(TrainingTaskType, name="ml_training_task_type"), nullable=False
    )
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    datasets = relationship("MLDataset", back_populates="project", cascade="all, delete-orphan")
    jobs = relationship("TrainingJob", back_populates="project", cascade="all, delete-orphan")
    model_versions = relationship("ModelVersion", back_populates="project", cascade="all, delete-orphan")


class MLDataset(Base):
    __tablename__ = "ml_datasets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_training_projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[DatasetSource] = mapped_column(SAEnum(DatasetSource, name="ml_dataset_source"), nullable=False)
    status: Mapped[DatasetStatus] = mapped_column(
        SAEnum(DatasetStatus, name="ml_dataset_status"), nullable=False, default=DatasetStatus.UPLOADING
    )

    storage_prefix: Mapped[str] = mapped_column(String, nullable=False)
    task_type: Mapped[TrainingTaskType] = mapped_column(
        SAEnum(TrainingTaskType, name="ml_dataset_task_type"), nullable=False
    )

    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    class_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    class_names: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    roboflow_workspace: Mapped[str | None] = mapped_column(String, nullable=True)
    roboflow_project: Mapped[str | None] = mapped_column(String, nullable=True)
    roboflow_version: Mapped[str | None] = mapped_column(String, nullable=True)
    roboflow_format: Mapped[str | None] = mapped_column(String, nullable=True)

    validation_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project = relationship("TrainingProject", back_populates="datasets")
    files = relationship("MLDatasetFile", back_populates="dataset", cascade="all, delete-orphan")
    jobs = relationship("TrainingJob", back_populates="dataset")


class MLDatasetFile(Base):
    __tablename__ = "ml_dataset_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    blob_path: Mapped[str] = mapped_column(String, nullable=False)
    split: Mapped[str | None] = mapped_column(String, nullable=True)  # train | valid | test
    is_annotation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("MLDataset", back_populates="files")


class TrainingJob(Base):
    __tablename__ = "ml_training_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_training_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    recipe: Mapped[str] = mapped_column(String, nullable=False)
    task_type: Mapped[TrainingTaskType] = mapped_column(
        SAEnum(TrainingTaskType, name="ml_job_task_type"), nullable=False
    )
    model_base: Mapped[str] = mapped_column(String, nullable=False)
    model_name: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    status: Mapped[TrainingJobStatus] = mapped_column(
        SAEnum(TrainingJobStatus, name="ml_training_job_status"), nullable=False, default=TrainingJobStatus.DRAFT, index=True
    )
    azure_ml_job_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    azure_ml_job_name: Mapped[str | None] = mapped_column(String, nullable=True)
    compute_target: Mapped[str | None] = mapped_column(String, nullable=True)
    gpu_sku: Mapped[str | None] = mapped_column(String, nullable=True)
    docker_image_ref: Mapped[str | None] = mapped_column(String, nullable=True)

    timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    progress_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_epochs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    output_storage_prefix: Mapped[str] = mapped_column(String, nullable=False)

    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project = relationship("TrainingProject", back_populates="jobs")
    dataset = relationship("MLDataset", back_populates="jobs")
    model_versions = relationship("ModelVersion", back_populates="job")


class ModelVersion(Base):
    __tablename__ = "ml_model_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_training_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_training_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_datasets.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    task_type: Mapped[TrainingTaskType] = mapped_column(
        SAEnum(TrainingTaskType, name="ml_model_task_type"), nullable=False
    )
    model_base: Mapped[str] = mapped_column(String, nullable=False)
    recipe: Mapped[str] = mapped_column(String, nullable=False)

    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    storage_prefix: Mapped[str] = mapped_column(String, nullable=False)
    azure_ml_model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    docker_image_ref: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[ModelVersionStatus] = mapped_column(
        SAEnum(ModelVersionStatus, name="ml_model_version_status"), nullable=False, default=ModelVersionStatus.ACTIVE
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project = relationship("TrainingProject", back_populates="model_versions")
    job = relationship("TrainingJob", back_populates="model_versions")
    artifacts = relationship("ModelArtifact", back_populates="model_version", cascade="all, delete-orphan")


class ModelArtifact(Base):
    __tablename__ = "ml_model_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ml_model_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[ModelArtifactType] = mapped_column(
        SAEnum(ModelArtifactType, name="ml_model_artifact_type"), nullable=False
    )
    blob_path: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    model_version = relationship("ModelVersion", back_populates="artifacts")


class MLAuditLog(Base):
    __tablename__ = "ml_training_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MLTrainingLimit(Base):
    __tablename__ = "ml_training_limits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_dataset_size_gb: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    max_job_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=180)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
