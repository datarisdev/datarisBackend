"""Bitácora de Campo: ciclos, labores, insumos, fenología y plantillas

Revision ID: c4d9a71b2f60
Revises: a1c2f5d7e930
Create Date: 2026-07-26

Sin tipos ENUM de PostgreSQL a propósito: `category`, `status` y `source` son
VARCHAR porque el enum nativo obliga a una migración por cada valor nuevo y ya
causó problemas de serialización en las tablas de ml_training. La validación de
los valores admitidos vive en los esquemas Pydantic del módulo.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d9a71b2f60"
down_revision: Union[str, Sequence[str], None] = "a1c2f5d7e930"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crop_cycles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "parcel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parcels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("crop_type", sa.String(length=120), nullable=False),
        sa.Column("variety", sa.String(length=180), nullable=True),
        sa.Column("template_key", sa.String(length=80), nullable=True),
        sa.Column("section_name", sa.String(length=180), nullable=True),
        sa.Column("section_geometry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("area_ha", sa.Float(), nullable=True),
        sa.Column("planting_date", sa.Date(), nullable=True),
        sa.Column("harvest_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="MXN"),
        sa.Column("target_price_per_ton", sa.Float(), nullable=True),
        sa.Column("expected_yield_ton_ha", sa.Float(), nullable=True),
        sa.Column("actual_yield_ton_ha", sa.Float(), nullable=True),
        sa.Column("budget_per_ha", sa.Float(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_crop_cycles_parcel_id", "crop_cycles", ["parcel_id"])
    op.create_index("ix_crop_cycles_user_id", "crop_cycles", ["user_id"])
    op.create_index("ix_crop_cycles_parcel_status", "crop_cycles", ["parcel_id", "status"])
    op.create_index("ix_crop_cycles_user_created", "crop_cycles", ["user_id", "created_at"])

    op.create_table(
        "field_log_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cycle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crop_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parcel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parcels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=400), nullable=False),
        sa.Column("unit", sa.String(length=60), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("unit_cost", sa.Float(), nullable=True),
        sa.Column("cost_per_ha", sa.Float(), nullable=False, server_default="0"),
        sa.Column("performed_at", sa.Date(), nullable=True),
        sa.Column("observations", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("location_verified", sa.Boolean(), nullable=True),
        sa.Column("photos", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="web"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "client_uuid", name="uq_field_log_entries_client_uuid"),
    )
    op.create_index("ix_field_log_entries_cycle_id", "field_log_entries", ["cycle_id"])
    op.create_index("ix_field_log_entries_parcel_id", "field_log_entries", ["parcel_id"])
    op.create_index("ix_field_log_entries_user_id", "field_log_entries", ["user_id"])
    op.create_index("ix_field_log_entries_performed_at", "field_log_entries", ["performed_at"])
    op.create_index(
        "ix_field_log_entries_cycle_category", "field_log_entries", ["cycle_id", "category"]
    )
    op.create_index(
        "ix_field_log_entries_cycle_performed", "field_log_entries", ["cycle_id", "performed_at"]
    )

    op.create_table(
        "field_log_entry_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("field_log_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("active_ingredient", sa.String(length=200), nullable=True),
        sa.Column("ia_concentration", sa.Float(), nullable=True),
        sa.Column("dose", sa.Float(), nullable=True),
        sa.Column("dose_unit", sa.String(length=40), nullable=True),
        sa.Column("ia_grams", sa.Float(), nullable=True),
        sa.Column("n_units", sa.Float(), nullable=True),
        sa.Column("p_units", sa.Float(), nullable=True),
        sa.Column("k_units", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_field_log_entry_inputs_entry_id", "field_log_entry_inputs", ["entry_id"])

    op.create_table(
        "phenology_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cycle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crop_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_code", sa.String(length=40), nullable=False),
        sa.Column("stage_label", sa.String(length=120), nullable=True),
        sa.Column("observed_at", sa.Date(), nullable=True),
        sa.Column("observations", sa.Text(), nullable=True),
        sa.Column("photos", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("location_verified", sa.Boolean(), nullable=True),
        sa.Column("client_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("cycle_id", "stage_code", name="uq_phenology_cycle_stage"),
    )
    op.create_index("ix_phenology_records_cycle_id", "phenology_records", ["cycle_id"])
    op.create_index("ix_phenology_records_user_id", "phenology_records", ["user_id"])

    op.create_table(
        "field_log_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("crop_type", sa.String(length=120), nullable=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "key", name="uq_field_log_templates_user_key"),
    )
    op.create_index("ix_field_log_templates_key", "field_log_templates", ["key"])
    op.create_index("ix_field_log_templates_user_id", "field_log_templates", ["user_id"])

    op.create_table(
        "field_log_labor_standards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("labor_name", sa.String(length=180), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("hours_per_ha", sa.Float(), nullable=True),
        sa.Column("fuel_l_per_ha", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "labor_name", name="uq_labor_standards_user_labor"),
    )
    op.create_index(
        "ix_field_log_labor_standards_user_id", "field_log_labor_standards", ["user_id"]
    )


def downgrade() -> None:
    op.drop_table("field_log_labor_standards")
    op.drop_table("field_log_templates")
    op.drop_table("phenology_records")
    op.drop_table("field_log_entry_inputs")
    op.drop_table("field_log_entries")
    op.drop_table("crop_cycles")
