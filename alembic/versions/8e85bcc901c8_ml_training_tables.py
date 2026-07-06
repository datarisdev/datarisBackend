"""ml_training tables

Revision ID: 8e85bcc901c8
Revises:
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8e85bcc901c8"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    task_type_detection = postgresql.ENUM(
        "detection", "segmentation", "classification", name="ml_training_task_type"
    )
    task_type_detection.create(op.get_bind(), checkfirst=True)

    dataset_task_type = postgresql.ENUM(
        "detection", "segmentation", "classification", name="ml_dataset_task_type"
    )
    dataset_task_type.create(op.get_bind(), checkfirst=True)

    job_task_type = postgresql.ENUM(
        "detection", "segmentation", "classification", name="ml_job_task_type"
    )
    job_task_type.create(op.get_bind(), checkfirst=True)

    model_task_type = postgresql.ENUM(
        "detection", "segmentation", "classification", name="ml_model_task_type"
    )
    model_task_type.create(op.get_bind(), checkfirst=True)

    dataset_source = postgresql.ENUM("upload", "roboflow", "existing", name="ml_dataset_source")
    dataset_source.create(op.get_bind(), checkfirst=True)

    dataset_status = postgresql.ENUM("uploading", "validating", "ready", "error", name="ml_dataset_status")
    dataset_status.create(op.get_bind(), checkfirst=True)

    job_status = postgresql.ENUM(
        "draft",
        "dataset_uploading",
        "dataset_validating",
        "ready",
        "queued",
        "provisioning_compute",
        "running",
        "finalizing",
        "completed",
        "failed",
        "cancelled",
        "expired",
        name="ml_training_job_status",
    )
    job_status.create(op.get_bind(), checkfirst=True)

    model_version_status = postgresql.ENUM("active", "archived", name="ml_model_version_status")
    model_version_status.create(op.get_bind(), checkfirst=True)

    model_artifact_type = postgresql.ENUM(
        "weights_best",
        "weights_last",
        "metrics",
        "config",
        "data_yaml",
        "manifest",
        "curve",
        "confusion_matrix",
        "preview",
        "onnx",
        "log",
        name="ml_model_artifact_type",
    )
    model_artifact_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ml_training_projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task_type", task_type_detection, nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_training_projects_user_id", "ml_training_projects", ["user_id"])

    op.create_table(
        "ml_datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ml_training_projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", dataset_source, nullable=False),
        sa.Column("status", dataset_status, nullable=False, server_default="uploading"),
        sa.Column("storage_prefix", sa.String(), nullable=False),
        sa.Column("task_type", dataset_task_type, nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("class_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("class_names", sa.JSON(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("roboflow_workspace", sa.String(), nullable=True),
        sa.Column("roboflow_project", sa.String(), nullable=True),
        sa.Column("roboflow_version", sa.String(), nullable=True),
        sa.Column("roboflow_format", sa.String(), nullable=True),
        sa.Column("validation_report", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_datasets_user_id", "ml_datasets", ["user_id"])
    op.create_index("ix_ml_datasets_project_id", "ml_datasets", ["project_id"])

    op.create_table(
        "ml_dataset_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ml_datasets.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("relative_path", sa.String(), nullable=False),
        sa.Column("blob_path", sa.String(), nullable=False),
        sa.Column("split", sa.String(), nullable=True),
        sa.Column("is_annotation", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_dataset_files_dataset_id", "ml_dataset_files", ["dataset_id"])

    op.create_table(
        "ml_training_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ml_training_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ml_datasets.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("recipe", sa.String(), nullable=False),
        sa.Column("task_type", job_task_type, nullable=False),
        sa.Column("model_base", sa.String(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="draft"),
        sa.Column("azure_ml_job_id", sa.String(), nullable=True),
        sa.Column("azure_ml_job_name", sa.String(), nullable=True),
        sa.Column("compute_target", sa.String(), nullable=True),
        sa.Column("gpu_sku", sa.String(), nullable=True),
        sa.Column("docker_image_ref", sa.String(), nullable=True),
        sa.Column("timeout_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("progress_percent", sa.Float(), nullable=True),
        sa.Column("current_epoch", sa.Integer(), nullable=True),
        sa.Column("total_epochs", sa.Integer(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("output_storage_prefix", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_training_jobs_user_id", "ml_training_jobs", ["user_id"])
    op.create_index("ix_ml_training_jobs_project_id", "ml_training_jobs", ["project_id"])
    op.create_index("ix_ml_training_jobs_dataset_id", "ml_training_jobs", ["dataset_id"])
    op.create_index("ix_ml_training_jobs_status", "ml_training_jobs", ["status"])
    op.create_index("ix_ml_training_jobs_azure_ml_job_id", "ml_training_jobs", ["azure_ml_job_id"])

    op.create_table(
        "ml_model_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ml_training_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ml_training_jobs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "dataset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ml_datasets.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("task_type", model_task_type, nullable=False),
        sa.Column("model_base", sa.String(), nullable=False),
        sa.Column("recipe", sa.String(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("storage_prefix", sa.String(), nullable=False),
        sa.Column("azure_ml_model_id", sa.String(), nullable=True),
        sa.Column("docker_image_ref", sa.String(), nullable=True),
        sa.Column("status", model_version_status, nullable=False, server_default="active"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_model_versions_user_id", "ml_model_versions", ["user_id"])
    op.create_index("ix_ml_model_versions_project_id", "ml_model_versions", ["project_id"])
    op.create_index("ix_ml_model_versions_job_id", "ml_model_versions", ["job_id"])

    op.create_table(
        "ml_model_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ml_model_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_type", model_artifact_type, nullable=False),
        sa.Column("blob_path", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_model_artifacts_model_version_id", "ml_model_artifacts", ["model_version_id"])

    op.create_table(
        "ml_training_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_training_audit_logs_user_id", "ml_training_audit_logs", ["user_id"])
    op.create_index("ix_ml_training_audit_logs_action", "ml_training_audit_logs", ["action"])

    op.create_table(
        "ml_training_limits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_dataset_size_gb", sa.Float(), nullable=False, server_default="5.0"),
        sa.Column("max_job_duration_minutes", sa.Integer(), nullable=False, server_default="180"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("ml_training_limits")
    op.drop_table("ml_training_audit_logs")
    op.drop_table("ml_model_artifacts")
    op.drop_table("ml_model_versions")
    op.drop_table("ml_training_jobs")
    op.drop_table("ml_dataset_files")
    op.drop_table("ml_datasets")
    op.drop_table("ml_training_projects")

    bind = op.get_bind()
    postgresql.ENUM(name="ml_model_artifact_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_model_version_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_training_job_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_dataset_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_dataset_source").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_model_task_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_job_task_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_dataset_task_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_training_task_type").drop(bind, checkfirst=True)
