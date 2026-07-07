"""ml_inference_jobs table

Revision ID: a1c2f5d7e930
Revises: 8e85bcc901c8
Create Date: 2026-07-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1c2f5d7e930"
down_revision: Union[str, Sequence[str], None] = "8e85bcc901c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Los labels van en MAYÚSCULA (nombre del miembro Python: DRAFT, QUEUED...),
    # no en minúscula (su .value). SAEnum(PyEnumClass, name=...) en
    # app/models/ml_training.py no usa values_callable, así que SQLAlchemy
    # serializa el NOMBRE del miembro al leer/escribir filas por el ORM —
    # confirmado en vivo inspeccionando pg_enum de un enum hermano
    # (ml_training_job_status) ya creado por AUTO_CREATE_TABLES. Usar el
    # .value en minúscula aquí (como se hizo por error en la migración
    # original 8e85bcc901c8) provoca "invalid input value for enum" en
    # cualquier escritura real del ORM.
    #
    # create_type=False: el tipo se crea explícitamente abajo con
    # checkfirst=True; sin create_type=False, el dispatch automático de
    # SQLAlchemy al crear la tabla (before_create del tipo Enum de la
    # columna) intenta volver a emitir CREATE TYPE y falla con "type
    # already exists" (probado en vivo). El .create() explícito funciona
    # igual con create_type=False: ese flag solo desactiva el hook
    # automático de creación/borrado ligado al ciclo de vida de la tabla.
    inference_job_status = postgresql.ENUM(
        "DRAFT",
        "QUEUED",
        "PROVISIONING_COMPUTE",
        "RUNNING",
        "FINALIZING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
        name="ml_inference_job_status",
        create_type=False,
    )
    inference_job_status.create(op.get_bind(), checkfirst=True)

    inference_input_format = postgresql.ENUM("PNG", "JPG", "TIFF", name="ml_inference_input_format", create_type=False)
    inference_input_format.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ml_inference_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "model_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ml_model_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_blob_path", sa.String(), nullable=False),
        sa.Column("input_file_name", sa.String(), nullable=False),
        sa.Column("input_format", inference_input_format, nullable=False),
        sa.Column("confidence_threshold", sa.Float(), nullable=False, server_default="0.25"),
        sa.Column("iou_threshold", sa.Float(), nullable=False, server_default="0.45"),
        sa.Column("status", inference_job_status, nullable=False, server_default="DRAFT"),
        sa.Column("azure_ml_job_id", sa.String(), nullable=True),
        sa.Column("azure_ml_job_name", sa.String(), nullable=True),
        sa.Column("compute_target", sa.String(), nullable=True),
        sa.Column("docker_image_ref", sa.String(), nullable=True),
        sa.Column("timeout_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("progress_percent", sa.Float(), nullable=True),
        sa.Column("tile_count", sa.Integer(), nullable=True),
        sa.Column("tiles_processed", sa.Integer(), nullable=True),
        sa.Column("detections", sa.JSON(), nullable=True),
        sa.Column("detection_count", sa.Integer(), nullable=True),
        sa.Column("image_width_px", sa.Integer(), nullable=True),
        sa.Column("image_height_px", sa.Integer(), nullable=True),
        sa.Column("output_storage_prefix", sa.String(), nullable=False),
        sa.Column("output_preview_blob_path", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_ml_inference_jobs_user_id", "ml_inference_jobs", ["user_id"])
    op.create_index("ix_ml_inference_jobs_model_version_id", "ml_inference_jobs", ["model_version_id"])
    op.create_index("ix_ml_inference_jobs_status", "ml_inference_jobs", ["status"])
    op.create_index("ix_ml_inference_jobs_azure_ml_job_id", "ml_inference_jobs", ["azure_ml_job_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("ml_inference_jobs")

    bind = op.get_bind()
    postgresql.ENUM(name="ml_inference_input_format").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ml_inference_job_status").drop(bind, checkfirst=True)
